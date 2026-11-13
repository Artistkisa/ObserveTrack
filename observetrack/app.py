import hashlib
import hmac
import json
import os
import re
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, InvalidHashError
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import config, db, editor_store
from .github import GitHub, CollectionError
from .reports import export_report, ZONE, window

HASHER = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1)
STATIC = Path(__file__).resolve().parents[1] / 'frontend' / 'dist'


@asynccontextmanager
async def lifespan(app):
    db.initialize()
    password = os.getenv('OBSERVE_ADMIN_PASSWORD', '')
    if password:
        with db.connect() as connection:
            previous = connection.execute("SELECT value FROM settings WHERE key='admin_hash'").fetchone()
            matching = False
            if previous:
                try:
                    matching = HASHER.verify(previous[0], password)
                except (VerifyMismatchError, InvalidHashError):
                    pass
            if not matching:
                connection.execute("INSERT OR REPLACE INTO settings VALUES('admin_hash',?)", (HASHER.hash(password),))
                connection.execute('DELETE FROM sessions')
    yield


app = FastAPI(title='G.H.O.S.T GitHub ObserveTrack', docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)


@app.middleware('http')
async def headers(request, call_next):
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'"
    if request.url.path.startswith('/api/'):
        response.headers['Cache-Control'] = 'no-store'
    return response


def authentication(request: Request):
    raw = request.cookies.get('observe_session', '')
    with db.connect() as connection:
        session = connection.execute('SELECT * FROM sessions WHERE token_hash=? AND expires_at>?', (hashlib.sha256(raw.encode()).hexdigest(), time.time())).fetchone()
    if not raw or not session:
        raise HTTPException(401, '请登录')
    if request.method not in {'GET', 'HEAD'}:
        if not hmac.compare_digest(session['csrf'], request.headers.get('X-CSRF-Token', '')):
            raise HTTPException(403, 'CSRF 校验失败')
        origin = request.headers.get('origin')
        if origin and origin.rstrip('/') != str(request.base_url).rstrip('/'):
            raise HTTPException(403, '请求来源不匹配')
    return dict(session)


async def body(request):
    data = await request.body()
    if len(data) > 32768:
        raise HTTPException(413, '请求过大')
    try:
        value = json.loads(data)
    except (ValueError, TypeError):
        raise HTTPException(400, '需要 JSON 对象')
    if not isinstance(value, dict):
        raise HTTPException(400, '需要 JSON 对象')
    return value


def repository(repo_id):
    with db.connect() as connection:
        row = connection.execute('SELECT * FROM repositories WHERE id=? AND deleted_at IS NULL', (repo_id,)).fetchone()
    if not row:
        raise HTTPException(404, '仓库不存在')
    return dict(row)


@app.get('/health')
def health():
    return {'status': 'ok', 'version': '1.0.0'}


@app.post('/api/login')
async def login(request: Request, response: Response):
    origin = request.headers.get('origin')
    if origin and origin.rstrip('/') != str(request.base_url).rstrip('/'):
        raise HTTPException(403, '请求来源不匹配')
    data = await body(request)
    address = request.client.host if request.client else 'unknown'
    with db.connect() as connection:
        connection.execute('BEGIN IMMEDIATE')
        attempts = connection.execute('SELECT count(*) FROM login_attempts WHERE address=? AND created_at>?', (address, time.time() - 900)).fetchone()[0]
        total = connection.execute('SELECT count(*) FROM login_attempts WHERE created_at>?', (time.time() - 900,)).fetchone()[0]
        if attempts >= 10 or total >= 100:
            raise HTTPException(429, '登录尝试过多，请 15 分钟后重试')
        connection.execute('INSERT INTO login_attempts VALUES(?,?)', (address, time.time()))
        stored = connection.execute("SELECT value FROM settings WHERE key='admin_hash'").fetchone()
    if not stored:
        raise HTTPException(503, '管理员尚未配置')
    password = data.get('password')
    if not isinstance(password, str) or len(password) > 1024:
        raise HTTPException(401, '密码不正确')
    try:
        HASHER.verify(stored[0], password)
    except (VerifyMismatchError, InvalidHashError):
        raise HTTPException(401, '密码不正确')
    token, csrf = secrets.token_urlsafe(40), secrets.token_urlsafe(32)
    with db.connect() as connection:
        connection.execute('INSERT INTO sessions VALUES(?,?,?)', (hashlib.sha256(token.encode()).hexdigest(), csrf, time.time() + 12 * 3600))
        connection.execute('DELETE FROM login_attempts WHERE address=?', (address,))
    response.set_cookie('observe_session', token, max_age=43200, httponly=True, samesite='strict', secure=os.getenv('OBSERVE_SECURE_COOKIE', 'false').lower() == 'true')
    return {'csrf': csrf}


