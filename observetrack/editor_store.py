import json
import time
import uuid

from . import db

PROMPT_VERSION = 'editor-v2.3'


def decode(row):
    item = dict(row)
    for key in ['budget', 'context', 'input', 'output', 'data']:
        if key in item:
            item[key] = json.loads(item[key])
    return item


def get(flow_id):
    with db.connect() as connection:
        row = connection.execute('SELECT * FROM analysis_flows WHERE id=?', (flow_id,)).fetchone()
    return decode(row) if row else None


def for_job(job_id):
    with db.connect() as connection:
        row = connection.execute('SELECT * FROM analysis_flows WHERE job_id=?', (job_id,)).fetchone()
    return decode(row) if row else None


def create(job, context, evidence):
    existing = for_job(job['id'])
    if existing:
        return existing
    settings = db.settings()
    limit = min(40 if job['kind'] == 'weekly' else 20, settings['weekly_items' if job['kind'] == 'weekly' else 'daily_items'])
    budget = {'base': min(6, max(2, settings['analysis_v2_base_calls'])), 'extra': min(2, max(0, settings['analysis_v2_extra_calls'])),
              'extra_authorized': False, 'extra_reason': None, 'followup_limit': min(12, max(0, settings['analysis_followup_requests'])),
              'followup_used': 0, 'item_limit': limit}
    flow_id = uuid.uuid4().hex
    with db.connect() as connection:
        connection.execute('BEGIN IMMEDIATE')
        connection.execute('INSERT OR IGNORE INTO analysis_flows(id,job_id,repo_id,status,created_at,updated_at,budget,context) VALUES(?,?,?,?,?,?,?,?)',
                           (flow_id, job['id'], job['repo_id'], 'queued', time.time(), time.time(), db.encode(budget), db.encode(context)))
    flow = for_job(job['id'])
    for item in evidence:
        add_evidence(flow['id'], item)
    return get(flow['id'])


def add_evidence(flow_id, item):
    with db.connect() as connection:
        connection.execute('BEGIN IMMEDIATE')
        flow = decode(connection.execute('SELECT * FROM analysis_flows WHERE id=?', (flow_id,)).fetchone())
        key = db.encode([item['dimension'], str(item['entity_id'])])
        known = {row[0] for row in connection.execute('SELECT DISTINCT entity_key FROM analysis_evidence WHERE flow_id=?', (flow_id,))}
        if key not in known and len(known) >= flow['budget']['item_limit']:
            raise ValueError('重点事项额度已用尽')
        connection.execute('INSERT INTO analysis_evidence VALUES(?,?,?,?,?) ON CONFLICT(flow_id,evidence_id) DO UPDATE SET data=excluded.data', (flow_id, item['id'], key, db.encode(item), time.time()))


def evidence(flow_id):
    with db.connect() as connection:
        return [json.loads(row[0]) for row in connection.execute('SELECT data FROM analysis_evidence WHERE flow_id=? ORDER BY added_at,evidence_id', (flow_id,))]


def steps(flow_id):
    with db.connect() as connection:
        return [decode(row) for row in connection.execute('SELECT * FROM analysis_steps WHERE flow_id=? ORDER BY seq', (flow_id,))]


def actions(flow_id):
    with db.connect() as connection:
        return [decode(row) for row in connection.execute('SELECT * FROM analysis_actions WHERE flow_id=? ORDER BY created_at,id', (flow_id,))]


def used(flow_id, connection=None):
    if connection is not None:
        return connection.execute('SELECT count(*) FROM analysis_steps WHERE flow_id=? AND call_id IS NOT NULL', (flow_id,)).fetchone()[0]
    with db.connect() as current:
        return used(flow_id, current)


def remaining(flow_id):
    budget = get(flow_id)['budget']
    return budget['base'] + (budget['extra'] if budget['extra_authorized'] else 0) - used(flow_id)


def authorize_extra(flow_id, reason, question_id):
    if not reason or not question_id:
        raise ValueError('追加复核额度必须说明具体问题与理由')
    with db.connect() as connection:
        connection.execute('BEGIN IMMEDIATE')
        flow = decode(connection.execute('SELECT * FROM analysis_flows WHERE id=?', (flow_id,)).fetchone())
        budget = flow['budget']
        if not budget['extra_authorized']:
            budget.update(extra_authorized=True, extra_reason={'question_id': question_id, 'reason': reason})
            connection.execute('UPDATE analysis_flows SET budget=?,updated_at=? WHERE id=?', (db.encode(budget), time.time(), flow_id))


