import asyncio
import json
import os
import time
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from test_observetrack import DatabaseCase
from observetrack import db, github, editor, editor_store as store, editor_protocol, editor_evidence, model_slots
from observetrack.app import app


def finding(source, question='Q1'):
    return {'id': 'C1', 'question_id': question, 'section': '本期结论', 'text': '观察到该事项，具体影响仍需验证。',
            'certainty': 'fact', 'evidence_ids': [source['id']], 'locators': [{'evidence_id': source['id'], 'field': 'title', 'quote': source['data']['title']}]}


class EditorCase(DatabaseCase):
    def flow(self):
        job = self.job('daily')
        item = self.item()
        github.store_items(self.repo, job['id'], 'issues', {'1': item}, False, 0)
        with db.connect() as connection:
            row = dict(connection.execute('SELECT * FROM evidence').fetchone())
        row['data'] = json.loads(row['data'])
        context = {'repository': {'id': self.repo, 'full_name': 'test/repository', 'preferences': {}}, 'kind': 'daily',
                   'period': self.period, 'coverage': [], 'catalog': [{'index_id': 'I001', 'id': row['id'], 'dimension': 'issues', 'title': 'Change'}]}
        return job, store.create(job, context, [editor_evidence.excerpt(row)])


class EditorPersistenceTests(EditorCase):
    def test_atomic_budget_reserves_final_editor_and_dedupes(self):
        job, flow = self.flow()
        self.assertTrue(store.begin(flow['id']))
        self.assertFalse(store.begin(flow['id']))
        for index in range(5):
            step, created = store.reserve(flow['id'], str(index), 'code', {}, [])
            self.assertTrue(created)
        _, created = store.reserve(flow['id'], '0', 'code', {}, [])
        self.assertFalse(created)
        with self.assertRaises(ValueError):
            store.reserve(flow['id'], 'overflow', 'code', {}, [])
        store.reserve(flow['id'], 'final', 'editor', {}, [], editor=True)
        self.assertEqual(store.remaining(flow['id']), 0)
        store.authorize_extra(flow['id'], '需核对具体版本风险', 'Q1')
        self.assertEqual(store.remaining(flow['id']), 2)
        store.authorize_extra(flow['id'], '再次申请不能增加额度', 'Q1')
        self.assertEqual(store.remaining(flow['id']), 2)

    def test_recovery_marks_uncertain_actions_and_steps(self):
        job, flow = self.flow()
        store.begin(flow['id'])
        step, _ = store.reserve(flow['id'], 'one', 'editor', {}, [], editor=True)
        action, _ = store.record_action(flow['id'], step['id'], {'type': 'read', 'question_id': 'Q1'})
        store.finish_action(action['id'], 'running', {})
        db.recover()
        self.assertEqual(store.steps(flow['id'])[0]['status'], 'interrupted')
        self.assertEqual(store.actions(flow['id'])[0]['status'], 'interrupted')
        self.assertEqual(store.remaining(flow['id']), 5)

    def test_explicit_resume_preserves_budget_and_original_results(self):
        job, flow = self.flow()
        store.begin(flow['id'])
        step, _ = store.reserve(flow['id'], 'one', 'code', {}, [])
        store.finish_step(step['id'], {'claims': [], 'summary': '已完成'})
        store.finish(flow['id'], 'partial', {'claims': []})
        db.update_job(job['id'], status='completed')
        result = store.resume(flow['id'])
        self.assertEqual(result['remaining'], 5)
        self.assertNotEqual(result['id'], job['id'])
        self.assertEqual(store.steps(flow['id'])[0]['output']['summary'], '已完成')
        with self.assertRaises(ValueError):
            store.resume(flow['id'])

    def test_followup_attempts_have_persistent_hard_limit(self):
        job, flow = self.flow()
        for attempt in range(12):
            editor_evidence.charge(flow['id'])
        with self.assertRaises(github.CollectionError):
            editor_evidence.charge(flow['id'])
        self.assertEqual(store.get(flow['id'])['budget']['followup_used'], 12)

    def test_locator_validation_and_risk_downgrade(self):
        job, flow = self.flow()
        sources = store.evidence(flow['id'])
        claim = finding(sources[0])
        claim['high_risk'] = True
        valid, rejected = editor_protocol.claims({'claims': [claim]}, sources, 'step', {'Q1'})
        self.assertEqual(valid[0]['certainty'], 'unverified')
        claim['locators'][0]['quote'] = '不存在的原文'
        valid, rejected = editor_protocol.claims({'claims': [claim]}, sources, 'step', {'Q1'})
        self.assertFalse(valid)
        self.assertTrue(rejected)

    def test_single_citation_reference_cannot_mismatch_locator(self):
        job, flow = self.flow()
        source = store.evidence(flow['id'])[0]
        claim = {**finding(source), 'citations': [source['id'] + '::P001']}
        accepted, rejected = editor_protocol.claims({'claims': [claim]}, [source], 'S1', {'Q1'})
        self.assertFalse(rejected)
        self.assertEqual(accepted[0]['locators'][0]['evidence_id'], source['id'])
        claim['citations'] = [source['id'] + '::P999']
        accepted, rejected = editor_protocol.claims({'claims': [claim]}, [source], 'S1', {'Q1'})
        self.assertFalse(accepted)
        self.assertTrue(rejected)


