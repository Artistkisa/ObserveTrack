import html
import io
import json
import re
import time
import uuid
import zipfile
from collections import Counter
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import analysis, charts, db, config, editor, editor_store
from .github import GitHub, CollectionError

ZONE = ZoneInfo('Asia/Shanghai')


def window(period, kind):
    start = datetime.strptime(period, '%Y-%m-%d').replace(tzinfo=ZONE)
    end = start + timedelta(days=7 if kind == 'weekly' else 1)
    return start.timestamp(), end.timestamp()


def safe_url(url):
    return url if isinstance(url, str) and re.match(r'^https://(?:github\.com|api\.github\.com|github\.blog)/', url) else ''


def evidence_priority(item, preferences):
    data = item['data']
    rank = {'security': 100, 'releases': 80, 'pulls': 60, 'commits': 50, 'issues': 40, 'discussions': 35}.get(item['dimension'], 20)
    labels = set(data.get('labels') or [])
    rank += 40 if labels & {'breaking-change', 'breaking', 'security', 'bug'} else 0
    rank += 20 if labels & set(preferences.get('labels', [])) else 0
    rank += 15 if data.get('merged_at') else 0
    rank += min(10, sum(data.get('reactions', {}).get(key, 0) for key in ['+1', 'heart', 'eyes']))
    return rank


def period_data(repo_id, period, kind):
    start, end = window(period, kind)
    with db.connect() as connection:
        repo = dict(connection.execute('SELECT * FROM repositories WHERE id=?', (repo_id,)).fetchone())
        rows = connection.execute('SELECT * FROM changes WHERE repo_id=? AND occurred_at>=? AND occurred_at<? ORDER BY observed_at', (repo_id, start, end)).fetchall()
        changes = [{**dict(row), 'transition': json.loads(row['transition'] or '{}')} for row in rows]
        ids = list(dict.fromkeys(row['evidence_id'] for row in rows))
        evidence = []
        for identity in ids:
            row = connection.execute('SELECT * FROM evidence WHERE id=?', (identity,)).fetchone()
            if row:
                evidence.append({**dict(row), 'data': json.loads(row['data'])})
        metrics = [{**dict(row), 'data': json.loads(row['data'])} for row in connection.execute(
            'SELECT * FROM metrics WHERE repo_id=? AND observed_at>=? AND observed_at<=? ORDER BY observed_at',
            (repo_id, start - (23 if kind == 'weekly' else 29) * 86400, min(time.time(), end + 86400)))]
        coverage = [dict(row) for row in connection.execute('SELECT * FROM coverage WHERE repo_id=?', (repo_id,))]
        collections = [dict(row) for row in connection.execute("SELECT * FROM jobs WHERE repo_id=? AND kind='collect' AND period>=? AND period<? ORDER BY created_at", (repo_id, period, datetime.fromtimestamp(end, ZONE).strftime('%Y-%m-%d')))]
    return repo, changes, evidence, metrics, coverage, collections