def begin(flow_id):
    with db.connect() as connection:
        connection.execute('BEGIN IMMEDIATE')
        row = connection.execute('SELECT status FROM analysis_flows WHERE id=?', (flow_id,)).fetchone()
        if not row or row[0] not in {'queued'}:
            return False
        connection.execute("UPDATE analysis_flows SET status='running',updated_at=? WHERE id=?", (time.time(), flow_id))
        return True


def reserve(flow_id, stage_key, role, payload, evidence_ids, editor=False):
    identity = db.identity([flow_id, stage_key, PROMPT_VERSION])
    with db.connect() as connection:
        connection.execute('BEGIN IMMEDIATE')
        previous = connection.execute('SELECT * FROM analysis_steps WHERE id=?', (identity,)).fetchone()
        if previous:
            return decode(previous), False
        flow = decode(connection.execute('SELECT * FROM analysis_flows WHERE id=?', (flow_id,)).fetchone())
        if flow['status'] != 'running':
            raise ValueError('分析流程当前不可派发')
        budget = flow['budget']
        available = budget['base'] + (budget['extra'] if budget['extra_authorized'] else 0) - used(flow_id, connection)
        if available < (1 if editor else 2):
            raise ValueError('预算不足；保留最后一次主编交付额度')
        seq = connection.execute('SELECT COALESCE(MAX(seq),0)+1 FROM analysis_steps WHERE flow_id=?', (flow_id,)).fetchone()[0]
        call_id = uuid.uuid4().hex
        connection.execute('INSERT INTO model_calls(id,job_id,role,status,started_at) VALUES(?,?,?,?,?)', (call_id, flow['context'].get('execution_job_id', flow['job_id']), role, 'reserved', time.time()))
        connection.execute('INSERT INTO analysis_steps(id,flow_id,seq,role,status,input,call_id,created_at) VALUES(?,?,?,?,?,?,?,?)',
                           (identity, flow_id, seq, role, 'queued', db.encode({**payload, 'prompt_version': PROMPT_VERSION}), call_id, time.time()))
        for reference in evidence_ids:
            connection.execute('INSERT INTO analysis_evidence_usage(step_id,evidence_id) VALUES(?,?)', (identity, reference))
        return decode(connection.execute('SELECT * FROM analysis_steps WHERE id=?', (identity,)).fetchone()), True


def finish_step(step_id, output=None, error=None):
    with db.connect() as connection:
        connection.execute('UPDATE analysis_steps SET status=?,output=?,error=?,ended_at=? WHERE id=?',
                           ('failed' if error else 'completed', db.encode(output or {}), error, time.time(), step_id))
        for reference in (output or {}).get('reviewed_evidence_ids', []):
            connection.execute('UPDATE analysis_evidence_usage SET reviewed=1 WHERE step_id=? AND evidence_id=? AND submitted=1', (step_id, reference))
        for claim in (output or {}).get('claims', []):
            for reference in claim['evidence_ids']:
                connection.execute('UPDATE analysis_evidence_usage SET cited=1 WHERE step_id=? AND evidence_id=? AND submitted=1', (step_id, reference))


def record_action(flow_id, editor_step, payload, evidence_version=None):
    signature = db.identity([payload, evidence_version])
    identity = db.identity([flow_id, signature])
    with db.connect() as connection:
        created = connection.execute('INSERT OR IGNORE INTO analysis_actions(id,flow_id,editor_step,signature,status,input,created_at) VALUES(?,?,?,?,?,?,?)',
                                     (identity, flow_id, editor_step, signature, 'pending', db.encode(payload), time.time())).rowcount
        return decode(connection.execute('SELECT * FROM analysis_actions WHERE id=?', (identity,)).fetchone()), bool(created)


def finish_action(action_id, status, output):
    with db.connect() as connection:
        connection.execute('UPDATE analysis_actions SET status=?,output=? WHERE id=?', (status, db.encode(output), action_id))


def finish(flow_id, status, output):
    with db.connect() as connection:
        connection.execute('UPDATE analysis_flows SET status=?,output=?,updated_at=? WHERE id=?', (status, db.encode(output), time.time(), flow_id))


