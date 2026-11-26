"""End-to-end protocol acceptance; all external effects hit ephemeral fake HTTP."""
import asyncio
import json
import os
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import delivery
from test_issue6 import job


class DeliveryConcurrencyTest(unittest.TestCase):
    def test_same_token_consumed_at_most_once_across_concurrent_connections(self):
        jobs = [job()]
        calls = []
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {'JOB_AGENT_HOME': home}, clear=True):
            token = delivery.preview(jobs, None, None)['confirm_token']
            def attempt(_):
                try:
                    def api(path, body=None):
                        if body is None:
                            return {}
                        calls.append((path, body))
                    return delivery.send(token, [], lambda: jobs, api)
                except ValueError:
                    return 'rejected'
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(attempt, range(2)))
            self.assertEqual(len(calls), 1)
            self.assertEqual(results.count('rejected'), 1)
            self.assertEqual(Path(home, 'confirmations.sqlite3').stat().st_mode & 0o777, 0o600)
            self.assertNotIn(token.encode(), Path(home, 'confirmations.sqlite3').read_bytes())


class Issue6StdioTest(unittest.IsolatedAsyncioTestCase):
    async def test_new_tools_fake_http_and_persistent_confirmation_across_stdio_restart(self):
        with tempfile.TemporaryDirectory() as home:
            requests = []
            current = job()
            task = None
            class Handler(BaseHTTPRequestHandler):
                def log_message(self, *_):
                    pass
                def reply(self, data, status=200):
                    raw = json.dumps(data, ensure_ascii=False).encode()
                    self.send_response(status)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                def do_GET(self):
                    requests.append(('GET', self.path, None))
                    path = urlsplit(self.path).path
                    if path == '/api/jobs':
                        self.reply([current])
                    elif path == '/api/jobs/search':
                        self.reply({'items': [current], 'total': 1})
                    elif path == '/api/jobs/fake%3A1':
                        self.reply(current)
                    elif path == '/api/agent/state':
                        self.reply({'preferences': {}})
                    elif path == '/api/stats':
                        self.reply({current['status']: 1})
                    elif path == '/api/activity':
                        self.reply([])
                    elif path == '/api/workbench':
                        self.reply({'funnel': {'采集总数': 1, '发送': 0, '回复': 0, '拒绝': 0},
                                    'task': task, 'last_task': task, 'send_quota': {'remaining': 30}})
                    else:
                        self.reply({'error': 'unexpected read'}, 404)
                def do_POST(self):
                    nonlocal task
                    body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                    requests.append(('POST', self.path, body))
                    if self.path == '/api/workbench/deliver':
                        task = {'id': 'fake-deliver', 'mode': 'deliver', 'status': 'running',
                                'progress': {'current': 1, 'total': 1}, 'metrics': {'sent': 1},
                                'logs': ['PRIVATE-FAILURE']}
                        self.reply(task)
                    elif self.path == '/api/agent/tasks':
                        task = {'id': 'fake-monitor', 'mode': 'monitor', 'status': 'running'}
                        self.reply({'task': task})
                    elif self.path in {'/api/workbench/task/fake-deliver/stop', '/api/workbench/task/fake-monitor/stop'}:
                        task = None
                        self.reply({'status': 'stopping'})
                    else:
                        self.reply({'error': 'unexpected write'}, 404)
            http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            worker = threading.Thread(target=http.serve_forever, daemon=True)
            worker.start()
            parameters = StdioServerParameters(command=sys.executable,
                args=[str(Path(__file__).resolve().parents[1]/'server.py')],
                env={**os.environ, 'JOB_AGENT_HOME': home, 'BOSSHUNTER_PORT': str(http.server_port)})
            count = 0
            async def invoke(client, name, args=None, error=False):
                nonlocal count
                result = await client.call_tool(name, args or {})
                count += 1
                self.assertEqual(bool(result.isError), error, result.content)
                return result.structuredContent
            try:
                async with stdio_client(parameters) as (read, write):
                    async with ClientSession(read, write) as client:
                        await client.initialize()
                        tools = {t.name: t for t in (await client.list_tools()).tools}
                        self.assertIn('ONLY after', tools['send_greetings'].description)
                        self.assertFalse(tools['send_greetings'].annotations.readOnlyHint)
                        self.assertTrue(tools['funnel_stats'].annotations.readOnlyHint)
                        await invoke(client, 'send_greetings', error=True)
                        await invoke(client, 'send_greetings', {'confirm_token': 'bad'}, error=True)
                        await invoke(client, 'start_reply_monitor', error=True)
                        token = (await invoke(client, 'preview_greetings'))['confirm_token']
                # Token is durable rather than process-local: restart only our test MCP.
                async with stdio_client(parameters) as (read, write):
                    async with ClientSession(read, write) as client:
                        await client.initialize()
                        await invoke(client, 'send_greetings', {'confirm_token': token})
                        await invoke(client, 'send_greetings', {'confirm_token': token}, error=True)
                        status = await invoke(client, 'delivery_status')
                        self.assertEqual(status['task']['progress']['current'], 1)
                        await invoke(client, 'stop_delivery')
                        # No service installation in this isolated test HOME; fail without legacy HTTP.
                        await invoke(client, 'start_reply_monitor', {'confirm': True}, error=True)
                        await invoke(client, 'stop_reply_monitor')
                        targets = await invoke(client, 'greeting_targets')
                        self.assertEqual(targets['items'][0]['jd'], current['jd'])
                        await invoke(client, 'log_event', {'job_id': current['id'], 'event': 'manual_applied'})
                        await invoke(client, 'log_event', {'job_id': current['id'], 'event': 'interview_done'})
                        stats = await invoke(client, 'funnel_stats', {'group_by': 'city'})
                        self.assertEqual(stats['total']['interview_done'], 1)
                        self.assertEqual(stats['groups'][0]['key'], '杭州')
                        current['status'] = 'replied'
                        brief = await invoke(client, 'briefing')
                        self.assertEqual(brief['todos']['company_briefs']['count'], 1)
                        md = '- 做什么：[官网](https://example.invalid/company)\n- 建议提问：[依据](https://example.invalid/job)'
                        await invoke(client, 'company_save', {'company': current['company'], 'md': md,
                                                             'source_url': 'https://example.invalid/company'})
                        cached = await invoke(client, 'company_get', {'company': current['company']})
                        self.assertEqual(cached['item']['md'], md)
            finally:
                await asyncio.to_thread(http.shutdown)
                http.server_close()
                worker.join()
            posts = [(p, b) for method, p, b in requests if method == 'POST']
            self.assertEqual(posts, [('/api/workbench/deliver', {'job_ids': ['fake:1'], 'direct_send': True}),
                                     ('/api/workbench/task/fake-deliver/stop', {})])
            audit = Path(home, 'activity.jsonl').read_text()
            self.assertEqual(len(audit.splitlines()), count)
            self.assertNotIn(token, audit)
            self.assertNotIn('PRIVATE', audit)
            self.assertNotIn('example.invalid', audit)
