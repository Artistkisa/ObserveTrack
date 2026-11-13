import asyncio
import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient

from observetrack import analysis, charts, config, db, github, reports, worker
from observetrack.app import app


class DatabaseCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.location = patch.object(config, 'DATA', Path(self.directory.name))
        self.location.start()
        db.initialize()
        with db.connect() as connection:
            self.repo = connection.execute("INSERT INTO repositories(full_name,github_id,created_at) VALUES('test/repository',42,?)", (time.time() - 10 * 86400,)).lastrowid
        self.period = (datetime.now(reports.ZONE).date() - timedelta(days=1)).isoformat()

    def tearDown(self):
        self.location.stop()
        self.directory.cleanup()

    def job(self, kind='collect', identity=None):
        return db.enqueue(self.repo, kind, self.period, dedupe=identity)

    def item(self, body='first', state='open'):
        return github.normalize({'id': 1, 'number': 1, 'title': 'Change', 'body': body, 'state': state,
                                 'created_at': self.period + 'T01:00:00Z', 'updated_at': self.period + 'T02:00:00Z',
                                 'html_url': 'https://github.com/test/repository/issues/1', 'user': {'login': 'alice'}}, 'issues')


class PersistenceTests(DatabaseCase):
    def test_queue_idempotence_and_one_running_per_repo(self):
        first = self.job()
        self.assertEqual(first['id'], self.job()['id'])
        self.job('daily')
        self.assertEqual(db.claim()['id'], first['id'])
        self.assertIsNone(db.claim())
        db.update_job(first['id'], status='completed', ended_at=time.time())
        self.assertEqual(db.claim()['kind'], 'daily')

    def test_failure_recovery_does_not_replay_model(self):
        job = self.job('daily')
        db.claim()
        db.recover()
        self.assertIsNone(db.claim())
        with db.connect() as connection:
            self.assertEqual(connection.execute('SELECT status FROM jobs WHERE id=?', (job['id'],)).fetchone()[0], 'interrupted')

    def test_comment_edit_is_change_but_updated_timestamp_alone_is_not(self):
        job = self.job()
        first = self.item()
        self.assertEqual(github.store_items(self.repo, job['id'], 'issues', {'1': first}, False, 0), 1)
        unchanged = dict(first)
        unchanged['data'] = {**first['data'], 'updated_at': '2026-10-16T01:00:00Z'}
        self.assertEqual(github.store_items(self.repo, job['id'], 'issues', {'1': unchanged}, False, 0), 0)
        edited = self.item('edited')
        self.assertEqual(github.store_items(self.repo, job['id'], 'issues', {'1': edited}, False, 0), 1)

    def test_state_transition_and_immutable_evidence(self):
        job = self.job()
        github.store_items(self.repo, job['id'], 'issues', {'1': self.item()}, False, 0)
        github.store_items(self.repo, job['id'], 'issues', {'1': self.item(state='closed')}, False, 0)
        with db.connect() as connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM evidence').fetchone()[0], 2)
            transitions = [json.loads(row[0]) for row in connection.execute('SELECT transition FROM changes')]
            self.assertTrue(any(item['from'] == 'open' and item['to'] == 'closed' for item in transitions))

    def test_first_baseline_excludes_old_activity(self):
        job = self.job()
        old = self.item()
        old['occurred_at'] = time.time() - 30 * 86400
        self.assertEqual(github.store_items(self.repo, job['id'], 'issues', {'1': old}, True, time.time() - 7 * 86400), 0)
        with db.connect() as connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM objects').fetchone()[0], 1)

    def test_schedule_timezone_week_and_no_duplicate_daily(self):
        moment = datetime(2026, 10, 19, 8, 31, tzinfo=reports.ZONE)
        with db.connect() as connection:
            connection.execute('UPDATE repositories SET created_at=? WHERE id=?', ((moment - timedelta(days=10)).timestamp(), self.repo))
        worker.schedule(moment)
        worker.schedule(moment)
        with db.connect() as connection:
            tasks = [dict(row) for row in connection.execute('SELECT * FROM jobs')]
        self.assertEqual(sum(row['kind'] == 'collect' for row in tasks), 1)
        self.assertEqual(next(row for row in tasks if row['kind'] == 'weekly')['period'], '2026-10-12')
        self.assertEqual(reports.window('2026-10-12', 'weekly')[1] - reports.window('2026-10-12', 'weekly')[0], 7 * 86400)

    def test_retention_keeps_active_jobs(self):
        job = self.job()
        with db.connect() as connection:
            connection.execute('UPDATE jobs SET created_at=? WHERE id=?', (time.time() - 400 * 86400, job['id']))
            connection.execute('INSERT INTO evidence VALUES(?,?,?,?,?,?,?)', ('expired', self.repo, 'issues', '1', 'hash', '{}', time.time() - 400 * 86400))
        db.cleanup()
        with db.connect() as connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM evidence').fetchone()[0], 0)
            self.assertEqual(connection.execute('SELECT count(*) FROM jobs').fetchone()[0], 1)

    def test_graphs_escape_content_and_do_not_invent_links(self):
        image = charts.relationships([{'data': {'title': '<script>alert(1)</script>'}, 'entity_id': '1', 'dimension': 'issues'}])
        self.assertNotIn('<script>', image)
        self.assertIn('没有明确关联边', image)
        self.assertIn('尚无指标采样', charts.trend([], []))