@app.get('/api/session')
def session(auth=Depends(authentication)):
    return {'csrf': auth['csrf'], 'expires_at': auth['expires_at']}


@app.post('/api/logout')
def logout(response: Response, auth=Depends(authentication)):
    with db.connect() as connection:
        connection.execute('DELETE FROM sessions WHERE token_hash=?', (auth['token_hash'],))
    response.delete_cookie('observe_session')
    return {'ok': True}


@app.get('/api/overview')
def overview(auth=Depends(authentication)):
    with db.connect() as connection:
        repos = [dict(row) for row in connection.execute('SELECT * FROM repositories WHERE deleted_at IS NULL ORDER BY created_at')]
        for repo in repos:
            repo['config'] = json.loads(repo['config'])
            metadata = json.loads(repo.pop('metadata'))
            repo['description'] = metadata.get('description')
            repo['language'] = metadata.get('language')
            metric = connection.execute('SELECT data FROM metrics WHERE repo_id=? ORDER BY observed_at DESC LIMIT 1', (repo['id'],)).fetchone()
            repo['metrics'] = json.loads(metric[0]) if metric else {}
            recent = connection.execute('SELECT * FROM jobs WHERE repo_id=? ORDER BY created_at DESC LIMIT 1', (repo['id'],)).fetchone()
            repo['latest_job'] = dict(recent) if recent else None
            report = connection.execute('SELECT id,kind,period,status,version,created_at FROM reports WHERE repo_id=? ORDER BY created_at DESC LIMIT 1', (repo['id'],)).fetchone()
            repo['latest_report'] = dict(report) if report else None
            repo['coverage'] = [dict(row) for row in connection.execute('SELECT * FROM coverage WHERE repo_id=?', (repo['id'],))]
        heartbeat = connection.execute("SELECT value FROM schedule_state WHERE key='heartbeat'").fetchone()
        running = [dict(row) for row in connection.execute("SELECT id,repo_id,kind,status,phase,not_before FROM jobs WHERE status IN ('running','queued','waiting_rate_limit') ORDER BY created_at")]
        for active in running:
            if str(active.get('phase') or '').startswith('v2:'):
                active['visual_active'] = bool(connection.execute("SELECT 1 FROM analysis_steps s JOIN model_calls m ON m.id=s.call_id WHERE m.job_id=? AND s.status='running' LIMIT 1", (active['id'],)).fetchone())
    preferences = db.settings()
    now = datetime.now(ZONE)
    hour, minute = map(int, preferences['daily_time'].split(':'))
    next_daily = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if next_daily <= now:
        next_daily += timedelta(days=1)
    return {'repositories': repos, 'settings': preferences, 'readiness': config.readiness(), 'active': running,
            'worker_online': bool(heartbeat and time.time() - float(heartbeat[0]) < 30), 'next_daily': next_daily.isoformat(), 'server_time': now.isoformat(),
            'analysis_v2': config.analysis_v2_enabled(), 'analysis_v2_available': os.getenv('OBSERVE_ANALYSIS_V2', 'false').lower() == 'true'}


@app.post('/api/repositories')
async def add_repository(request: Request, auth=Depends(authentication)):
    data = await body(request)
    name = str(data.get('full_name', '')).strip().removeprefix('https://github.com/').rstrip('/')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', name):
        raise HTTPException(400, '请输入 owner/repository 或 GitHub 仓库链接')
    with db.connect() as connection:
        count = connection.execute('SELECT count(*) FROM repositories WHERE deleted_at IS NULL').fetchone()[0]
    if count >= 20:
        raise HTTPException(400, '首版最多追踪 20 个仓库')
    client = GitHub()
    try:
        metadata, _ = await client.request('/repos/' + name)
        if metadata.get('private'):
            raise HTTPException(400, '首版仅支持公开仓库')
    except CollectionError as error:
        raise HTTPException(400, str(error))
    finally:
        await client.close()
    with db.connect() as connection:
        connection.execute('BEGIN IMMEDIATE')
        old = connection.execute('SELECT id FROM repositories WHERE github_id=? OR full_name=?', (metadata['id'], metadata['full_name'])).fetchone()
        if old:
            connection.execute('UPDATE repositories SET deleted_at=NULL,enabled=1,full_name=? WHERE id=?', (metadata['full_name'], old['id']))
            repo_id = old['id']
        else:
            cursor = connection.execute('INSERT INTO repositories(github_id,full_name,created_at,metadata) VALUES(?,?,?,?)', (metadata['id'], metadata['full_name'], time.time(), db.encode(metadata)))
            repo_id = cursor.lastrowid
    period = (datetime.now(ZONE).date() - timedelta(days=1)).isoformat()
    job = db.enqueue(repo_id, 'collect', period, dedupe='baseline:' + str(repo_id), payload={'reconcile': True})
    return {'id': repo_id, 'job_id': job['id']}


