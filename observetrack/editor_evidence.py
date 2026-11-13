import json
import re
import time

from . import db, editor_store as store
from .github import GitHub, CollectionError, normalize


def excerpt(item):
    data = dict(item['data'])
    data['body'] = str(data.get('body') or '')[:3500]
    return {'id': item['id'], 'dimension': item['dimension'], 'entity_id': item['entity_id'], 'data': data,
            'diff_excerpts': item.get('diff_excerpts', []), 'reading_scope': '限定摘录，未执行代码或验证测试',
            'fetched_at': item.get('created_at'), 'supplemental': item.get('supplemental', False)}


def charge(flow_id):
    with db.connect() as connection:
        connection.execute('BEGIN IMMEDIATE')
        flow = store.decode(connection.execute('SELECT * FROM analysis_flows WHERE id=?', (flow_id,)).fetchone())
        budget = flow['budget']
        if budget['followup_used'] >= budget['followup_limit']:
            raise CollectionError('partial', '补读 HTTP 额度已用尽，保留缺口')
        budget['followup_used'] += 1
        connection.execute('UPDATE analysis_flows SET budget=? WHERE id=?', (db.encode(budget), flow_id))


async def read(flow_id, action, transport=None):
    flow = store.get(flow_id)
    catalog = {item['index_id']: item['id'] for item in flow['context']['catalog']}
    catalog.update({item['id']: item['id'] for item in flow['context']['catalog']})
    catalog.update({item['id']: item['id'] for item in store.evidence(flow_id)})
    references = action.get('evidence_ids', [])
    if not references or len(references) > 8 or any(reference not in catalog for reference in references):
        raise ValueError('补读只能引用本期索引或已授权证据')
    mode = action.get('mode', 'saved')
    if mode not in {'saved', 'refresh', 'diff', 'comments', 'related'}:
        raise ValueError('补读模式不受支持')
    if flow['context']['kind'] == 'weekly' and mode != 'saved':
        raise ValueError('周报只读取本地已有资料')
    result = []
    client = GitHub(flow['job_id'], transport=transport, request_hook=lambda: charge(flow_id))
    try:
        for reference in references:
            identity = catalog[reference]
            with db.connect() as connection:
                row = connection.execute('SELECT * FROM evidence WHERE id=? AND repo_id=?', (identity, flow['repo_id'])).fetchone()
            if not row:
                raise ValueError('证据不存在、已过期或归属不匹配')
            source = {**dict(row), 'data': json.loads(row['data'])}
            record = excerpt(source)
            existing = next((item for item in store.evidence(flow_id) if item['id'] == record['id']), None)
            if existing and existing.get('diff_excerpts'):
                record['diff_excerpts'] = existing['diff_excerpts']
            store.add_evidence(flow_id, record)
            if mode == 'related':
                links = list(dict.fromkeys(re.findall(r'https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/(issues|pull)/(\d+)(?=[\s)#?\]<>.,;]|$)', str(source['data'].get('body') or ''))))
                if not links:
                    raise ValueError('原始正文中没有明确关联的公开 Issue 或 PR 链接')
                for repository, resource, number in links[:4]:
                    metadata, _ = await client.request('/repos/' + repository)
                    if metadata.get('private') is not False or metadata.get('visibility', 'public') != 'public':
                        raise ValueError('关联资料必须属于公开仓库')
                    dimension = 'pulls' if resource == 'pull' else 'issues'
                    current, _ = await client.request('/repos/' + repository + '/' + dimension + '/' + number)
                    item = normalize(current, dimension)
                    entity = 'related:' + str(metadata['id']) + ':' + item['entity_id']
                    data = {**item['data'], 'source_repository': repository, 'linked_from': source['id']}
                    digest = db.identity(data)
                    identity = db.identity([flow['repo_id'], dimension, entity, digest])
                    now = time.time()
                    with db.connect() as connection:
                        connection.execute('INSERT OR IGNORE INTO evidence VALUES(?,?,?,?,?,?,?)',
                                           (identity, flow['repo_id'], dimension, entity, digest, db.encode(data), now))
                    store.add_evidence(flow_id, excerpt({'id': identity, 'dimension': dimension, 'entity_id': entity,
                                                        'data': data, 'created_at': now, 'supplemental': True}))
                    result.append(identity)
                continue
            if mode == 'comments':
                if source['dimension'] not in {'issues', 'pulls'} or source['data'].get('parent') or not source['data'].get('number'):
                    raise ValueError('仅支持明确 Issue 或 PR 的评论补读')
                path = '/repos/' + source['data'].get('source_repository', flow['context']['repository']['full_name']) + '/issues/' + str(int(source['data']['number'])) + '/comments'
                async for comment in client.pages(path):
                    item = normalize(comment, 'issue_comments', parent=source['entity_id'])
                    identity = db.identity([flow['repo_id'], 'issue_comments', item['entity_id'], item['digest']])
                    now = time.time()
                    with db.connect() as connection:
                        connection.execute('INSERT OR IGNORE INTO evidence VALUES(?,?,?,?,?,?,?)',
                                           (identity, flow['repo_id'], 'issue_comments', item['entity_id'], item['digest'], db.encode(item['data']), now))
                    store.add_evidence(flow_id, excerpt({'id': identity, 'dimension': 'issue_comments', 'entity_id': item['entity_id'],
                                                        'data': item['data'], 'created_at': now, 'supplemental': True}))
                    result.append(identity)
                continue
            if mode != 'saved':
                data = source['data']
                dimension = source['dimension']
                if data.get('parent'):
                    raise ValueError('关联子记录只能读取已保存版本')
                if dimension not in {'issues', 'pulls', 'commits', 'releases'}:
                    raise ValueError('该资料类型暂只支持已保存版本')
                target = str(data['number']) if dimension in {'issues', 'pulls'} else source['entity_id']
                if not target.isalnum():
                    raise ValueError('资料对象标识不合法')
                base = '/repos/' + data.get('source_repository', flow['context']['repository']['full_name'])
                current, _ = await client.request(base + '/' + dimension + '/' + target)
                if mode == 'diff':
                    if dimension not in {'pulls', 'commits'}:
                        raise ValueError('该资料没有支持的代码差异')
                    files = []
                    if dimension == 'pulls':
                        if current['head']['sha'] != data.get('head_sha') or current['base']['sha'] != data.get('base_sha'):
                            raise ValueError('PR 版本变化，不能将当前差异作为旧版证据')
                        async for file in client.pages(base + '/pulls/' + target + '/files'):
                            files.append(file)
                        after, _ = await client.request(base + '/pulls/' + target)
                        if after['head']['sha'] != current['head']['sha'] or after['base']['sha'] != current['base']['sha']:
                            raise ValueError('读取期间 PR 版本改变，差异未采用')
                    else:
                        files = current.get('files', [])
                    record['diff_excerpts'] = [{key: (str(file.get(key) or '')[:2000] if key == 'patch' else file.get(key))
                                                for key in ['filename', 'status', 'patch']} for file in files[:3]]
                    record['reading_scope'] = '最多三个文件的差异摘录；未执行代码，未保证覆盖完整'
                else:
                    item = normalize(current, dimension, entity_id=source['entity_id'])
                    for field in ['source_repository', 'linked_from']:
                        if field in data:
                            item['data'][field] = data[field]
                    item['digest'] = db.identity(item['data'])
                    identity = db.identity([flow['repo_id'], dimension, source['entity_id'], item['digest']])
                    now = time.time()
                    with db.connect() as connection:
                        connection.execute('INSERT OR IGNORE INTO evidence VALUES(?,?,?,?,?,?,?)',
                                           (identity, flow['repo_id'], dimension, source['entity_id'], item['digest'], db.encode(item['data']), now))
                    record = excerpt({'id': identity, 'dimension': dimension, 'entity_id': source['entity_id'],
                                      'data': item['data'], 'created_at': now, 'supplemental': True})
                with db.connect() as connection:
                    connection.execute('UPDATE analysis_evidence SET data=? WHERE flow_id=? AND evidence_id=?', (db.encode(record), flow_id, record['id']))
                store.add_evidence(flow_id, record)
            result.append(record['id'])
        return {'evidence_ids': result, 'mode': mode}
    finally:
        await client.close()
