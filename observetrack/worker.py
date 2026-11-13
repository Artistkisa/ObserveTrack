import asyncio
import json
import logging
import os
import signal
import time
from datetime import datetime, timedelta

from . import db
from .github import collect, CollectionError
from .reports import generate, ZONE, window

LOG = logging.getLogger('observetrack.worker')


def schedule(now=None):
    now = now or datetime.now(ZONE)
    preferences = db.settings()
    today = now.date()
    yesterday = (today - timedelta(days=1)).isoformat()
    current = now.strftime('%H:%M')
    with db.connect() as connection:
        repositories = [dict(row) for row in connection.execute('SELECT * FROM repositories WHERE enabled=1 AND deleted_at IS NULL')]
    for repo in repositories:
        if current >= preferences['collect_time']:
            db.enqueue(repo['id'], 'collect', yesterday, payload={'reconcile': now.weekday() == 0, 'scheduled': True})
        if current >= preferences['daily_time']:
            with db.connect() as connection:
                pending = connection.execute("SELECT 1 FROM jobs WHERE repo_id=? AND kind='collect' AND period=? AND status IN ('queued','running','waiting_rate_limit')", (repo['id'], yesterday)).fetchone()
                collection = connection.execute("SELECT id FROM jobs WHERE repo_id=? AND kind='collect' AND period=? AND status='completed' ORDER BY created_at DESC LIMIT 1", (repo['id'], yesterday)).fetchone()
            if collection and not pending:
                db.enqueue(repo['id'], 'daily', yesterday, dedupe='report-for:' + collection['id'])
        monday = today - timedelta(days=now.weekday())
        prior_monday = (monday - timedelta(days=7)).isoformat()
        if now.weekday() > 0 or current >= preferences['weekly_time']:
            if datetime.fromtimestamp(repo['created_at'], ZONE).date() <= monday:
                db.enqueue(repo['id'], 'weekly', prior_monday)
    with db.connect() as connection:
        connection.execute("INSERT OR REPLACE INTO schedule_state VALUES('heartbeat',?)", (str(time.time()),))


async def execute(job):
    try:
        result = await collect(job) if job['kind'] == 'collect' else await generate(job)
        if result.get('retry_at'):
            db.update_job(job['id'], status='waiting_rate_limit', not_before=result['retry_at'], phase='waiting_rate_limit', result=db.encode(result))
            return
        if db.cancelled(job['id']):
            db.update_job(job['id'], status='cancelled', phase='cancelled', ended_at=time.time(), result=db.encode(result))
            return
        db.update_job(job['id'], status='completed', phase='completed', ended_at=time.time(), result=db.encode(result))
        if job['kind'] == 'collect':
            payload = json.loads(job['payload'])
            if not payload.get('scheduled') or datetime.now(ZONE).strftime('%H:%M') >= db.settings()['daily_time']:
                db.enqueue(job['repo_id'], 'daily', job['period'], dedupe='report-for:' + job['id'])
            with db.connect() as connection:
                prior = [dict(row) for row in connection.execute('SELECT DISTINCT kind,period FROM reports WHERE repo_id=?', (job['repo_id'],))]
                dates = [row[0] for row in connection.execute('SELECT occurred_at FROM changes WHERE job_id=?', (job['id'],))]
            for report in prior:
                start, end = window(report['period'], report['kind'])
                if any(start <= observed < end for observed in dates) and (report['kind'] != 'daily' or report['period'] != job['period']):
                    db.enqueue(job['repo_id'], report['kind'], report['period'], dedupe=f'supplement:{job["id"]}:{report["kind"]}:{report["period"]}')
    except CollectionError as failure:
        db.update_job(job['id'], status='waiting_rate_limit' if failure.retry_at else 'cancelled' if failure.status == 'cancelled' else 'failed',
                      phase=failure.status, error=str(failure), not_before=failure.retry_at or 0, ended_at=time.time())
    except asyncio.CancelledError:
        status = 'cancelled' if db.cancelled(job['id']) else 'interrupted'
        db.update_job(job['id'], status=status, phase=status, ended_at=time.time(), error='已停止；原始数据及专家成果保留，分析不自动重放')
        raise
    except Exception as error:
        LOG.exception('Job %s failed', job['id'])
        db.update_job(job['id'], status='failed', phase='failed', ended_at=time.time(), error=type(error).__name__ + ': ' + str(error)[:250])


async def run():
    db.initialize()
    lockfile = open(db.config.DATA / 'worker.lock', 'a+')
    if os.name != 'nt':
        import fcntl
        fcntl.flock(lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
    db.recover()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for name in ['SIGTERM', 'SIGINT']:
        try:
            loop.add_signal_handler(getattr(signal, name), stop.set)
        except NotImplementedError:
            pass
    tasks = set()
    last_cleanup = 0
    try:
        while not stop.is_set():
            schedule()
            if time.time() - last_cleanup > 3600:
                db.cleanup()
                last_cleanup = time.time()
            tasks = {task for task in tasks if not task.done()}
            while len(tasks) < 2:
                job = db.claim()
                if not job:
                    break
                tasks.add(asyncio.create_task(execute(job)))
            try:
                await asyncio.wait_for(stop.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        lockfile.close()


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    asyncio.run(run())
