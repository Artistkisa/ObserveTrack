import asyncio
import json
import time

from . import analysis, config, db, editor_store as store, editor_protocol as protocol, editor_evidence


class FlowBusy(Exception):
    pass


def board(flow_id):
    flow = store.get(flow_id)
    stages = store.steps(flow_id)
    messages = []
    for stage in stages:
        output = stage['output']
        messages.append({'stage_id': stage['id'], 'stage_ref': f'S{stage["seq"]}', 'role': stage['role'], 'status': stage['status'], 'error': stage['error'],
                         'summary': output.get('summary'), 'questions': output.get('questions', []),
                         'claims': output.get('claims', []), 'unresolved': output.get('unresolved', []),
                         'limitations': output.get('limitations', []), 'followup_requests': output.get('followup_requests', []),
                         'decisions': output.get('decisions', []), 'rejected': output.get('rejected', [])})
    return {'repository': flow['context']['repository'], 'period': flow['context']['period'], 'kind': flow['context']['kind'],
            'coverage': flow['context']['coverage'], 'budget': flow['budget'], 'remaining': store.remaining(flow_id),
            'after_current_call': max(0, store.remaining(flow_id) - 1),
            'phase': 'accepting' if stages else 'planning', 'force_finalize': store.remaining(flow_id) <= 1,
            'catalog': flow['context']['catalog'][:200], 'catalog_total': len(flow['context']['catalog']),
            'catalog_truncated': len(flow['context']['catalog']) > 200, 'catalog_read_url': f'/api/analysis/{flow_id}/evidence',
            'stages': messages, 'actions': [{'id': item['id'], 'input': item['input'], 'status': item['status'], 'output': item['output']} for item in store.actions(flow_id)]}


async def stage(job, flow_id, key, role, context, sources, transport=None):
    source_ids = [item['id'] for item in sources]
    step, fresh = store.reserve(flow_id, key, role, {'context': context, 'evidence': sources}, source_ids, editor=role == 'editor')
    if not fresh:
        return step
    instruction = protocol.EDITOR if role == 'editor' else protocol.COMMON + '\n' + protocol.ROLES[role] + '''
按 context.task 回答指定问题。输出 {"summary":"工作摘要","question_id":"任务问题ID","claims":[],"reviewed_evidence_ids":[],"limitations":[],"unresolved":[],"followup_requests":[]}。
limitations、unresolved、followup_requests 为简短字符串数组，补读请求仅作为建议，不自行派工。'''
    try:
        output = await analysis.call_model(job['id'], role, sources, [], transport, reservation=step['call_id'], instruction=instruction, context=context)
        prior = [item for item in store.steps(flow_id) if item['id'] != step['id']]
        normalized = protocol.validate(output, sources, f'S{step["seq"]}', role, prior)
        if role != 'editor':
            expected = context['task']['question_id']
            if any(claim['question_id'] != expected for claim in normalized['claims']):
                raise ValueError('专家交付未关联派发的问题')
        if role == 'editor' and context['force_finalize'] and normalized['mode'] == 'act':
            raise ValueError('最后一次主编调用不能继续派工')
        store.finish_step(step['id'], normalized)
    except asyncio.CancelledError:
        store.finish_step(step['id'], error='执行已取消或中断；已派发请求不自动重放')
        raise
    except Exception as error:
        store.finish_step(step['id'], error=str(error)[:500])
    return next(item for item in store.steps(flow_id) if item['id'] == step['id'])