class CollectorTests(DatabaseCase, unittest.IsolatedAsyncioTestCase):
    async def test_pagination_partial_failure_preserves_cursor_and_observations(self):
        job = self.job()
        with db.connect() as connection:
            connection.execute('INSERT INTO coverage VALUES(?,?,?,?,?,?,?)', (self.repo, 'issues', 'complete', '1000', time.time(), None, job['id']))
        async def dimension(client, name, metadata, since, reconcile):
            if name == 'issues':
                yield self.item()
                raise github.CollectionError('partial', 'page 2 failed')
            if False:
                yield None
        metadata = {'id': 42, 'full_name': 'test/repository', 'default_branch': 'main', 'stargazers_count': 3, 'forks_count': 1, 'open_issues_count': 1, 'has_discussions': False}
        with patch.object(github.GitHub, 'request', AsyncMock(return_value=(metadata, None))), patch.object(github.GitHub, 'dimension', dimension):
            result = await github.collect(job)
        self.assertEqual(result['status'], 'partial')
        with db.connect() as connection:
            row = connection.execute("SELECT * FROM coverage WHERE dimension='issues'").fetchone()
            self.assertEqual(row['cursor'], '1000')
            self.assertEqual(connection.execute('SELECT count(*) FROM changes').fetchone()[0], 1)

    async def test_unchanged_and_metric_only_skip_models(self):
        metadata = {'id': 42, 'full_name': 'test/repository', 'default_branch': 'main', 'stargazers_count': 3, 'forks_count': 1, 'open_issues_count': 1, 'has_discussions': False}
        async def dimension(*args):
            if False:
                yield None
        with patch.object(github.GitHub, 'request', AsyncMock(side_effect=lambda *args: (dict(metadata), None))), patch.object(github.GitHub, 'dimension', dimension):
            self.assertEqual((await github.collect(self.job(identity='first')))['status'], 'baseline')
            self.assertEqual((await github.collect(self.job(identity='second')))['status'], 'skipped_no_change')
            metadata['stargazers_count'] += 1
            self.assertEqual((await github.collect(self.job(identity='third')))['status'], 'metrics_only')

    async def test_rate_limit_differs_from_permission_error(self):
        job = self.job()
        for status, headers, expected in [(403, {'x-ratelimit-remaining': '0', 'x-ratelimit-reset': str(int(time.time() + 600))}, 'waiting_rate_limit'), (403, {}, 'forbidden'), (401, {}, 'unauthorized'), (404, {}, 'not_found')]:
            client = github.GitHub(job['id'], transport=httpx.MockTransport(lambda request: httpx.Response(status, headers=headers, json={'message': 'denied'})))
            with self.assertRaises(github.CollectionError) as error:
                await client.request('/repos/test/repository')
            self.assertEqual(error.exception.status, expected)
            await client.close()

    async def test_cached_304_preserves_next_page(self):
        calls = []
        def handler(request):
            calls.append(request)
            if len(calls) == 1:
                return httpx.Response(200, headers={'etag': 'version', 'link': '<https://api.github.com/items?page=2>; rel="next"'}, json=[{'id': 1}])
            return httpx.Response(304)
        client = github.GitHub(transport=httpx.MockTransport(handler))
        initial = await client.request('/items')
        repeated = await client.request('/items')
        self.assertEqual(initial, repeated)
        self.assertEqual(calls[1].headers['if-none-match'], 'version')
        await client.close()

    async def test_discussions_need_token_and_external_url_rejected(self):
        client = github.GitHub(token='')
        with self.assertRaises(github.CollectionError):
            async for _ in client.discussions({'has_discussions': True}, 0, False):
                pass
        with self.assertRaises(ValueError):
            await client.request('//attacker.invalid/')
        await client.close()