async def prepare_evidence(job, repo, evidence, limit=None, network=True):
    limit = limit or db.settings()['weekly_items' if job['kind'] == 'weekly' else 'daily_items']
    preferences = json.loads(repo['config'])
    latest = {}
    for item in evidence:
        latest[(item['dimension'], item['entity_id'])] = item
    ranked = sorted(latest.values(), key=lambda item: (-evidence_priority(item, preferences), -item['created_at']))
    selected = []
    if not network:
        for dimensions in [{'security'}, {'commits', 'pulls', 'releases', 'tags'}, {'issues', 'issue_comments', 'review_comments', 'discussions'}]:
            match = next((item for item in ranked if item['dimension'] in dimensions), None)
            if match and len(selected) < limit:
                selected.append(match)
    selected.extend(item for item in ranked if item not in selected)
    selected = selected[:limit]
    payload = []
    client = GitHub(job['id'])
    try:
        remaining = 65000
        for item in selected:
            data = dict(item['data'])
            data['body'] = str(data.get('body', ''))[:3500]
            with db.connect() as connection:
                cached = connection.execute('SELECT data FROM cache WHERE key=?', (db.identity(['diff', item['id']]),)).fetchone()
            extra, error = json.loads(cached[0]) if cached else [], None
            if network and not cached and job['kind'] != 'weekly' and item['dimension'] in {'pulls', 'commits'} and not data.get('parent'):
                try:
                    base = '/repos/' + repo['full_name']
                    if item['dimension'] == 'pulls' and data.get('number'):
                        current, _ = await client.request(base + f'/pulls/{data["number"]}')
                        if current.get('head', {}).get('sha') != data.get('head_sha') or current.get('base', {}).get('sha') != data.get('base_sha'):
                            raise CollectionError('partial', 'PR 版本已变化，未将当前差异作为历史证据')
                        async for file in client.pages(base + f'/pulls/{data["number"]}/files'):
                            extra.append({key: file.get(key) for key in ['filename', 'status', 'additions', 'deletions', 'patch']})
                        verified, _ = await client.request(base + f'/pulls/{data["number"]}')
                        if verified.get('head', {}).get('sha') != data.get('head_sha') or verified.get('base', {}).get('sha') != data.get('base_sha'):
                            extra = []
                            raise CollectionError('partial', '读取期间 PR 版本变化，差异未采用')
                    elif item['dimension'] == 'commits':
                        commit, _ = await client.request(base + '/commits/' + item['entity_id'])
                        extra = [{key: file.get(key) for key in ['filename', 'status', 'additions', 'deletions', 'patch']} for file in commit.get('files', [])]
                        if len(extra) >= 300:
                            error = '提交文件列表可能截断，未完整阅读'
                    paths = preferences.get('paths', [])
                    extra.sort(key=lambda file: not any(str(file['filename']).startswith(path) for path in paths))
                    if not error:
                        with db.connect() as connection:
                            cache_id = db.identity(['diff', item['id']])
                            connection.execute('INSERT OR REPLACE INTO cache VALUES(?,?,?,?)', (cache_id, None, db.encode(extra), time.time()))
                except CollectionError as failure:
                    error = str(failure)
            else:
                with db.connect() as connection:
                    cached = connection.execute('SELECT data FROM cache WHERE key=?', (db.identity(['diff', item['id']]),)).fetchone()
                    extra = json.loads(cached[0]) if cached else []
            excerpts = []
            used = 0
            for file in extra:
                if used >= 6000:
                    break
                file = dict(file)
                file['patch'] = str(file.get('patch') or '未提供文本差异（可能为二进制或被截断）')[:min(3000, 6000 - used)]
                used += len(file['patch'])
                excerpts.append(file)
            record = {'id': item['id'], 'dimension': item['dimension'], 'entity_id': item['entity_id'], 'data': data,
                      'diff_excerpts': excerpts, 'reading_scope': '正文与差异为限额摘录，不代表全量审查', 'diff_error': error}
            size = len(db.encode(record))
            if size > remaining:
                break
            remaining -= size
            payload.append(record)
    finally:
        await client.close()
    return payload