async def execute_action(job, flow_id, action, transport=None, github_transport=None):
    if action['status'] != 'pending':
        return action['output']
    payload = action['input']
    try:
        if db.cancelled(job['id']):
            raise asyncio.CancelledError()
        if payload['type'] == 'read':
            if not payload.get('reason'):
                raise ValueError('补读必须说明具体缺失信息')
            store.finish_action(action['id'], 'running', {})
            result = await editor_evidence.read(flow_id, payload, github_transport)
        else:
            role = payload.get('role')
            if role not in protocol.ROLES or payload['type'] == 'review' and role != 'reviewer' or payload['type'] == 'dispatch' and role == 'reviewer':
                raise ValueError('专家角色与动作不匹配')
            if not payload.get('question') or not payload.get('acceptance'):
                raise ValueError('派发必须包含具体问题与交付要求')
            references = payload.get('evidence_ids', [])
            known = {source['id']: source for source in store.evidence(flow_id)}
            if not references or any(reference not in known for reference in references):
                raise ValueError('专家只能读取已登记的本流程证据')
            prior = [{'stage_id': item['id'], 'output': item['output']} for item in store.steps(flow_id)
                     if item['role'] == role and item['status'] == 'completed'
                     and item['input']['context'].get('task', {}).get('question_id') == payload['question_id']]
            if payload['type'] in {'revise', 'review'}:
                matches = [item for item in store.steps(flow_id) if item['id'] == payload.get('source_step') and item['role'] != 'editor' and item['status'] == 'completed']
                if not matches:
                    raise ValueError('修订或复核必须引用已完成的专家交付')
                if payload['type'] == 'revise' and matches[0]['role'] != role:
                    raise ValueError('修订必须沿用原专家身份')
                if payload['type'] == 'revise' and matches[0]['input']['context']['task']['question_id'] != payload['question_id']:
                    raise ValueError('修订必须沿用原任务问题，新的问题应独立派发')
                prior = [{'stage_id': item['id'], 'output': item['output']} for item in matches]
            source_version = db.identity(sorted([known[reference] for reference in references], key=lambda item: item['id']))
            duplicate = [item for item in store.steps(flow_id) if item['role'] == role and item['status'] == 'completed'
                         and item['input']['context'].get('task', {}).get('question') == payload['question']
                         and db.identity(sorted(item['input']['evidence'], key=lambda source: source['id'])) == source_version]
            if duplicate:
                result = {'stage_id': duplicate[-1]['id'], 'reused': True, 'reason': '同一问题及证据已有完成结果'}
            else:
                store.finish_action(action['id'], 'running', {})
                task = {**payload, 'task_id': db.identity([flow_id, role, payload['question_id']])}
                step = await stage(job, flow_id, action['id'], role, {'task': task, 'prior_results': prior,
                                    'repository': store.get(flow_id)['context']['repository']}, [known[reference] for reference in references], transport)
                result = {'stage_id': step['id'], 'status': step['status'], 'error': step['error'], 'task_id': task['task_id'],
                          'continuation_of': prior[-1]['stage_id'] if prior else None}
        store.finish_action(action['id'], 'completed', result)
        return result
    except asyncio.CancelledError:
        store.finish_action(action['id'], 'interrupted', {'reason': '取消或重启；不自动重放'})
        raise
    except Exception as error:
        result = {'reason': str(error)[:500]}
        store.finish_action(action['id'], 'rejected', result)
        return result


def outcome(flow_id, final=None, error=None):
    stages = store.steps(flow_id)
    rejected = sum(len(item['output'].get('rejected', [])) for item in stages)
    experts = [claim for item in stages if item['role'] != 'editor' and item['status'] == 'completed' for claim in item['output'].get('claims', [])]
    errors = [item['error'] for item in stages if item['error']]
    if error:
        errors.append(error)
    final = final or {}
    unresolved = list(final.get('unresolved', []))
    dispositions = {item['claim_id'] for item in final.get('decisions', [])}
    missing = [claim['id'] for claim in experts if claim['id'] not in dispositions]
    if missing:
        unresolved.append('主编尚未说明处理结果的结论：' + ', '.join(missing))
    resolved = {item['stage_id'] for item in final.get('resolutions', [])}
    for item in stages:
        if item['role'] != 'editor' and item['id'] not in resolved:
            unresolved.extend(item['output'].get('unresolved', []))
    finalized = final.get('mode') == 'finalize'
    status = 'analyzed' if finalized and not errors and not unresolved and not rejected else 'analysis_partial' if final or experts else 'analysis_failed'
    return {'status': status, 'summary': final.get('summary', ''), 'claims': final.get('claims', []), 'expert_claims': experts, 'decisions': final.get('decisions', []),
            'unresolved': list(dict.fromkeys(unresolved)), 'resolutions': final.get('resolutions', []), 'errors': errors, 'rejected_claims': rejected,
            'finalized': finalized, 'flow_id': flow_id, 'version': 2}