class ReportTests(DatabaseCase, unittest.IsolatedAsyncioTestCase):
    async def test_empty_partial_report_never_says_no_changes(self):
        job = self.job('daily')
        with patch.object(analysis, 'analyze', AsyncMock()) as model:
            result = await reports.generate(job)
            model.assert_not_called()
        self.assertEqual(result['status'], 'partial')
        with db.connect() as connection:
            report = dict(connection.execute('SELECT * FROM reports').fetchone())
        self.assertIn('不能解释为无变化', report['markdown'])
        content, _ = reports.export_report(report, 'zip')
        import zipfile, io
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            self.assertIn('relationships.svg', archive.namelist())

    async def test_generation_versions_and_same_job_idempotence(self):
        first = self.job('daily')
        await reports.generate(first)
        await reports.generate(first)
        second = self.job('daily', 'new-version')
        await reports.generate(second)
        with db.connect() as connection:
            self.assertEqual([row[0] for row in connection.execute('SELECT version FROM reports ORDER BY version')], [1, 2])

    async def test_invalid_model_citation_is_not_published(self):
        claims, rejected = analysis.validate_claims({'claims': [{'section': '安全观察', 'text': '存在漏洞', 'certainty': 'fact', 'evidence_ids': ['invented']}]}, {'known'})
        self.assertEqual(claims, [])
        self.assertEqual(rejected, 1)

    async def test_transient_retry_and_usage_accounting(self):
        job = self.job('daily')
        attempts = []
        def handler(request):
            attempts.append(request)
            if len(attempts) == 1:
                return httpx.Response(503)
            return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps({'claims': [{'section': '本期结论', 'text': '观测到更新', 'certainty': 'fact', 'evidence_ids': ['known']}]})}}], 'usage': {'total_tokens': 40}})
        with patch.dict(os.environ, {'OBSERVE_MODEL_BASE_URL': 'https://model.invalid/v1', 'OBSERVE_MODEL_NAME': 'fixture', 'OBSERVE_MODEL_API_KEY': 'test'}), patch.object(analysis.asyncio, 'sleep', AsyncMock()):
            claims, _ = await analysis.call_model(job['id'], '主编', [{'id': 'known'}], [], httpx.MockTransport(handler))
        self.assertEqual(len(claims), 1)
        with db.connect() as connection:
            call = connection.execute('SELECT * FROM model_calls').fetchone()
            self.assertEqual((call['attempts'], call['retries'], call['tokens']), (2, 1, 40))


class ApiTests(DatabaseCase):
    def test_login_csrf_download_and_secret_redaction(self):
        with patch.dict(os.environ, {'OBSERVE_ADMIN_PASSWORD': 'correct-password', 'OBSERVE_MODEL_API_KEY': 'private-key-fixture'}), TestClient(app) as client:
            self.assertEqual(client.get('/api/overview').status_code, 401)
            self.assertEqual(client.get('/api/reports/missing/download/zip').status_code, 401)
            self.assertEqual(client.post('/api/login', json={'password': 'wrong'}).status_code, 401)
            response = client.post('/api/login', json={'password': 'correct-password'})
            self.assertEqual(response.status_code, 200)
            self.assertIn('HttpOnly', response.headers['set-cookie'])
            self.assertEqual(client.patch('/api/settings', json={'quiet': True}).status_code, 403)
            csrf = response.json()['csrf']
            result = client.patch('/api/settings', json={'quiet': True}, headers={'X-CSRF-Token': csrf})
            self.assertEqual(result.status_code, 200)
            self.assertNotIn('private-key-fixture', client.get('/api/settings').text)
            self.assertEqual(client.patch('/api/settings', json={'daily_items': 999}, headers={'X-CSRF-Token': csrf}).status_code, 400)

    def test_cancel_queued_job_never_runs(self):
        job = self.job()
        with patch.dict(os.environ, {'OBSERVE_ADMIN_PASSWORD': 'password'}), TestClient(app) as client:
            csrf = client.post('/api/login', json={'password': 'password'}).json()['csrf']
            self.assertEqual(client.post(f'/api/jobs/{job["id"]}/cancel', headers={'X-CSRF-Token': csrf}).status_code, 200)
            self.assertIsNone(db.claim())


if __name__ == '__main__':
    unittest.main()
