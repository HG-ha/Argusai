#!/usr/bin/env python3
"""Local-only packaged Shell smoke test; never uses real endpoint credentials.

The work directory must not exist. All evidence stays there for inspection.
Run using the build interpreter, with the compiled executable as the first argument.
"""
from pathlib import Path
import os
import subprocess
import json
import time
import threading
import socket
import http.client
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import argparse
parser = argparse.ArgumentParser(description='Exercise a Shell binary against local fixtures and isolated settings.')
parser.add_argument('executable', type=Path)
parser.add_argument('--work-dir', type=Path, required=True)
args = parser.parse_args()
executable = args.executable.resolve()
root = args.work_dir.resolve()
if root.exists():
    raise SystemExit('Use a new, empty --work-dir to avoid touching existing settings')
root.mkdir(parents=True)

class Handler(BaseHTTPRequestHandler):

    def log_message(self, *a):
        pass

    def do_GET(self):
        b = b'<html><title>Local test</title><body>test</body></html>'
        self.send_response(200)
        self.send_header('Content-Type', 'text/html')
        self.end_headers()
        self.wfile.write(b)

    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0'))))
        requests.append(data)
        self.send_response(200)
        if data.get('stream'):
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            for d in [{'choices': [{'index': 0, 'delta': {'role': 'assistant', 'content': 'SHELL_FUNCTIONAL_OK'}, 'finish_reason': None}]}, {'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]}]:
                self.wfile.write(('data: ' + json.dumps(d) + '\n\n').encode())
            self.wfile.write(b'data: [DONE]\n\n')
        else:
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'id': 'local', 'choices': [{'message': {'role': 'assistant', 'content': 'SHELL_FUNCTIONAL_OK'}, 'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15}}).encode())
requests = []
server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
url = f'http://127.0.0.1:{server.server_port}'
rows = []
for edition in ['shell']:
    exe = executable
    env = dict(os.environ, APPDATA=str(root / ('functional-' + edition)), LOCALAPPDATA=str(root / ('local-' + edition)), PYTHONIOENCODING='utf-8', HOME=str(root / 'home'), XDG_CONFIG_HOME=str(root / 'config'), XDG_DATA_HOME=str(root / 'data'))
    cmds = [['version', '--json'], ['ai', 'add', '--name', 'local-fixture', '--base-url', url + '/v1', '--api-key', 'fixture-not-secret', '--model', 'fixture', '--protocol', 'openai', '--activate', '--json'], ['ai', 'list', '--json'], ['chat', 'hello', '--once', '--json', '--timeout', '30'], ['scan', url, '--checks', 'headers', '--out', str(root / ('scan-' + edition)), '--json'], ['cron', 'add', '--name', 'fixture', '--prompt', 'fixture', '--every', '3600', '--disable', '--json'], ['cron', 'list', '--json'], ['team', 'create', 'fixture', '--objective', 'local packaging test', '--json'], ['team', 'list', '--json'], ['reverse', 'create', '--title', 'fixture', '--scope', 'web', '--json'], ['reverse', 'list', '--json'], ['channels', 'list', '--json'], ['--gui']]
    for args in cmds:
        t = time.monotonic()
        try:
            p = subprocess.run([str(exe), *args], env=env, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=65)
            passed = p.returncode == (2 if args == ['--gui'] else 0)
            if args[0] == 'chat':
                passed = passed and 'SHELL_FUNCTIONAL_OK' in p.stdout
            if args[0] == 'scan':
                passed = passed and all((any((root / ('scan-' + edition)).rglob('report.' + suffix)) for suffix in ('html', 'md', 'json')))
            row = dict(edition=edition, args=args, code=p.returncode, passed=passed, seconds=round(time.monotonic() - t, 3), stdout=p.stdout, stderr=p.stderr)
        except Exception as e:
            row = dict(edition=edition, args=args, passed=False, error=str(e))
        rows.append(row)
        print(edition, args[:2], row['passed'], row.get('seconds'), flush=True)
        (root / 'functional-results.json').write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding='utf-8')
# Exercise the compiled proxy without changing system proxy settings or trust stores.
with socket.socket() as listener:
    listener.bind(('127.0.0.1', 0))
    proxy_port = listener.getsockname()[1]
proxy = subprocess.Popen([str(executable), 'proxy', '--port', str(proxy_port), '--json'],
                         env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
try:
    for attempt in range(100):
        if proxy.poll() is not None:
            raise RuntimeError('Proxy exited before listening')
        try:
            with socket.create_connection(('127.0.0.1', proxy_port), timeout=.2):
                break
        except OSError:
            time.sleep(.2)
    connection = http.client.HTTPConnection('127.0.0.1', proxy_port, timeout=10)
    try:
        connection.request('GET', url + '/proxy-fixture')
        response = connection.getresponse()
        body = response.read()
        assert response.status == 200 and b'<title>Local test</title>' in body
    finally:
        connection.close()
    rows.append({'args': ['proxy', 'local-forward'], 'passed': True})
except Exception as exc:
    rows.append({'args': ['proxy', 'local-forward'], 'passed': False, 'error': str(exc)})
finally:
    proxy.terminate()
    try:
        proxy.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        proxy.kill()
        proxy.communicate(timeout=5)
    (root / 'functional-results.json').write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding='utf-8')
server.shutdown()
print('mock_requests', len(requests))
assert all((r['passed'] for r in rows))