def summary(flow_id, details=False):
    flow = get(flow_id)
    if not flow:
        return None
    stage_rows = steps(flow_id)
    with db.connect() as connection:
        calls = [dict(row) for row in connection.execute('SELECT m.* FROM model_calls m JOIN analysis_steps s ON s.call_id=m.id WHERE s.flow_id=?', (flow_id,))]
        coverage = [dict(row) for row in connection.execute('''SELECT u.evidence_id,MAX(u.assigned) assigned,MAX(u.submitted) submitted,
            MAX(u.reviewed) reviewed,MAX(u.cited) cited FROM analysis_evidence_usage u JOIN analysis_steps s ON s.id=u.step_id
            WHERE s.flow_id=? GROUP BY u.evidence_id''', (flow_id,))]
    stages = [{key: row[key] for key in ['id', 'seq', 'role', 'status', 'created_at', 'dispatched_at', 'ended_at', 'error']} for row in stage_rows]
    result = {key: flow[key] for key in ['id', 'job_id', 'repo_id', 'status', 'budget', 'generation', 'updated_at']}
    result.update(stages=stage_rows if details else stages, evidence_coverage=coverage, remaining=remaining(flow_id),
                  usage={'logical_calls': len(calls), 'requests': sum(row['attempts'] for row in calls),
                         'retries': sum(row['retries'] for row in calls), 'tokens': sum(row['tokens'] or 0 for row in calls),
                         'tokens_complete': all(row['tokens'] is not None for row in calls),
                         'queue_seconds': round(sum(max(0, (row['dispatched_at'] or row['ended_at'] or time.time()) - row['created_at']) for row in stage_rows), 2),
                         'request_seconds': round(sum(max(0, (row['ended_at'] or time.time()) - row['dispatched_at']) for row in stage_rows if row['dispatched_at']), 2)},
                  plans=[{'stage_id': row['id'], 'questions': row['output']['questions']} for row in stage_rows if row['output'].get('questions')],
                  findings=[{'stage_id': row['id'], 'role': row['role'], 'task': row['input'].get('context', {}).get('task', {}), **row['output']} for row in stage_rows if row['role'] != 'editor' and row['status'] == 'completed'],
                  decisions=flow['output'].get('decisions', []), resolutions=flow['output'].get('resolutions', []), unresolved=flow['output'].get('unresolved', []))
    if details:
        result['actions'] = actions(flow_id)
    return result


def resume(flow_id):
    with db.connect() as connection:
        connection.execute('BEGIN IMMEDIATE')
        row = connection.execute('SELECT * FROM analysis_flows WHERE id=?', (flow_id,)).fetchone()
        if not row:
            raise ValueError('分析流程不存在或已过期')
        flow = decode(row)
        if flow['status'] in {'running', 'queued', 'completed'}:
            raise ValueError('当前流程不可继续或已经完成')
        budget = flow['budget']
        if used(flow_id, connection) >= budget['base'] + (budget['extra'] if budget['extra_authorized'] else 0):
            raise ValueError('budget_required：原流程额度不足；重新生成才能建立新预算')
        root = connection.execute('SELECT * FROM jobs WHERE id=?', (flow['job_id'],)).fetchone()
        repository = connection.execute('SELECT enabled,deleted_at FROM repositories WHERE id=?', (flow['repo_id'],)).fetchone()
        if not root or not repository or not repository['enabled'] or repository['deleted_at']:
            raise ValueError('仓库暂停、已移除或原任务已过期')
        active = connection.execute("SELECT 1 FROM jobs WHERE repo_id=? AND status IN ('running','queued','waiting_rate_limit')", (flow['repo_id'],)).fetchone()
        if active:
            raise ValueError('仓库已有活动任务，请完成后继续')
        job_id = uuid.uuid4().hex
        generation = flow['generation'] + 1
        connection.execute('INSERT INTO jobs(id,repo_id,kind,period,dedupe,status,created_at,payload) VALUES(?,?,?,?,?,?,?,?)',
                           (job_id, flow['repo_id'], root['kind'], root['period'], f'resume:{flow_id}:{generation}', 'queued', time.time(), db.encode({'analysis_flow_id': flow_id})))
        context = flow['context']
        context['execution_job_id'] = job_id
        connection.execute("UPDATE analysis_flows SET status='queued',context=?,generation=?,updated_at=? WHERE id=?", (db.encode(context), generation, time.time(), flow_id))
        return {'id': job_id, 'flow_id': flow_id, 'remaining': budget['base'] + (budget['extra'] if budget['extra_authorized'] else 0) - used(flow_id, connection)}
