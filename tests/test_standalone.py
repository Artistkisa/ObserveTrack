import os
import json
import time
import unittest
from unittest.mock import patch, AsyncMock

import httpx
from fastapi.testclient import TestClient

from test_observetrack import DatabaseCase
from observetrack import github, db, reports, analysis
from observetrack.app import app


class StandaloneTests(DatabaseCase):
    def test_clean_install_login_empty_views_and_add_repository(self):
        # DatabaseCase creates a fixture repository; remove it to test a new install.
        with github.db.connect() as connection:
            connection.execute('DELETE FROM repositories')
        async def repository_info(client, path, *args, **kwargs):
            return {'id': 123, 'full_name': 'example/project', 'private': False,
                    'archived': False, 'disabled': False}, {}
        with patch.dict(os.environ, {'OBSERVE_ADMIN_PASSWORD': 'local-test-password'}), TestClient(app) as client:
            self.assertEqual(client.get('/').status_code, 200)
            self.assertEqual(client.get('/api/overview').status_code, 401)
            self.assertEqual(client.post('/api/login', json={'password': 'wrong'}).status_code, 401)
            login = client.post('/api/login', json={'password': 'local-test-password'})
            self.assertEqual(login.status_code, 200)
            self.assertEqual(client.get('/api/overview').json()['repositories'], [])
            self.assertEqual(client.get('/api/reports').json(), [])
            headers = {'X-CSRF-Token': login.json()['csrf']}
            self.assertEqual(client.post('/api/repositories', json={'full_name': 'bad'}, headers=headers).status_code, 400)
            with patch.object(github.GitHub, 'request', repository_info):
                result = client.post('/api/repositories', json={'full_name': 'example/project'}, headers=headers)
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(len(client.get('/api/overview').json()['repositories']), 1)
            for path in ['/patrol', '/api/rogue/overview', '/api/public/rogue/findings']:
                self.assertEqual(client.get(path).status_code, 404)

    def test_https_cookie_is_secure(self):
        with patch.dict(os.environ, {'OBSERVE_ADMIN_PASSWORD': 'test-password', 'OBSERVE_SECURE_COOKIE': 'true'}), TestClient(app, base_url='https://testserver') as client:
            response = client.post('/api/login', json={'password': 'test-password'})
            self.assertIn('secure', response.headers['set-cookie'].lower())
            self.assertEqual(client.get('/api/session').status_code, 200)


class PipelineTests(DatabaseCase, unittest.IsolatedAsyncioTestCase):
    async def collect(self, identity, changed=False):
        job = self.job(identity=identity)
        metadata = {'id': 42, 'full_name': 'test/repository', 'default_branch': 'main',
                    'stargazers_count': 3, 'forks_count': 1, 'open_issues_count': 1,
                    'has_discussions': False}
        item = self.item()
        async def dimension(client, name, *args):
            if changed and name == 'issues':
                yield item
        with patch.object(github.GitHub, 'request', AsyncMock(return_value=(metadata, None))), patch.object(github.GitHub, 'dimension', dimension):
            result = await github.collect(job)
        db.update_job(job['id'], status='completed', ended_at=time.time(), result=db.encode(result))
        return result

    async def test_complete_no_change_report_does_not_call_model(self):
        await self.collect('baseline')
        self.assertEqual((await self.collect('unchanged'))['status'], 'skipped_no_change')
        with patch.object(analysis, 'analyze', AsyncMock()) as model:
            report = await reports.generate(self.job('daily'))
            model.assert_not_called()
        self.assertEqual(report['status'], 'skipped_no_change')

    async def test_change_to_cited_model_summary(self):
        await self.collect('baseline')
        self.assertEqual((await self.collect('change', changed=True))['status'], 'changed')
        with db.connect() as connection:
            evidence_id = connection.execute('SELECT id FROM evidence').fetchone()[0]
        requests = []
        def handler(request):
            requests.append(request)
            claim = {'section': '本期结论', 'text': '观察到一个公开事项更新。', 'certainty': 'fact', 'evidence_ids': [evidence_id]}
            return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps({'claims': [claim]})}}], 'usage': {'total_tokens': 30}})
        original = analysis.analyze
        async def analyze(job, evidence):
            return await original(job, evidence, httpx.MockTransport(handler))
        with patch.dict(os.environ, {'OBSERVE_MODEL_BASE_URL': 'https://model.invalid/v1', 'OBSERVE_MODEL_NAME': 'fixture', 'OBSERVE_MODEL_API_KEY': 'test', 'OBSERVE_ANALYSIS_V2': 'false'}), patch.object(analysis, 'analyze', analyze):
            result = await reports.generate(self.job('daily'))
        self.assertEqual(result['analysis_status'], 'analyzed')
        self.assertEqual(len(requests), 1)
        with db.connect() as connection:
            row = connection.execute('SELECT markdown FROM reports WHERE id=?', (result['report_id'],)).fetchone()
        self.assertIn(evidence_id, row['markdown'])