def compose(repo, period, kind, payload, include_collaboration=True):
    label = '周报' if kind == 'weekly' else '日报'
    markdown = [f'# {repo["full_name"]} · {label}', f'> {period} · Asia/Shanghai · {payload["status"]}',
                '## 观察范围', '本报告基于可见公开数据；无公开安全公告不代表没有漏洞。',
                f'记录 {len(payload["changes"])} 个变化版本，分析 {len(payload["analyzed_ids"])} 个重点条目，{len(payload["unread_ids"])} 个未深读。']
    for item in payload['coverage']:
        markdown.append(f'- {item["dimension"]}：{item["status"]}' + (f' — {item["error"]}' if item.get('error') else ''))
    if payload['baseline']:
        markdown.extend(['', '这是初次基线／回补，不代表已有完整历史；图表不外推过去指标。'])
    if not payload['coverage_complete']:
        markdown.extend(['', '**本期采集覆盖不完整，不能解释为无变化。**'])
    for section in analysis.SECTIONS:
        claims = [claim for claim in payload['analysis']['claims'] if claim['section'] == section]
        if claims:
            markdown.extend(['', '## ' + section])
            for claim in claims:
                refs = ', '.join(f'`{identity}`' for identity in claim['evidence_ids'])
                markdown.append(f'- [{claim["certainty"]}] {claim["text"]}\n  证据：{refs}')
    if not payload['analysis']['claims']:
        markdown.extend(['', '## 事实简报', payload['skip_reason'] or payload['analysis'].get('summary') or '模型分析暂不可用，保留事实与完整变化列表。'])
    if payload['analysis'].get('errors'):
        markdown.extend(['', '## 分析限制', *['- ' + error for error in payload['analysis']['errors']]])
    collaboration = payload['analysis'].get('collaboration')
    if collaboration and include_collaboration:
        markdown.extend(['', '## 主编与专家分析', f'流程 `{collaboration["id"]}`；逻辑调用 {collaboration["usage"]["logical_calls"]}；剩余授权调用 {collaboration["remaining"]}。'])
        for plan in collaboration['plans']:
            for question in plan['questions']:
                markdown.append(f'- 问题 {question["id"]}：{question["text"]}；交付要求：{question["acceptance"]}')
        for finding in collaboration['findings']:
            markdown.extend(['', f'### {finding["role"]} · 专家交付', finding.get('summary', '')])
            for claim in finding.get('claims', []):
                markdown.append(f'- `{claim["id"]}` [{claim["certainty"]}] {claim["text"]}；证据：' + ', '.join(claim['evidence_ids']))
        for decision in collaboration['decisions']:
            markdown.append(f'- 主编处理 `{decision["claim_id"]}`：{decision["disposition"]}；{decision["reason"]}')
        if collaboration['unresolved']:
            markdown.extend(['', '### 未解决事项', *['- ' + item for item in collaboration['unresolved']]])
    markdown.extend(['', '## 变化与来源'])
    for item in payload['evidence']:
        data = item['data']
        title = str(data.get('title') or data.get('body') or item['entity_id']).replace('\n', ' ')[:200]
        url = safe_url(data.get('url'))
        marker = '已分析摘录' if item['id'] in payload['analyzed_ids'] else '未深读'
        markdown.append(f'- **{item["dimension"]} · {marker}** {title}\n  证据 `{item["id"]}`' + (f' · <{url}>' if url else ''))
    markdown.extend(['', '## 模型与采集用量', f'逻辑调用：{payload["usage"]["logical_calls"]}；实际请求：{payload["usage"]["requests"]}；重试：{payload["usage"]["retries"]}；Token：{payload["usage"]["tokens"] if payload["usage"]["tokens"] is not None else "未提供"}。'])
    return '\n'.join(markdown)


