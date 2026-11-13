import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager

from . import config


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def identity(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


@contextmanager
def connect():
    config.DATA.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(config.DATA / 'observe.sqlite3', timeout=20)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA foreign_keys=ON')
    connection.execute('PRAGMA busy_timeout=20000')
    try:
        yield connection
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def initialize():
    with connect() as connection:
        connection.execute('PRAGMA journal_mode=WAL')
        connection.executescript('''
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS repositories(id INTEGER PRIMARY KEY AUTOINCREMENT, github_id INTEGER UNIQUE,
          full_name TEXT NOT NULL UNIQUE, enabled INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL,
          config TEXT NOT NULL DEFAULT '{}', metadata TEXT NOT NULL DEFAULT '{}', deleted_at REAL);
        CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, repo_id INTEGER REFERENCES repositories(id),
          kind TEXT NOT NULL, period TEXT NOT NULL, dedupe TEXT NOT NULL UNIQUE, status TEXT NOT NULL,
          created_at REAL NOT NULL, started_at REAL, ended_at REAL, not_before REAL NOT NULL DEFAULT 0,
          cancel INTEGER NOT NULL DEFAULT 0, phase TEXT, error TEXT, payload TEXT NOT NULL DEFAULT '{}',
          result TEXT NOT NULL DEFAULT '{}', requests INTEGER NOT NULL DEFAULT 0);
        CREATE INDEX IF NOT EXISTS jobs_queue ON jobs(status, not_before, created_at);
        CREATE UNIQUE INDEX IF NOT EXISTS repo_running ON jobs(repo_id) WHERE status='running';
        CREATE TABLE IF NOT EXISTS coverage(repo_id INTEGER NOT NULL, dimension TEXT NOT NULL,
          status TEXT NOT NULL, cursor TEXT, checked_at REAL NOT NULL, error TEXT, job_id TEXT,
          PRIMARY KEY(repo_id,dimension));
        CREATE TABLE IF NOT EXISTS objects(repo_id INTEGER NOT NULL, dimension TEXT NOT NULL,
          entity_id TEXT NOT NULL, digest TEXT NOT NULL, data TEXT NOT NULL, last_seen REAL NOT NULL,
          PRIMARY KEY(repo_id,dimension,entity_id));
        CREATE TABLE IF NOT EXISTS evidence(id TEXT PRIMARY KEY, repo_id INTEGER NOT NULL, dimension TEXT NOT NULL,
          entity_id TEXT NOT NULL, digest TEXT NOT NULL, data TEXT NOT NULL, created_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS changes(id TEXT PRIMARY KEY, repo_id INTEGER NOT NULL, job_id TEXT NOT NULL,
          dimension TEXT NOT NULL, entity_id TEXT NOT NULL, evidence_id TEXT NOT NULL,
          occurred_at REAL NOT NULL, observed_at REAL NOT NULL, previous TEXT, transition TEXT);
        CREATE INDEX IF NOT EXISTS change_window ON changes(repo_id,occurred_at);
        CREATE TABLE IF NOT EXISTS metrics(id TEXT PRIMARY KEY, repo_id INTEGER NOT NULL, observed_at REAL NOT NULL, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS cache(key TEXT PRIMARY KEY, etag TEXT, data TEXT NOT NULL, updated_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS reports(id TEXT PRIMARY KEY, repo_id INTEGER NOT NULL, kind TEXT NOT NULL,
          period TEXT NOT NULL, version INTEGER NOT NULL, job_id TEXT NOT NULL UNIQUE,
          created_at REAL NOT NULL, status TEXT NOT NULL, data TEXT NOT NULL, markdown TEXT NOT NULL,
          UNIQUE(repo_id,kind,period,version));
        CREATE TABLE IF NOT EXISTS model_calls(id TEXT PRIMARY KEY, job_id TEXT NOT NULL, role TEXT NOT NULL,
          status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, retries INTEGER NOT NULL DEFAULT 0,
          tokens INTEGER, started_at REAL NOT NULL, ended_at REAL, error TEXT);
        CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY, csrf TEXT NOT NULL, expires_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS login_attempts(address TEXT NOT NULL, created_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS schedule_state(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS analysis_flows(id TEXT PRIMARY KEY, job_id TEXT NOT NULL UNIQUE,
          repo_id INTEGER NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
          budget TEXT NOT NULL, context TEXT NOT NULL, output TEXT NOT NULL DEFAULT '{}', generation INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS analysis_steps(id TEXT PRIMARY KEY, flow_id TEXT NOT NULL REFERENCES analysis_flows(id) ON DELETE CASCADE,
          seq INTEGER NOT NULL, role TEXT NOT NULL, status TEXT NOT NULL, input TEXT NOT NULL, output TEXT NOT NULL DEFAULT '{}',
          call_id TEXT UNIQUE, created_at REAL NOT NULL, dispatched_at REAL, ended_at REAL, error TEXT,
          UNIQUE(flow_id,seq));
        CREATE TABLE IF NOT EXISTS analysis_actions(id TEXT PRIMARY KEY, flow_id TEXT NOT NULL REFERENCES analysis_flows(id) ON DELETE CASCADE,
          editor_step TEXT NOT NULL, signature TEXT NOT NULL, status TEXT NOT NULL, input TEXT NOT NULL,
          output TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL, UNIQUE(flow_id,signature));
        CREATE TABLE IF NOT EXISTS analysis_evidence(flow_id TEXT NOT NULL REFERENCES analysis_flows(id) ON DELETE CASCADE,
          evidence_id TEXT NOT NULL, entity_key TEXT NOT NULL, data TEXT NOT NULL, added_at REAL NOT NULL,
          PRIMARY KEY(flow_id,evidence_id));
        CREATE TABLE IF NOT EXISTS analysis_evidence_usage(step_id TEXT NOT NULL REFERENCES analysis_steps(id) ON DELETE CASCADE,
          evidence_id TEXT NOT NULL, assigned INTEGER NOT NULL DEFAULT 1, submitted INTEGER NOT NULL DEFAULT 0,
          reviewed INTEGER NOT NULL DEFAULT 0, cited INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(step_id,evidence_id));
        ''')


def settings():
    with connect() as connection:
        saved = connection.execute("SELECT value FROM settings WHERE key='preferences'").fetchone()
    return {**config.DEFAULTS, **(json.loads(saved[0]) if saved else {})}


def enqueue(repo_id, kind, period, *, dedupe=None, payload=None, not_before=0):
    key = dedupe or f'{repo_id}:{kind}:{period}'
    with connect() as connection:
        connection.execute('INSERT OR IGNORE INTO jobs(id,repo_id,kind,period,dedupe,status,created_at,payload,not_before) VALUES(?,?,?,?,?,?,?,?,?)',
                           (uuid.uuid4().hex, repo_id, kind, period, key, 'queued', time.time(), encode(payload or {}), not_before))
        return dict(connection.execute('SELECT * FROM jobs WHERE dedupe=?', (key,)).fetchone())


def claim():
    with connect() as connection:
        connection.execute('BEGIN IMMEDIATE')
        row = connection.execute('''SELECT j.* FROM jobs j JOIN repositories r ON r.id=j.repo_id
          WHERE j.status IN ('queued','waiting_rate_limit') AND j.not_before<=? AND j.cancel=0 AND r.enabled=1 AND r.deleted_at IS NULL
          AND NOT EXISTS(SELECT 1 FROM jobs active WHERE active.repo_id=j.repo_id AND active.status='running')
          ORDER BY COALESCE((SELECT MAX(ended_at) FROM jobs done WHERE done.repo_id=j.repo_id),0),j.created_at LIMIT 1''', (time.time(),)).fetchone()
        if not row:
            return None
        connection.execute("UPDATE jobs SET status='running',started_at=?,phase='starting' WHERE id=?", (time.time(), row['id']))
        return dict(row)


def update_job(job_id, **fields):
    allowed = {'status', 'phase', 'error', 'ended_at', 'not_before', 'result'}
    if not fields.keys() <= allowed:
        raise ValueError('Invalid job update')
    with connect() as connection:
        connection.execute('UPDATE jobs SET ' + ','.join(key + '=?' for key in fields) + ' WHERE id=?', (*fields.values(), job_id))


def cancelled(job_id):
    with connect() as connection:
        row = connection.execute('SELECT cancel FROM jobs WHERE id=?', (job_id,)).fetchone()
    return not row or bool(row[0])


def recover():
    with connect() as connection:
        connection.execute("UPDATE jobs SET status='queued',phase='recovered',error='重启后继续只读采集，已保存对象按指纹去重' WHERE status='running' AND kind='collect' AND cancel=0")
        connection.execute("UPDATE jobs SET status='interrupted',error='进程重启：已保存数据，请重试本阶段',ended_at=? WHERE status='running'", (time.time(),))
        connection.execute("UPDATE model_calls SET status='interrupted',ended_at=? WHERE status IN ('running','reserved')", (time.time(),))
        connection.execute("UPDATE analysis_steps SET status='interrupted',ended_at=?,error='进程重启；结果未知，不自动重放' WHERE status IN ('queued','running','waiting_retry')", (time.time(),))
        connection.execute("UPDATE analysis_actions SET status='interrupted' WHERE status='running'")
        connection.execute("UPDATE analysis_flows SET status='interrupted',updated_at=? WHERE status='running'", (time.time(),))


def cleanup():
    cutoff = time.time() - settings()['retention_days'] * 86400
    with connect() as connection:
        for table, field in [('reports', 'created_at'), ('evidence', 'created_at'), ('changes', 'observed_at'), ('metrics', 'observed_at'),
                             ('cache', 'updated_at'), ('objects', 'last_seen'), ('jobs', 'created_at'), ('model_calls', 'started_at')]:
            extra = " AND status NOT IN ('running','queued','waiting_rate_limit')" if table == 'jobs' else ''
            connection.execute(f'DELETE FROM {table} WHERE {field}<?{extra}', (cutoff,))
        connection.execute('DELETE FROM sessions WHERE expires_at<?', (time.time(),))
        connection.execute('DELETE FROM login_attempts WHERE created_at<?', (time.time() - 3600,))
        connection.execute("DELETE FROM analysis_flows WHERE created_at<? AND status NOT IN ('running','queued')", (cutoff,))