class EditorAsyncTests(EditorCase, __import__('unittest').IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        fixture = patch.dict(os.environ, {
            'OBSERVE_MODEL_BASE_URL': 'https://model.invalid/v1',
            'OBSERVE_MODEL_NAME': 'fixture',
            'OBSERVE_MODEL_API_KEY': 'test-model-key',
        })
        fixture.start()
        self.addCleanup(fixture.stop)

    async def test_revision_retains_task_and_closes_only_explicit_gaps(self):
        job, flow = self.flow()
        revision_contexts = []
        def handler(request):
            prompt = json.loads(json.loads(request.content)['messages'][1]['content'])
            source, context = prompt['evidence'][0], prompt['context']
            if prompt['role'] != 'editor':
                revision_contexts.append(context)
                output = {'question_id': 'Q1', 'claims': [finding(source)], 'unresolved': ['需核对版本'] if len(revision_contexts) == 1 else []}
            elif not context['stages']:
                output = {'mode': 'act', 'questions': [{'id': 'Q1', 'text': '核对影响'}], 'actions': [{'type': 'dispatch', 'role': 'code', 'question_id': 'Q1', 'question': '核对影响', 'acceptance': '版本依据', 'evidence_ids': [source['id']]}]}
            elif len(revision_contexts) == 1:
                output = {'mode': 'act', 'actions': [{'type': 'revise', 'source_step': 'S2', 'role': 'code', 'question_id': 'Q1', 'question': '补答版本边界', 'acceptance': '版本依据', 'evidence_ids': [source['id']]}]}
            else:
                output = {'mode': 'finalize', 'claims': [finding(source)], 'resolutions': [{'stage_id': 'S2', 'reason': '修订明确限定版本，已回答遗留问题', 'evidence_ids': [source['id']]}], 'decisions': [{'claim_id': claim['id'], 'disposition': 'merge', 'reason': '修订后合并'} for stage in context['stages'] for claim in stage['claims']]}
            return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(output)}}]})
        result = await editor.run(job, flow, httpx.MockTransport(handler))
        self.assertEqual(result['status'], 'analyzed')
        self.assertFalse(result['unresolved'])
        self.assertEqual(revision_contexts[0]['task']['task_id'], revision_contexts[1]['task']['task_id'])
        self.assertEqual(len(revision_contexts[1]['prior_results']), 1)
        self.assertEqual(store.steps(flow['id'])[1]['output']['unresolved'], ['需核对版本'])

    async def test_unconfigured_does_not_spend_budget(self):
        job, flow = self.flow()
        with patch.dict(os.environ, {'OBSERVE_MODEL_API_KEY': '', 'OBSERVE_MODEL_BASE_URL': ''}):
            result = await editor.run(job, flow)
        self.assertEqual(result['status'], 'model_unconfigured')
        self.assertEqual(store.remaining(flow['id']), 6)
        self.assertFalse(store.steps(flow['id']))

    async def test_cancellation_does_not_replay_entered_request(self):
        job, flow = self.flow()
        entered = asyncio.Event()
        calls = 0
        async def handler(request):
            nonlocal calls
            calls += 1
            entered.set()
            await asyncio.Event().wait()
        task = asyncio.create_task(editor.run(job, flow, httpx.MockTransport(handler)))
        await asyncio.wait_for(entered.wait(), 3)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(calls, 1)
        self.assertEqual(store.get(flow['id'])['status'], 'interrupted')
        self.assertEqual(store.remaining(flow['id']), 5)
        self.assertIsNotNone(store.steps(flow['id'])[0]['dispatched_at'])

    async def test_parallel_experts_and_merged_editor_acceptance(self):
        job, flow = self.flow()
        active, peak, editors = 0, 0, 0
        async def handler(request):
            nonlocal active, peak, editors
            prompt = json.loads(json.loads(request.content)['messages'][1]['content'])
            source, role, context = prompt['evidence'][0], prompt['role'], prompt['context']
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.02)
            if role == 'editor':
                editors += 1
                if context['phase'] == 'planning':
                    output = {'mode': 'act', 'questions': [{'id': 'Q1', 'text': '解释影响', 'acceptance': '引用实际证据'}],
                              'actions': [{'type': 'dispatch', 'role': specialist, 'question_id': 'Q1', 'question': '解释影响', 'acceptance': '引用原文', 'evidence_ids': [source['id']]} for specialist in ['code', 'security', 'community']]}
                else:
                    output = {'mode': 'finalize', 'claims': [finding(source)], 'decisions': [{'claim_id': claim['id'], 'disposition': 'adopt', 'reason': '证据与问题一致'} for stage in context['stages'] for claim in stage['claims']]}
            else:
                output = {'question_id': 'Q1', 'claims': [finding(source)], 'reviewed_evidence_ids': [source['id']], 'summary': role + '分析完成'}
            active -= 1
            return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(output)}, 'finish_reason': 'stop'}], 'usage': {'total_tokens': 50}})
        result = await editor.run(job, flow, httpx.MockTransport(handler))
        self.assertEqual(result['status'], 'analyzed')
        self.assertEqual(editors, 2)
        self.assertEqual(peak, 2)
        self.assertEqual(result['collaboration']['usage']['logical_calls'], 5)
        self.assertEqual(len(result['collaboration']['findings']), 3)
        again = await editor.run(job, store.get(flow['id']), httpx.MockTransport(handler))
        self.assertEqual(editors, 2)
        self.assertEqual(again['status'], 'analyzed')

    async def test_failed_editor_keeps_completed_expert_results(self):
        job, flow = self.flow()
        def handler(request):
            prompt = json.loads(json.loads(request.content)['messages'][1]['content'])
            source, context = prompt['evidence'][0], prompt['context']
            if prompt['role'] == 'editor' and context['phase'] != 'planning':
                return httpx.Response(400)
            if prompt['role'] == 'editor':
                output = {'mode': 'act', 'questions': [{'id': 'Q1', 'text': '检查', 'acceptance': '依据'}],
                          'actions': [{'type': 'dispatch', 'role': 'code', 'question_id': 'Q1', 'question': '检查', 'acceptance': '依据', 'evidence_ids': [source['id']]}]}
            else:
                output = {'question_id': 'Q1', 'claims': [finding(source)], 'summary': '完成'}
            return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(output)}}]})
        result = await editor.run(job, flow, httpx.MockTransport(handler))
        self.assertEqual(result['status'], 'analysis_partial')
        self.assertEqual(len(result['expert_claims']), 1)
        self.assertFalse(result['claims'])
        self.assertEqual(store.remaining(flow['id']), 3)

    async def test_saved_read_is_idempotent_and_cross_flow_rejected(self):
        job, flow = self.flow()
        store.begin(flow['id'])
        payload = {'type': 'read', 'question_id': 'Q1', 'reason': '查原文', 'evidence_ids': ['I001'], 'mode': 'saved'}
        action, _ = store.record_action(flow['id'], 'editor', payload)
        result = await editor.execute_action(job, flow['id'], action)
        same, created = store.record_action(flow['id'], 'editor-other', payload)
        self.assertFalse(created)
        self.assertEqual(result, await editor.execute_action(job, flow['id'], same))
        with self.assertRaises(ValueError):
            await editor_evidence.read(flow['id'], {**payload, 'evidence_ids': ['other-repository']})

    async def test_weekly_does_not_fetch_network(self):
        job, flow = self.flow()
        context = flow['context']
        context['kind'] = 'weekly'
        with db.connect() as connection:
            connection.execute('UPDATE analysis_flows SET context=? WHERE id=?', (db.encode(context), flow['id']))
        with self.assertRaises(ValueError):
            await editor_evidence.read(flow['id'], {'evidence_ids': ['I001'], 'mode': 'refresh'})

    async def test_related_read_uses_saved_link_and_checks_public_repository(self):
        job, flow = self.flow()
        identity = flow['context']['catalog'][0]['id']
        source = store.evidence(flow['id'])[0]
        source['data']['body'] = 'See https://github.com/public/project/issues/42 for evidence.'
        with db.connect() as connection:
            connection.execute('UPDATE evidence SET data=? WHERE id=?', (db.encode(source['data']), identity))
        seen = []
        def handler(request):
            seen.append(request.url.path)
            if request.url.path == '/repos/public/project':
                return httpx.Response(200, json={'id': 123, 'private': False})
            return httpx.Response(200, json={'id': 456, 'number': 42, 'title': 'Linked issue', 'body': 'Actual source'})
        result = await editor_evidence.read(flow['id'], {'mode': 'related', 'evidence_ids': ['I001']}, httpx.MockTransport(handler))
        self.assertEqual(seen, ['/repos/public/project', '/repos/public/project/issues/42'])
        self.assertEqual(len(result['evidence_ids']), 1)
        linked = next(item for item in store.evidence(flow['id']) if item['id'] == result['evidence_ids'][0])
        self.assertEqual(linked['data']['linked_from'], identity)
        self.assertEqual(store.get(flow['id'])['budget']['followup_used'], 2)
        with self.assertRaises(ValueError):
            await editor_evidence.read(flow['id'], {'mode': 'related', 'evidence_ids': ['https://example.com']})

    async def test_fair_slots_round_robin_and_cancellation(self):
        pool = model_slots.FairPool(1)
        order = []
        async def execute(group, marker):
            async with pool.slot(group):
                order.append(marker)
                await asyncio.sleep(0.01)
        await asyncio.gather(execute('a', 'a1'), execute('a', 'a2'), execute('b', 'b1'))
        self.assertEqual(order, ['a1', 'b1', 'a2'])
        self.assertEqual(pool.active, 0)


class EditorApiTests(EditorCase):
    def test_auth_and_disabled_continue_and_history(self):
        job, flow = self.flow()
        db.update_job(job['id'], status='completed')
        store.finish(flow['id'], 'partial', {})
        with patch.dict(os.environ, {'OBSERVE_ADMIN_PASSWORD': 'test-secret', 'OBSERVE_ANALYSIS_V2': 'false'}), TestClient(app) as client:
            self.assertEqual(client.get('/api/analysis/' + flow['id']).status_code, 401)
            token = client.post('/api/login', json={'password': 'test-secret'}).json()['csrf']
            self.assertEqual(client.get('/api/analysis/' + flow['id']).status_code, 200)
            self.assertEqual(client.post('/api/analysis/' + flow['id'] + '/continue', headers={'X-CSRF-Token': token}).status_code, 409)