@app.patch('/api/repositories/{repo_id}')
async def edit_repository(repo_id: int, request: Request, auth=Depends(authentication)):
    repo = repository(repo_id)
    data = await body(request)
    preferences = json.loads(repo['config'])
    for field in ['labels', 'paths']:
        if field in data:
            if not isinstance(data[field], list) or len(data[field]) > 30 or any(not isinstance(value, str) or len(value) > 200 for value in data[field]):
                raise HTTPException(400, '关注项必须是最多 30 个字符串')
            preferences[field] = data[field]
    enabled = data.get('enabled', bool(repo['enabled']))
    if type(enabled) is not bool:
        raise HTTPException(400, 'enabled 必须为布尔值')
    with db.connect() as connection:
        connection.execute('UPDATE repositories SET config=?,enabled=? WHERE id=?', (db.encode(preferences), int(enabled), repo_id))
    return {'ok': True}


@app.delete('/api/repositories/{repo_id}')
def remove_repository(repo_id: int, auth=Depends(authentication)):
    repository(repo_id)
    with db.connect() as connection:
        connection.execute('UPDATE repositories SET enabled=0,deleted_at=? WHERE id=?', (time.time(), repo_id))
        connection.execute("UPDATE jobs SET cancel=1,status=CASE WHEN status='running' THEN status ELSE 'cancelled' END WHERE repo_id=? AND status IN ('running','queued','waiting_rate_limit')", (repo_id,))
    return {'ok': True, 'history_retained': True}


@app.get('/api/repositories/{repo_id}')
def detail(repo_id: int, dimension: str = '', before: str = '', limit: int = 50, auth=Depends(authentication)):
    repo = repository(repo_id)
    limit = min(100, max(1, limit))
    try:
        cursor = before.split('|', 1)
        before_time = float(cursor[0]) if cursor[0] else time.time() + 1
        after_id = cursor[1] if len(cursor) > 1 else '~'
    except ValueError:
        raise HTTPException(400, '分页游标格式不正确')
    with db.connect() as connection:
        rows = connection.execute('''SELECT c.*,e.data FROM changes c LEFT JOIN evidence e ON e.id=c.evidence_id
          WHERE c.repo_id=? AND (?='' OR c.dimension=?) AND (c.observed_at<? OR (c.observed_at=? AND c.id>?))
          ORDER BY c.observed_at DESC,c.id LIMIT ?''',
          (repo_id, dimension, dimension, before_time, before_time, after_id, limit + 1)).fetchall()
        events = [{**dict(row), 'data': json.loads(row['data']) if row['data'] else {'expired': True}, 'transition': json.loads(row['transition'] or '{}')} for row in rows[:limit]]
        reports = [dict(row) for row in connection.execute('SELECT id,kind,period,version,created_at,status FROM reports WHERE repo_id=? ORDER BY created_at DESC LIMIT 100', (repo_id,))]
        metrics = [{**dict(row), 'data': json.loads(row['data'])} for row in connection.execute('SELECT * FROM metrics WHERE repo_id=? ORDER BY observed_at DESC LIMIT 90', (repo_id,))]
        coverage = [dict(row) for row in connection.execute('SELECT * FROM coverage WHERE repo_id=?', (repo_id,))]
    repo['config'] = json.loads(repo['config'])
    repo['metadata'] = json.loads(repo['metadata'])
    return {'repository': repo, 'events': events, 'has_more': len(rows) > limit, 'next_before': f'{events[-1]["observed_at"]}|{events[-1]["id"]}' if events else None,
            'next_id': events[-1]['id'] if events else '', 'reports': reports, 'metrics': list(reversed(metrics)), 'coverage': coverage}


@app.post('/api/repositories/{repo_id}/check')
def check_repository(repo_id: int, auth=Depends(authentication)):
    repo = repository(repo_id)
    if not repo['enabled']:
        raise HTTPException(409, '仓库已暂停，请先恢复')
    with db.connect() as connection:
        active = connection.execute("SELECT id FROM jobs WHERE repo_id=? AND kind='collect' AND status IN ('queued','running','waiting_rate_limit')", (repo_id,)).fetchone()
    if active:
        return {'id': active['id'], 'existing': True}
    period = (datetime.now(ZONE).date() - timedelta(days=1)).isoformat()
    return db.enqueue(repo_id, 'collect', period, dedupe='manual:' + uuid.uuid4().hex, payload={'reconcile': True})