async def generate(job):
    with db.connect() as connection:
        existing = connection.execute('SELECT id,status FROM reports WHERE job_id=?', (job['id'],)).fetchone()
    if existing:
        return {'report_id': existing['id'], 'status': existing['status'], 'existing': True}
    repo, changes, evidence, metrics, coverage, collections = period_data(job['repo_id'], job['period'], job['kind'])
    flow_id = json.loads(job['payload']).get('analysis_flow_id')
    flow = editor_store.get(flow_id) if flow_id else editor_store.for_job(job['id'])
    if flow:
        if flow['repo_id'] != job['repo_id']:
            raise ValueError('分析流程归属不匹配')
        basis = flow['context']['report_basis']
        repo, changes, evidence, metrics, coverage, collections = [basis[key] for key in ['repo', 'changes', 'evidence', 'metrics', 'coverage', 'collections']]
    latest_collections = {}
    for run in collections:
        if run['result'] != '{}':
            latest_collections[run['period']] = run
    source_results = [json.loads(run['result']) for run in latest_collections.values()]
    complete = bool(source_results) and all(result.get('status') not in {'partial', 'failed'} for result in source_results)
    complete = complete and len(coverage) == 9 and all(item['status'] in {'complete', 'disabled'} for item in coverage)
    if job['kind'] == 'weekly':
        start, end = window(job['period'], 'weekly')
        expected = {(datetime.fromtimestamp(start, ZONE) + timedelta(days=offset)).strftime('%Y-%m-%d') for offset in range(7)}
        complete = complete and expected <= {run['period'] for run in collections if run['status'] == 'completed'}
    baseline = any(result.get('baseline') for result in source_results)
    metric_changed = any(result.get('metrics_changed') for result in source_results)
    status = 'partial' if not complete else 'baseline' if baseline else 'changed' if evidence else 'metrics_only' if metric_changed else 'skipped_no_change'
    skip = ''
    selected = []
    if evidence:
        if flow or config.analysis_v2_enabled(repo['id']):
            if not flow:
                limit = db.settings()['weekly_items' if job['kind'] == 'weekly' else 'daily_items']
                initial = max(1, limit - min(8 if job['kind'] == 'weekly' else 4, max(0, limit - 1)))
                selected = await prepare_evidence(job, repo, evidence, limit=initial, network=False)
                context = {'repository': {'id': repo['id'], 'full_name': repo['full_name'], 'preferences': json.loads(repo['config'])},
                           'kind': job['kind'], 'period': job['period'], 'coverage': coverage,
                           'catalog': [{'index_id': f'I{index + 1:03}', 'id': item['id'], 'dimension': item['dimension'], 'entity_id': item['entity_id'],
                                        'title': str(item['data'].get('title') or item['data'].get('body') or '')[:180]} for index, item in enumerate(evidence)],
                           'report_basis': {'repo': repo, 'changes': changes, 'evidence': evidence, 'metrics': metrics, 'coverage': coverage, 'collections': collections}}
                flow = editor_store.create(job, context, selected)
            outcome = await editor.run(job, flow)
            selected = editor_store.evidence(flow['id'])
            known_ids = {item['id'] for item in evidence}
            evidence = [*evidence, *[item for item in selected if item['id'] not in known_ids]]
        else:
            selected = await prepare_evidence(job, repo, evidence)
            outcome = await analysis.analyze(job, selected)
    else:
        skip = '仅指标变化，已跳过模型长文。' if metric_changed else '完整检查无变化，已跳过模型长文。' if complete and not baseline else '暂无可分析变化；基线或采集覆盖尚不足以判定无变化。'
        outcome = {'status': 'skipped', 'claims': [], 'errors': [], 'rejected_claims': 0}
    with db.connect() as connection:
        if flow:
            calls = [dict(row) for row in connection.execute('SELECT m.* FROM model_calls m JOIN analysis_steps s ON s.call_id=m.id WHERE s.flow_id=?', (flow['id'],))]
        else:
            calls = [dict(row) for row in connection.execute('SELECT * FROM model_calls WHERE job_id=?', (job['id'],))]
    usage = {'logical_calls': len(calls), 'requests': sum(call['attempts'] for call in calls), 'retries': sum(call['retries'] for call in calls),
             'tokens': sum(call['tokens'] or 0 for call in calls) if calls and all(call['tokens'] is not None for call in calls) else None,
             'model_seconds': round(sum(max(0, (call['ended_at'] or call['started_at']) - call['started_at']) for call in calls), 2),
             'github_requests': sum(run['requests'] for run in collections)}
    analyzed_ids = [item['id'] for item in selected] if outcome['claims'] else []
    if flow:
        analyzed_ids = [item['evidence_id'] for item in outcome['collaboration']['evidence_coverage'] if item['reviewed']]
    payload = {'status': status, 'coverage_complete': complete, 'baseline': baseline, 'analysis': outcome, 'coverage': coverage,
               'changes': changes, 'evidence': evidence, 'metrics': metrics, 'analyzed_ids': analyzed_ids,
               'unread_ids': [item['id'] for item in evidence if item['id'] not in analyzed_ids], 'reading_excerpts': selected,
               'usage': usage, 'skip_reason': skip, 'community': community_metrics(evidence)}
    payload['charts'] = {'trend': charts.trend(metrics, changes), 'transitions': charts.transitions(changes), 'relationships': charts.relationships(evidence)}
    if db.cancelled(job['id']):
        raise CollectionError('cancelled', '报告发布前已取消')
    markdown = compose(repo, job['period'], job['kind'], payload)
    if flow:
        payload['reader_markdown'] = compose(repo, job['period'], job['kind'], payload, include_collaboration=False)
    with db.connect() as connection:
        connection.execute('BEGIN IMMEDIATE')
        previous = connection.execute('SELECT id FROM reports WHERE job_id=?', (job['id'],)).fetchone()
        if previous:
            return {'report_id': previous[0], 'status': status}
        version = connection.execute('SELECT COALESCE(MAX(version),0)+1 FROM reports WHERE repo_id=? AND kind=? AND period=?', (repo['id'], job['kind'], job['period'])).fetchone()[0]
        report_id = uuid.uuid4().hex
        connection.execute('INSERT INTO reports VALUES(?,?,?,?,?,?,?,?,?,?)', (report_id, repo['id'], job['kind'], job['period'], version, job['id'], time.time(), status, db.encode(payload), markdown))
    return {'report_id': report_id, 'status': status, 'analysis_status': outcome['status']}


