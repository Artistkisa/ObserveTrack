import asyncio
import json
import os
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx

from . import db

DIMENSIONS = ('commits', 'releases', 'tags', 'issues', 'issue_comments', 'pulls', 'review_comments', 'discussions', 'security')


class CollectionError(Exception):
    def __init__(self, status, message, retry_at=None):
        self.status, self.retry_at = status, retry_at
        super().__init__(message)


def timestamp(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
    except (TypeError, ValueError):
        return None


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace('+00:00', 'Z')


def normalize(item, dimension, *, entity_id=None, parent=None):
    user = item.get('user') or item.get('author') or {}
    if not isinstance(user, dict):
        user = {}
    commit = item.get('commit') or {}
    author = commit.get('author') or {}
    data = {'title': item.get('title') or item.get('name') or item.get('summary') or commit.get('message', '').split('\n')[0],
            'body': item.get('body') or item.get('description') or commit.get('message') or '',
            'url': item.get('html_url') or item.get('url') or '', 'author': user.get('login'),
            'author_association': item.get('author_association'),
            'created_at': item.get('created_at') or item.get('createdAt') or author.get('date'),
            'updated_at': item.get('updated_at') or item.get('updatedAt'),
            'number': item.get('number'), 'state': item.get('state'), 'parent': parent,
            'labels': sorted(label['name'] for label in item.get('labels', []) if isinstance(label, dict)),
            'dimension': dimension}
    fields = ('closed_at', 'merged_at', 'draft', 'prerelease', 'published_at', 'withdrawn_at', 'severity', 'cve_id', 'ghsa_id',
              'merge_commit_sha', 'merged', 'conclusion', 'status', 'submitted_at', 'event', 'commit_id', 'dismissed_review',
              'vulnerabilities', 'cvss', 'answerChosenAt', 'isAnswered', 'isLocked', 'reactions', 'locked', 'assignees', 'tag_name', 'target_commitish')
    for field in fields:
        if field in item:
            data[field] = item[field]
    if 'head' in item:
        data['head_sha'] = item['head']['sha']
    if 'base' in item:
        data['base_sha'] = item['base']['sha']
    if item.get('answer'):
        data['answer_id'] = item['answer'].get('id')
    if dimension == 'tags':
        data['sha'] = commit.get('sha')
    if item.get('pull_request'):
        data['is_pull_request'] = True
    if item.get('issue_url'):
        data['issue_number'] = item['issue_url'].rsplit('/', 1)[-1]
    if item.get('source'):
        source = item['source'].get('issue') or {}
        data['source'] = {'number': source.get('number'), 'url': source.get('html_url')}
    reference = entity_id or str(item.get('id') or item.get('sha') or item.get('ghsa_id') or item.get('name'))
    occurred = timestamp(item.get('updated_at') or item.get('updatedAt') or item.get('submitted_at') or item.get('published_at') or data['created_at'])
    fingerprint = {key: value for key, value in data.items() if key not in {'updated_at'}}
    return {'entity_id': reference, 'data': data, 'digest': db.identity(fingerprint), 'occurred_at': occurred}


class GitHub:
    def __init__(self, job_id=None, token=None, transport=None, request_hook=None):
        self.job_id = job_id
        self.requests = 0
        self.request_hook = request_hook
        self.token = token if token is not None else os.getenv('OBSERVE_GITHUB_TOKEN', '')
        headers = {'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'GHOST-ObserveTrack/1.0'}
        if self.token:
            headers['Authorization'] = 'Bearer ' + self.token
        self.client = httpx.AsyncClient(base_url='https://api.github.com', headers=headers, timeout=30, transport=transport, follow_redirects=False)

    async def close(self):
        await self.client.aclose()

    async def request(self, path, params=None, query=None):
        if self.job_id and db.cancelled(self.job_id):
            raise CollectionError('cancelled', '已请求取消，保留已采集数据')
        if not path.startswith('/') or path.startswith('//') or urlparse(path).netloc:
            raise ValueError('Only GitHub API paths are allowed')
        if self.requests >= 350:
            raise CollectionError('partial', '本轮 GitHub 请求额度达到 350；分页未完成，未推进游标')
        key = db.identity([path, params, query, bool(self.token)])
        with db.connect() as connection:
            cached = connection.execute('SELECT * FROM cache WHERE key=?', (key,)).fetchone()
        headers = {'If-None-Match': cached['etag']} if cached and cached['etag'] and query is None else {}
        for attempt in range(3):
            if self.requests >= 350:
                raise CollectionError('partial', '本轮 GitHub 请求额度达到 350；分页未完成')
            if self.request_hook:
                self.request_hook()
            self.requests += 1
            if self.job_id:
                with db.connect() as connection:
                    connection.execute('UPDATE jobs SET requests=requests+1 WHERE id=?', (self.job_id,))
            try:
                response = await self.client.request('POST' if query else 'GET', path, params=params, json=query, headers=headers)
            except httpx.TransportError:
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)
                    continue
                raise CollectionError('failed', 'GitHub 网络请求失败')
            if response.status_code == 304 and cached:
                payload = json.loads(cached['data'])
                return payload['body'], payload.get('next')
            if response.status_code in {301, 302, 307, 308}:
                target = urlparse(response.headers.get('location', ''))
                if target.scheme == 'https' and target.netloc == 'api.github.com':
                    return await self.request(target.path, params=params, query=query)
                raise CollectionError('failed', 'GitHub 返回不受支持的重定向')
            if response.status_code == 429 or response.status_code == 403 and (
                    response.headers.get('x-ratelimit-remaining') == '0' or 'retry-after' in response.headers or 'rate limit' in response.text.lower()):
                reset = float(response.headers.get('x-ratelimit-reset') or time.time() + 60)
                retry = response.headers.get('retry-after', '60')
                delay = float(retry) if retry.isdigit() else 60
                raise CollectionError('waiting_rate_limit', 'GitHub 限流，等待配额恢复', max(time.time() + delay, reset))
            if response.status_code >= 500 and attempt < 2:
                await asyncio.sleep(2 ** attempt)
                continue
            if response.status_code >= 400:
                status = {401: 'unauthorized', 403: 'forbidden', 404: 'not_found', 410: 'unsupported'}.get(response.status_code, 'failed')
                raise CollectionError(status, f'GitHub HTTP {response.status_code}：{path}')
            body = response.json()
            if isinstance(body, dict) and body.get('errors'):
                errors = body['errors']
                if any(error.get('type') == 'RATE_LIMITED' for error in errors):
                    raise CollectionError('waiting_rate_limit', 'GitHub GraphQL 配额不足', time.time() + 3600)
                raise CollectionError('partial', 'GraphQL 未返回完整数据：' + '; '.join(str(error.get('type', 'query_error')) for error in errors))
            next_url = response.links.get('next', {}).get('url')
            with db.connect() as connection:
                connection.execute('INSERT OR REPLACE INTO cache VALUES(?,?,?,?)',
                                   (key, response.headers.get('etag'), db.encode({'body': body, 'next': next_url}), time.time()))
            return body, next_url
        raise CollectionError('failed', 'GitHub 请求重试耗尽')

    async def pages(self, path, params=None, *, list_key=None, since=None):
        params = {**(params or {}), 'per_page': 100}
        for page in range(1, 101):
            body, following = await self.request(path, {**params, 'page': page})
            items = body.get(list_key, []) if list_key else body
            if not isinstance(items, list):
                raise CollectionError('partial', '分页响应结构不正确')
            for item in items:
                if since and (timestamp(item.get('updated_at')) or float('inf')) < since:
                    return
                yield item
            if not following:
                return
        raise CollectionError('partial', '分页超过安全上限；未推进游标')

    async def graph_connection(self, query, variables, extract):
        cursor = None
        for page in range(100):
            payload, _ = await self.request('/graphql', query={'query': query, 'variables': {**variables, 'cursor': cursor}})
            connection = extract(payload['data'])
            if not connection:
                raise CollectionError('partial', 'GraphQL 数据不可访问')
            for node in connection['nodes']:
                yield node
            if not connection['pageInfo']['hasNextPage']:
                return
            cursor = connection['pageInfo']['endCursor']
        raise CollectionError('partial', 'GraphQL 分页未完成')

    async def discussions(self, repository, since, reconcile):
        if not repository.get('has_discussions'):
            return
        if not self.token:
            raise CollectionError('unauthorized', 'Discussions GraphQL 需要独立 GitHub Token')
        owner, name = repository['full_name'].split('/')
        query = '''query($owner:String!,$name:String!,$cursor:String){repository(owner:$owner,name:$name){discussions(first:50,after:$cursor,orderBy:{field:UPDATED_AT,direction:DESC}){nodes{id number title body url createdAt updatedAt isAnswered answerChosenAt answer{id} author{login}} pageInfo{hasNextPage endCursor}}}}'''
        comments = '''query($id:ID!,$cursor:String){node(id:$id){... on Discussion{comments(first:100,after:$cursor){nodes{id body url createdAt updatedAt author{login}} pageInfo{hasNextPage endCursor}}}}}'''
        replies = '''query($id:ID!,$cursor:String){node(id:$id){... on DiscussionComment{replies(first:100,after:$cursor){nodes{id body url createdAt updatedAt author{login}} pageInfo{hasNextPage endCursor}}}}}'''
        async for discussion in self.graph_connection(query, {'owner': owner, 'name': name}, lambda data: data['repository']['discussions']):
            if not reconcile and (timestamp(discussion['updatedAt']) or 0) < since:
                break
            yield normalize(discussion, 'discussions')
            async for comment in self.graph_connection(comments, {'id': discussion['id']}, lambda data: data['node']['comments']):
                yield normalize(comment, 'discussions', parent=discussion['id'])
                async for reply in self.graph_connection(replies, {'id': comment['id']}, lambda data: data['node']['replies']):
                    yield normalize(reply, 'discussions', parent=comment['id'])

    async def dimension(self, dimension, repository, since, reconcile=False):
        base = '/repos/' + repository['full_name']
        if dimension == 'discussions':
            async for item in self.discussions(repository, since, reconcile):
                yield item
            return
        path, params, key = base + '/' + dimension, {}, None
        if dimension == 'commits':
            params = {'since': iso(since), 'sha': repository['default_branch']}
        elif dimension == 'issues':
            params = {'state': 'all', 'sort': 'updated', 'direction': 'desc', 'since': iso(since)}
        elif dimension == 'issue_comments':
            path, params = base + '/issues/comments', {'since': iso(since), 'sort': 'updated', 'direction': 'desc'}
        elif dimension == 'review_comments':
            path, params = base + '/pulls/comments', {'since': iso(since), 'sort': 'updated', 'direction': 'desc'}
        elif dimension == 'pulls':
            params = {'state': 'all', 'sort': 'updated', 'direction': 'desc'}
        elif dimension == 'security':
            path = base + '/security-advisories'
        async for item in self.pages(path, params, list_key=key, since=since if dimension == 'pulls' and not reconcile else None):
            if dimension == 'pulls' and reconcile and (timestamp(item.get('updated_at')) or 0) < since and item.get('state') != 'open':
                continue
            if dimension == 'issues' and item.get('pull_request'):
                continue
            if dimension == 'pulls':
                item, _ = await self.request(base + f'/pulls/{item["number"]}')
            yield normalize(item, dimension)
            if dimension in {'issues', 'pulls'}:
                number = item['number']
                async for event in self.pages(base + f'/issues/{number}/timeline'):
                    if event.get('event') in {'closed', 'reopened', 'merged', 'cross-referenced', 'connected', 'disconnected'}:
                        yield normalize(event, dimension, entity_id=f'{number}:event:{event.get("id") or db.identity(event)}', parent=str(item['id']))
            if dimension == 'pulls':
                details = item
                async for review in self.pages(base + f'/pulls/{item["number"]}/reviews'):
                    yield normalize(review, dimension, entity_id='review:' + str(review['id']), parent=str(item['id']))
                head = details['head']['sha']
                async for check in self.pages(base + f'/commits/{head}/check-runs', list_key='check_runs'):
                    yield normalize(check, dimension, entity_id='check:' + str(check['id']), parent=str(item['id']))
        if dimension == 'issues' and reconcile:
            async for item in self.pages(base + '/issues', {'state': 'open', 'sort': 'updated', 'direction': 'desc'}):
                if not item.get('pull_request'):
                    yield normalize(item, dimension)


def store_items(repo_id, job_id, dimension, items, baseline, since):
    changed = 0
    now = time.time()
    with db.connect() as connection:
        for item in items.values():
            entity = item['entity_id']
            old = connection.execute('SELECT * FROM objects WHERE repo_id=? AND dimension=? AND entity_id=?', (repo_id, dimension, entity)).fetchone()
            data = item['data']
            evidence_id = db.identity([repo_id, dimension, entity, item['digest']])
            connection.execute('INSERT OR IGNORE INTO evidence VALUES(?,?,?,?,?,?,?)', (evidence_id, repo_id, dimension, entity, item['digest'], db.encode(data), now))
            if not old or old['digest'] != item['digest']:
                occurred = item['occurred_at'] or now
                if not baseline or item['occurred_at'] is not None and occurred >= since:
                    previous = json.loads(old['data']) if old else None
                    transition = {'from': previous.get('state') if previous else None, 'to': data.get('state'), 'event': data.get('event')}
                    change_id = db.identity([job_id, dimension, entity, item['digest']])
                    count = connection.execute('INSERT OR IGNORE INTO changes VALUES(?,?,?,?,?,?,?,?,?,?)',
                        (change_id, repo_id, job_id, dimension, entity, evidence_id, occurred, now, db.encode(previous) if previous else None, db.encode(transition))).rowcount
                    changed += count
            connection.execute('INSERT OR REPLACE INTO objects VALUES(?,?,?,?,?,?)', (repo_id, dimension, entity, item['digest'], db.encode(data), now))
    return changed


async def collect(job):
    with db.connect() as connection:
        repo = dict(connection.execute('SELECT * FROM repositories WHERE id=?', (job['repo_id'],)).fetchone())
        coverage = {row['dimension']: dict(row) for row in connection.execute('SELECT * FROM coverage WHERE repo_id=?', (repo['id'],))}
        previous_metric = connection.execute('SELECT data FROM metrics WHERE repo_id=? ORDER BY observed_at DESC LIMIT 1', (repo['id'],)).fetchone()
    started = time.time()
    baseline = not coverage
    client = GitHub(job['id'])
    changes, statuses = 0, {}
    retry_at = None
    try:
        metadata, _ = await client.request('/repositories/' + str(repo['github_id']) if repo['github_id'] else '/repos/' + repo['full_name'])
        if metadata.get('private'):
            raise CollectionError('forbidden', '首版只支持公开仓库')
        if repo['github_id'] and repo['github_id'] != metadata['id']:
            raise CollectionError('failed', '仓库名称现已属于不同 GitHub ID，请重新添加')
        with db.connect() as connection:
            connection.execute('UPDATE repositories SET github_id=?,full_name=?,metadata=? WHERE id=?', (metadata['id'], metadata['full_name'], db.encode(metadata), repo['id']))
        metrics = {'stars': metadata['stargazers_count'], 'forks': metadata['forks_count'], 'open_items': metadata['open_issues_count']}
        metric_changed = bool(previous_metric and json.loads(previous_metric['data']) != metrics)
        with db.connect() as connection:
            connection.execute('INSERT OR IGNORE INTO metrics VALUES(?,?,?,?)', (job['id'], repo['id'], started, db.encode(metrics)))
        reconcile = json.loads(job['payload']).get('reconcile', False)
        for dimension in DIMENSIONS:
            db.update_job(job['id'], phase='collecting:' + dimension)
            old = coverage.get(dimension)
            since = float(old['cursor']) - 300 if old and old.get('cursor') else started - 7 * 86400
            items = {}
            error, state = None, 'complete'
            try:
                async for item in client.dimension(dimension, metadata, since, reconcile):
                    items[item['entity_id']] = item
                if dimension == 'discussions' and not metadata.get('has_discussions'):
                    state = 'disabled'
            except CollectionError as failure:
                state, error = failure.status, str(failure)
                if failure.retry_at:
                    retry_at = failure.retry_at
            changes += store_items(repo['id'], job['id'], dimension, items, not old or not old.get('cursor'), since)
            cursor = str(started) if state in {'complete', 'disabled'} else old.get('cursor') if old else None
            with db.connect() as connection:
                connection.execute('INSERT OR REPLACE INTO coverage VALUES(?,?,?,?,?,?,?)', (repo['id'], dimension, state, cursor, time.time(), error, job['id']))
            statuses[dimension] = {'status': state, 'count': len(items), 'error': error}
            if state in {'cancelled', 'waiting_rate_limit'}:
                break
        for dimension in set(DIMENSIONS) - statuses.keys():
            statuses[dimension] = {'status': 'not_checked', 'count': 0, 'error': '本轮尚未采集'}
        complete = all(value['status'] in {'complete', 'disabled'} for value in statuses.values())
        status = 'partial' if not complete else 'baseline' if baseline else 'changed' if changes else 'metrics_only' if metric_changed else 'skipped_no_change'
        return {'status': status, 'changes': changes, 'metrics_changed': metric_changed, 'baseline': baseline, 'coverage': statuses, 'retry_at': retry_at, 'requests': client.requests}
    finally:
        await client.close()