@app.get('/api/jobs')
def jobs(before: float | None = None, auth=Depends(authentication)):
    with db.connect() as connection:
        rows = [dict(row) for row in connection.execute('SELECT j.*,r.full_name FROM jobs j JOIN repositories r ON r.id=j.repo_id WHERE j.created_at<? ORDER BY j.created_at DESC LIMIT 100', (before or time.time() + 1,))]
        for row in rows:
            row['result'] = json.loads(row['result'])
            row['model_calls'] = [dict(call) for call in connection.execute('SELECT * FROM model_calls WHERE job_id=?', (row['id'],))]
            flow_id = json.loads(row['payload']).get('analysis_flow_id')
            flow = editor_store.get(flow_id) if flow_id else editor_store.for_job(row['id'])
            if flow:
                summary = editor_store.summary(flow['id'])
                row['analysis_flow'] = {key: summary[key] for key in ['id', 'status', 'budget', 'remaining', 'usage', 'stages']}
    return rows


@app.post('/api/jobs/{job_id}/{action}')
def job_action(job_id: str, action: str, auth=Depends(authentication)):
    with db.connect() as connection:
        row = connection.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        if not row:
            raise HTTPException(404, '任务不存在')
        job = dict(row)
        if action == 'cancel':
            connection.execute("UPDATE jobs SET cancel=1,status=CASE WHEN status='running' THEN status ELSE 'cancelled' END WHERE id=? AND status IN ('running','queued','waiting_rate_limit')", (job_id,))
            return {'ok': True, 'status': 'stopping' if job['status'] == 'running' else 'cancelled'}
        if action != 'retry' or job['status'] in {'running', 'queued', 'waiting_rate_limit'}:
            raise HTTPException(409, '任务当前不可重试')
    flow = editor_store.get(json.loads(job['payload']).get('analysis_flow_id')) or editor_store.for_job(job_id)
    if flow:
        return continue_analysis(flow['id'], auth)
    return db.enqueue(job['repo_id'], job['kind'], job['period'], dedupe='retry:' + job_id + ':' + uuid.uuid4().hex, payload=json.loads(job['payload']))


def accessible_flow(flow_id):
    flow = editor_store.get(flow_id)
    if not flow:
        raise HTTPException(404, '分析流程不存在或已过期')
    return flow


@app.get('/api/analysis/{flow_id}')
def analysis_detail(flow_id: str, auth=Depends(authentication)):
    accessible_flow(flow_id)
    return editor_store.summary(flow_id, details=False)


@app.get('/api/analysis/{flow_id}/steps/{step_id}')
def analysis_step(flow_id: str, step_id: str, auth=Depends(authentication)):
    accessible_flow(flow_id)
    with db.connect() as connection:
        row = connection.execute('SELECT * FROM analysis_steps WHERE flow_id=? AND id=?', (flow_id, step_id)).fetchone()
    if not row:
        raise HTTPException(404, '阶段不属于该流程或已过期')
    return editor_store.decode(row)


@app.get('/api/analysis/{flow_id}/evidence')
def analysis_evidence(flow_id: str, offset: int = 0, limit: int = 50, auth=Depends(authentication)):
    flow = accessible_flow(flow_id)
    catalog = flow['context']['catalog']
    offset, limit = max(0, offset), min(100, max(1, limit))
    return {'items': catalog[offset:offset + limit], 'total': len(catalog), 'next_offset': offset + limit if offset + limit < len(catalog) else None}


@app.post('/api/analysis/{flow_id}/continue')
def continue_analysis(flow_id: str, auth=Depends(authentication)):
    flow = accessible_flow(flow_id)
    if not config.analysis_v2_enabled(flow['repo_id']):
        raise HTTPException(409, 'V2 已关闭或此仓库未在启用范围内，历史仍可读取')
    try:
        return editor_store.resume(flow_id)
    except ValueError as error:
        raise HTTPException(409, str(error))


@app.get('/api/analysis/{flow_id}/evidence/{evidence_id}')
def analysis_evidence_detail(flow_id: str, evidence_id: str, auth=Depends(authentication)):
    accessible_flow(flow_id)
    with db.connect() as connection:
        row = connection.execute('SELECT data FROM analysis_evidence WHERE flow_id=? AND evidence_id=?', (flow_id, evidence_id)).fetchone()
    if not row:
        raise HTTPException(404, '此证据不属于该分析流程或已过期')
    return json.loads(row['data'])