def community_metrics(evidence):
    authors = {item['data'].get('author') for item in evidence if item['data'].get('author')}
    durations, response_durations = [], []
    from .github import timestamp
    latest = {(item['dimension'], item['entity_id']): item for item in evidence}
    for item in latest.values():
        data = item['data']
        opened, closed = timestamp(data.get('created_at')), timestamp(data.get('closed_at'))
        if opened and closed and closed >= opened:
            durations.append((closed - opened) / 3600)
        if item['dimension'] == 'issues' and opened and data.get('number'):
            replies = [timestamp(reply['data'].get('created_at')) for reply in latest.values()
                       if reply['dimension'] == 'issue_comments' and reply['data'].get('issue_number') == str(data['number'])
                       and reply['data'].get('author') != data.get('author')]
            replies = [reply for reply in replies if reply and reply >= opened]
            if replies:
                response_durations.append((min(replies) - opened) / 3600)
    return {'observed_participants': len(authors), 'observed_closed_items': len(durations),
            'mean_close_hours': sum(durations) / len(durations) if durations else None,
            'first_response_hours': sum(response_durations) / len(response_durations) if response_durations else None,
            'scope': '仅本期可见且可配对的证据样本；非仓库完整历史首响或处理时长'}


def export_report(report, kind):
    payload = json.loads(report['data'])
    if kind == 'md':
        return report['markdown'].encode(), 'text/markdown; charset=utf-8'
    if kind in {'trend', 'transitions', 'relationships'}:
        return payload['charts'][kind].encode(), 'image/svg+xml'
    document = '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>GHOST ObserveTrack</title><style>body{background:#0d141b;color:#dce8ed;font:15px/1.8 system-ui;max-width:1000px;margin:40px auto;padding:24px}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit}svg{width:100%;height:auto;margin:16px 0}</style><h1>G.H.O.S.T ObserveTrack</h1><pre>' + html.escape(report['markdown']) + '</pre>' + ''.join(payload['charts'].values()) + '</html>'
    if kind == 'html':
        return document.encode(), 'text/html; charset=utf-8'
    if kind == 'zip':
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('report.md', report['markdown'])
            archive.writestr('report.html', document)
            for name, image in payload['charts'].items():
                archive.writestr(name + '.svg', image)
            archive.writestr('evidence.json', db.encode(payload['evidence']))
            if payload['analysis'].get('collaboration'):
                archive.writestr('analysis.json', db.encode(payload['analysis']['collaboration']))
        return buffer.getvalue(), 'application/zip'
    raise ValueError('Unsupported export format')
