"""MCP stdio + CLI process-restart acceptance against one fake HTTP service."""
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import watch
from test_watch import job

ROOT = Path(__file__).resolve().parents[1]


class NotifyIntegrationTest(unittest.IsolatedAsyncioTestCase):
    async def test_stdio_test_and_watch_restarts_only_get_upstream(self):
        with tempfile.TemporaryDirectory() as home:
            requests = []
            class Handler(BaseHTTPRequestHandler):
                def log_message(self, *_):
                    pass
                def reply(self, value):
                    data = json.dumps(value).encode()
                    self.send_response(200)
                    self.send_header('Content-Length', str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                def do_GET(self):
                    requests.append(('GET', self.path, None))
                    path = urlsplit(self.path).path
                    if path == '/api/jobs':
                        self.reply([job()])
                    elif path == '/api/history':
                        self.reply([{'id': 7, 'job_id': 'fake:1', 'action': 'hr_reply_detected', 'detail': '合成回复'}])
                    else:
                        raise AssertionError(self.path)
                def do_POST(self):
                    data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                    requests.append(('POST', self.path, data))
                    if self.path != '/user-notify':
                        raise AssertionError('Unexpected upstream write: '+self.path)
                    self.reply({})
            http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            worker = threading.Thread(target=http.serve_forever, daemon=True)
            worker.start()
            env = {**os.environ, 'JOB_AGENT_HOME': home, 'BOSSHUNTER_PORT': str(http.server_port),
                   'NOTIFY_TEST_URL': f'http://127.0.0.1:{http.server_port}/user-notify'}
            config = Path(home, 'config.yaml')
            config.write_text('notify:\n  enabled: false\n  channels:\n    hook:\n      type: webhook\n      enabled: true\n      url_env: NOTIFY_TEST_URL\n')
            try:
                params = StdioServerParameters(command=sys.executable, args=[str(ROOT/'server.py')], env=env)
                async with stdio_client(params) as (read, write):
                    async with ClientSession(read, write) as client:
                        await client.initialize()
                        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
                        self.assertIn('notify_test', tools)
                        self.assertFalse(tools['notify_test'].annotations.readOnlyHint)
                        result = await client.call_tool('notify_test', {'channel': 'hook'})
                        self.assertFalse(result.isError, result.content)
                        self.assertTrue(result.structuredContent['success'])
                        invalid = await client.call_tool('notify_test', {'channel': 'missing'})
                        self.assertTrue(invalid.isError)
                self.assertEqual(len(requests), 1)  # Test only sends to the user; no BossHunter reads.
                audit = Path(home, 'activity.jsonl').read_text()
                self.assertNotIn('合成', audit)
                self.assertNotIn('NOTIFY_TEST_URL', audit)
                config.write_text(config.read_text().replace('enabled: false', 'enabled: true', 1))
                for sent in (1, 0):
                    run = subprocess.run([sys.executable, str(ROOT/'cli.py'), 'watch', '--once'], env=env,
                                         capture_output=True, text=True, timeout=15)
                    self.assertEqual(run.returncode, 0, run.stderr)
                    self.assertEqual(json.loads(run.stdout)['sent'], sent)
                posts = [r for r in requests if r[0] == 'POST']
                self.assertEqual(len(posts), 2)  # explicit MCP test + one reply notification
                self.assertTrue(all(path == '/user-notify' for _, path, _ in posts))
                self.assertTrue(all(urlsplit(path).path in {'/api/jobs', '/api/history'}
                                    for method, path, _ in requests if method == 'GET'))
            finally:
                http.shutdown()
                http.server_close()
                worker.join(timeout=2)

    async def test_disabled_cli_exits_without_http(self):
        with tempfile.TemporaryDirectory() as home:
            run = subprocess.run([sys.executable, str(ROOT/'cli.py'), 'watch', '--once'],
                env={**os.environ, 'JOB_AGENT_HOME': home}, capture_output=True, text=True, timeout=10)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(json.loads(run.stdout)['sent'], 0)
            self.assertFalse(Path(home, 'notified.jsonl').exists())


class DeliveryCLIIntegrationTest(unittest.TestCase):
    def test_delivery_cli_restart_dedup_and_only_read_upstream(self):
        import test_delivery_summary as fixtures
        with tempfile.TemporaryDirectory() as home:
            requests = []
            state = {'task': None, 'last_task': fixtures.task()}
            class Handler(BaseHTTPRequestHandler):
                def log_message(self, *_):
                    pass
                def reply(self, value):
                    data = json.dumps(value).encode()
                    self.send_response(200)
                    self.send_header('Content-Length', str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                def do_GET(self):
                    requests.append(('GET', self.path, None))
                    path = urlsplit(self.path).path
                    if path == '/api/workbench':
                        self.reply(state)
                    elif path == '/api/jobs':
                        self.reply([])
                    elif path == '/api/history':
                        self.reply([fixtures.history('ok', 'sent'),
                                    fixtures.history('bad', 'error', '发送失败: attachment_selection_unverified', source_platform='51job'),
                                    fixtures.history('bad2', 'error', '发送失败: confirmed_action_changed', source_platform='liepin')])
                    else:
                        raise AssertionError(self.path)
                def do_POST(self):
                    data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                    requests.append(('POST', self.path, data))
                    if self.path != '/fake-telegram':
                        raise AssertionError('Unexpected upstream write: '+self.path)
                    self.reply({'ok': True})
            http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            worker = threading.Thread(target=http.serve_forever, daemon=True)
            worker.start()
            env = {**os.environ, 'JOB_AGENT_HOME': home, 'BOSSHUNTER_PORT': str(http.server_port),
                   'TG_TEST': 'synthetic-token', 'TZ': 'Asia/Shanghai',
                   'FAKE_TG_URL': f'http://127.0.0.1:{http.server_port}/fake-telegram', 'PYTHONPATH': str(ROOT)}
            Path(home, 'config.yaml').write_text('notify:\n  enabled: true\n  channels:\n    tg:\n      type: telegram\n      enabled: true\n      bot_token_env: TG_TEST\n      chat_id: "123"\n')
            # Exercise the real CLI and Telegram adapter in separate processes;
            # only the HTTP transport is redirected to a local fake user inbox.
            runner = """import os, notify, cli
real_post = notify.post
def fake_post(url, body, headers=None):
    assert url == 'https://api.telegram.org/botsynthetic-token/sendMessage'
    return real_post(os.environ['FAKE_TG_URL'], body, headers)
notify.post = fake_post
raise SystemExit(cli.main(['watch', '--once']))
"""
            try:
                for sent in (1, 0, 0):
                    run = subprocess.run([sys.executable, '-c', runner], env=env, capture_output=True, text=True, timeout=15)
                    self.assertEqual(run.returncode, 0, run.stderr)
                    self.assertEqual(json.loads(run.stdout)['sent'], sent)
                posts = [r for r in requests if r[0] == 'POST']
                self.assertEqual(len(posts), 1)
                text = posts[0][2]['text']
                for phrase in ('成功 1 / 失败 2（共 3）', '附件确认不了', '动作变了', '合成公司ok'):
                    self.assertIn(phrase, text)
                self.assertTrue(all(urlsplit(path).path in {'/api/jobs', '/api/history', '/api/workbench'}
                                    for method, path, _ in requests if method == 'GET'))
                self.assertEqual(Path(home, 'notified.jsonl').stat().st_mode & 0o777, 0o600)
            finally:
                http.shutdown()
                http.server_close()
                worker.join(timeout=2)

    def test_cli_get_allowlist_rejects_mutating_routes(self):
        import cli
        for path in ('/api/workbench/deliver', '/api/workbench/task', '/api/workbench/task/id/stop', '/api/agent/tasks'):
            with self.assertRaises(ValueError):
                cli.boss_get(path, 1)