@app.get('/api/reports')
def report_list(auth=Depends(authentication)):
    with db.connect() as connection:
        return [dict(row) for row in connection.execute('SELECT p.id,p.repo_id,p.kind,p.period,p.version,p.status,p.created_at,r.full_name FROM reports p JOIN repositories r ON r.id=p.repo_id ORDER BY p.created_at DESC LIMIT 200')]


def get_report(report_id):
    with db.connect() as connection:
        row = connection.execute('SELECT * FROM reports WHERE id=?', (report_id,)).fetchone()
    if not row:
        raise HTTPException(404, '报告不存在或已过期')
    return dict(row)


@app.get('/api/reports/{report_id}')
def report_detail(report_id: str, auth=Depends(authentication)):
    report = get_report(report_id)
    report['data'] = json.loads(report['data'])
    return report


@app.post('/api/reports/{report_id}/regenerate')
def regenerate(report_id: str, auth=Depends(authentication)):
    report = get_report(report_id)
    return db.enqueue(report['repo_id'], report['kind'], report['period'], dedupe='regenerate:' + uuid.uuid4().hex)


@app.post('/api/repositories/{repo_id}/reports')
async def new_report(repo_id: int, request: Request, auth=Depends(authentication)):
    repository(repo_id)
    data = await body(request)
    kind, period = data.get('kind', 'daily'), data.get('period')
    if kind not in {'daily', 'weekly'}:
        raise HTTPException(400, '报告类型不正确')
    try:
        start, end = window(period, kind)
    except (ValueError, TypeError):
        raise HTTPException(400, '日期需要 YYYY-MM-DD')
    if start > time.time() or start < time.time() - 365 * 86400:
        raise HTTPException(400, '日期不在保留窗口内')
    if kind == 'weekly' and datetime.fromtimestamp(start, ZONE).weekday() != 0:
        raise HTTPException(400, '周报起始日期必须是周一')
    return db.enqueue(repo_id, kind, period, dedupe='manual-report:' + uuid.uuid4().hex)


@app.get('/api/reports/{report_id}/download/{kind}')
def download(report_id: str, kind: str, auth=Depends(authentication)):
    try:
        content, mime = export_report(get_report(report_id), kind)
    except ValueError:
        raise HTTPException(400, '不支持此下载格式')
    suffix = 'svg' if kind in {'trend', 'transitions', 'relationships'} else kind
    return Response(content, media_type=mime, headers={'Content-Disposition': f'attachment; filename="observetrack-{report_id}-{kind}.{suffix}"'})


@app.get('/api/settings')
def get_settings(auth=Depends(authentication)):
    return {'preferences': db.settings(), 'readiness': config.readiness(), 'model_name': os.getenv('OBSERVE_MODEL_NAME', '')}


@app.patch('/api/settings')
async def edit_settings(request: Request, auth=Depends(authentication)):
    data = await body(request)
    preferences = db.settings()
    for key in ['collect_time', 'daily_time', 'weekly_time']:
        if key in data:
            if not isinstance(data[key], str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', data[key]):
                raise HTTPException(400, '时间格式为 HH:MM')
            preferences[key] = data[key]
    for key, maximum in [('daily_items', 20), ('weekly_items', 40), ('analysis_calls', 4), ('retention_days', 365)]:
        if key in data:
            if type(data[key]) is not int or not 1 <= data[key] <= maximum:
                raise HTTPException(400, f'{key} 必须为 1–{maximum}')
            preferences[key] = data[key]
    for key, minimum, maximum in [('analysis_v2_base_calls', 2, 6), ('analysis_v2_extra_calls', 0, 2), ('analysis_followup_requests', 0, 12)]:
        if key in data:
            if type(data[key]) is not int or not minimum <= data[key] <= maximum:
                raise HTTPException(400, f'{key} 必须为 {minimum}–{maximum}')
            preferences[key] = data[key]
    if 'quiet' in data:
        preferences['quiet'] = bool(data['quiet'])
    if preferences['collect_time'] > preferences['daily_time']:
        raise HTTPException(400, '采集时间不能晚于日报时间')
    with db.connect() as connection:
        connection.execute("INSERT OR REPLACE INTO settings VALUES('preferences',?)", (db.encode(preferences),))
    return preferences


if STATIC.exists():
    app.mount('/assets', StaticFiles(directory=STATIC / 'assets'), name='assets')


@app.get('/')
def index():
    if (STATIC / 'index.html').exists():
        return FileResponse(STATIC / 'index.html')
    return {'app': 'GHOST ObserveTrack', 'frontend': 'Build frontend first'}