async def _run(job, flow, transport=None, github_transport=None):
    if flow['status'] == 'completed':
        result = flow['output']
        result['collaboration'] = store.summary(flow['id'])
        return result
    if not store.begin(flow['id']):
        raise FlowBusy('主编已有执行实例或流程尚未明确恢复')
    flow_id = flow['id']
    result = None
    try:
        if not config.readiness()['model']:
            result = outcome(flow_id, error='model_unconfigured：模型尚未配置，未消耗调用额度')
            result['status'] = 'model_unconfigured'
            store.finish(flow_id, 'partial', result)
            result['collaboration'] = store.summary(flow_id)
            return result
        while store.remaining(flow_id) > 0:
            if db.cancelled(job['id']):
                raise asyncio.CancelledError()
            existing_steps = store.steps(flow_id)
            for prior in existing_steps:
                if prior['role'] == 'editor' and prior['status'] == 'completed':
                    for payload in prior['output'].get('actions', []):
                        version = db.identity([item for item in prior['input']['evidence'] if item['id'] in payload.get('evidence_ids', [])]) if payload['type'] != 'read' else None
                        store.record_action(flow_id, prior['id'], payload, version)
            pending = [item for item in store.actions(flow_id) if item['status'] == 'pending']
            if pending:
                db.update_job(job['id'], phase='v2:experts')
                reads = [item for item in pending if item['input']['type'] == 'read']
                for item in reads:
                    if store.remaining(flow_id) > 1:
                        await execute_action(job, flow_id, item, transport, github_transport)
                    else:
                        store.finish_action(item['id'], 'rejected', {'reason': '仅剩最终交付额度，不再补读'})
                await asyncio.gather(*(execute_action(job, flow_id, item, transport, github_transport) for item in pending if item['input']['type'] != 'read'))
            current = board(flow_id)
            db.update_job(job['id'], phase='v2:' + ('finalizing' if current['force_finalize'] else current['phase']))
            key = 'editor:' + str(len(store.steps(flow_id))) + ':' + str(store.get(flow_id)['generation'])
            step = await stage(job, flow_id, key, 'editor', current, store.evidence(flow_id), transport)
            if step['status'] != 'completed':
                result = outcome(flow_id, error='主编未完成；专家成果已保留，可使用剩余额度继续')
                break
            output = step['output']
            if output.get('extra_budget'):
                grant = output['extra_budget']
                store.authorize_extra(flow_id, grant['reason'], grant['question_id'])
            if output['mode'] in {'finalize', 'partial'}:
                if output['rejected'] and store.remaining(flow_id) > 0:
                    continue
                result = outcome(flow_id, output)
                break
        if result is None:
            result = outcome(flow_id, error='budget_required：调用额度已耗尽，保留部分结果')
        store.finish(flow_id, 'completed' if result['finalized'] else 'partial', result)
    except asyncio.CancelledError:
        result = outcome(flow_id, error='已停止；未确认的请求不会自动重放')
        store.finish(flow_id, 'interrupted', result)
        raise
    except Exception as error:
        result = outcome(flow_id, error=str(error)[:500])
        store.finish(flow_id, 'partial', result)
    result['collaboration'] = store.summary(flow_id)
    return result


async def run(job, flow, transport=None, github_transport=None):
    current = asyncio.current_task()
    async def cancellation():
        while True:
            await asyncio.sleep(0.5)
            if db.cancelled(job['id']):
                current.cancel()
                return
    watcher = asyncio.create_task(cancellation())
    try:
        return await _run(job, flow, transport, github_transport)
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)
