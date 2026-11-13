"""Start an isolated API/worker with empty data; never uses configured tokens."""
import http.cookiejar
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    if (ROOT / '.env').exists():
        raise RuntimeError('Run smoke test in a clean checkout without .env')
    with tempfile.TemporaryDirectory() as data, socket.socket() as allocation:
        allocation.bind(('127.0.0.1', 0))
        port = allocation.getsockname()[1]
        allocation.close()
        environment = {key: value for key, value in os.environ.items() if not key.startswith('OBSERVE_')}
        password = secrets.token_urlsafe(24)
        environment.update(OBSERVE_DATA=data, OBSERVE_ADMIN_PASSWORD=password, OBSERVE_SECURE_COOKIE='false')
        api = worker = None
        with tempfile.TemporaryFile() as logs:
            try:
                api = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'observetrack.app:app', '--host', '127.0.0.1', '--port', str(port)], cwd=ROOT, env=environment, stdout=logs, stderr=logs)
                base = f'http://127.0.0.1:{port}'
                client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
                def request(path, value=None):
                    body = json.dumps(value).encode() if value is not None else None
                    return client.open(urllib.request.Request(base + path, data=body, headers={'Content-Type': 'application/json'}), timeout=3)
                for _ in range(100):
                    if api.poll() is not None:
                        raise RuntimeError('API exited during startup')
                    try:
                        with request('/health') as response:
                            assert json.load(response)['status'] == 'ok'
                        break
                    except (OSError, urllib.error.URLError):
                        time.sleep(0.1)
                else:
                    raise RuntimeError('API startup timed out')
                worker = subprocess.Popen([sys.executable, '-m', 'observetrack.worker'], cwd=ROOT, env=environment, stdout=logs, stderr=logs)
                with request('/') as response:
                    assert b'<html' in response.read().lower(), 'Build frontend before smoke test'
                with request('/api/login', {'password': password}) as response:
                    assert json.load(response)['csrf']
                for _ in range(50):
                    with request('/api/overview') as response:
                        snapshot = json.load(response)
                    if snapshot['worker_online']:
                        break
                    assert worker.poll() is None, 'Worker exited'
                    time.sleep(0.1)
                assert snapshot['worker_online'] and snapshot['repositories'] == []
                assert not snapshot['readiness']['model'] and not snapshot['readiness']['github']
                with request('/api/reports') as response:
                    assert json.load(response) == []
                print('PASS: isolated API, worker heartbeat, built page, login, empty repositories/reports, no external credentials.')
            finally:
                for process in [worker, api]:
                    if process is not None:
                        process.terminate()
                        try:
                            process.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=5)


if __name__ == '__main__':
    main()
