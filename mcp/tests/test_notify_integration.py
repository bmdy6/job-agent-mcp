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
                        raise AssertionError('No job HTTP reads expected')
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
            env = {**os.environ, 'JOB_AGENT_HOME': home,
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
                self.assertEqual(len(requests), 1)  # Test only sends to the user; no platform reads.
                audit = Path(home, 'activity.jsonl').read_text()
                self.assertNotIn('合成', audit)
                self.assertNotIn('NOTIFY_TEST_URL', audit)
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
