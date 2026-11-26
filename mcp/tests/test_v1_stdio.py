"""Real stdio protocol and loopback HTTP coverage; synthetic state only."""
import asyncio
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class V1StdioTest(unittest.IsolatedAsyncioTestCase):
    async def test_all_v1_tools_over_stdio_and_fake_http_with_redacted_audit(self):
        with tempfile.TemporaryDirectory() as home:
            requests = []
            resume = Path(home, 'source.md')
            resume.write_text('PRIVATE-RESUME', encoding='utf-8')
            job = {'id': 'fake:1', 'company': 'ACME科技有限公司', 'title': '合成岗位',
                   'source_platform': 'liepin', 'status': 'ready', 'jd': 'PRIVATE-JD',
                   'url': 'https://example.invalid/job'}
            class Handler(BaseHTTPRequestHandler):
                def log_message(self, *_):
                    pass
                def reply(self, body, status=200):
                    encoded = json.dumps(body, ensure_ascii=False).encode('utf-8')
                    self.send_response(status)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(encoded)))
                    self.end_headers()
                    self.wfile.write(encoded)
                def do_GET(self):
                    requests.append(('GET', self.path, None))
                    path = urlsplit(self.path).path
                    if path == '/api/agent/state':
                        self.reply({'preferences': {'cities': [], 'keywords': [], 'salary': {}}})
                    elif path == '/api/jobs/search':
                        self.reply({'items': [job], 'total': 1})
                    elif path == '/api/jobs/fake%3A1':
                        self.reply(job)
                    elif path == '/api/config':
                        self.reply({'profile': {'resume_path': str(resume)}})
                    elif path == '/json/version':
                        self.reply({'Browser': 'Synthetic'})
                    elif path == '/api/agent/evaluations/pending':
                        self.reply({'jobs': [job], 'resume': {'text': 'PRIVATE-RESUME'}, 'extra': {'preserved': True}})
                    else:
                        self.reply({'error': 'unknown route'}, 404)
                def do_POST(self):
                    body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                    requests.append(('POST', self.path, body))
                    if self.path == '/api/agent/evaluations':
                        if not body['evaluations']:
                            self.reply({'error': 'PRIVATE upstream validation'}, 400)
                        else:
                            self.reply({'accepted': body['evaluations'], 'extra': 'preserved'})
                    elif self.path == '/api/jobs/fake%3A1/greeting':
                        self.reply({'success': True, **body})
                    else:
                        self.reply({'error': 'unexpected write'}, 404)
            http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            worker = threading.Thread(target=http.serve_forever, daemon=True)
            worker.start()
            Path(home, 'config.yaml').write_text(
                f'mode: practice\nbrowser:\n  cdp_url: ws://127.0.0.1:{http.server_port}/devtools/browser/synthetic\n')
            parameters = StdioServerParameters(command=sys.executable,
                args=[str(Path(__file__).resolve().parents[1]/'server.py')],
                env={**os.environ, 'BOSSHUNTER_PORT': str(http.server_port), 'JOB_AGENT_HOME': home,
                     'JOB_AGENT_CALLER': 'synthetic-v1-stdio'})
            count = 0
            try:
                async with stdio_client(parameters) as (read, write):
                    async with ClientSession(read, write) as client:
                        await client.initialize()
                        async def call(name, args=None, error=False):
                            nonlocal count
                            result = await client.call_tool(name, args or {})
                            count += 1
                            self.assertEqual(bool(result.isError), error, result.content)
                            return result.structuredContent
                        self.assertEqual((await call('profile'))['mode'], 'practice')
                        self.assertEqual((await call('experience_list'))['items'], [])
                        experience = await call('experience_add', {'title': '素材', 'md': 'PRIVATE-EXPERIENCE'})
                        self.assertEqual((await call('experience_get', {'id': experience['id']}))['item']['md'], 'PRIVATE-EXPERIENCE')
                        self.assertEqual((await call('experience_list'))['items'][0]['id'], experience['id'])
                        self.assertIsNone((await call('company_get', {'company': 'ACME'}))['item'])
                        await call('company_save', {'company': 'ACME', 'md': 'PRIVATE-COMPANY', 'source_url': 'https://example.invalid/source'})
                        self.assertEqual((await call('company_get', {'company': 'ACME有限公司'}))['item']['md'], 'PRIVATE-COMPANY')
                        await call('log_event', {'job_id': 'fake:1', 'event': 'replied', 'note': 'PRIVATE-NOTE', 'chat_url': 'https://example.invalid/chat'})
                        history = await call('company_history', {'company': 'ACME'})
                        self.assertEqual(history['jobs'][0]['chat_url'], 'https://example.invalid/chat')
                        self.assertEqual((await call('funnel'))['total']['greeted'], 1)
                        pending = await call('pending_evaluations', {'limit': 2})
                        self.assertEqual(pending['extra'], {'preserved': True})
                        evaluations = [{'job_id': 'fake:1', 'score': {'raw': {'arbitrary': [1, False]}}, 'greeting': 'PRIVATE-GREETING'}]
                        self.assertEqual((await call('submit_evaluations', {'evaluations': evaluations}))['accepted'], evaluations)
                        await call('submit_evaluations', {'evaluations': []}, error=True)
                        before = len(requests)
                        await call('set_greeting', {'job_id': 'fake:1', 'greeting': 'PRIVATE'}, error=True)
                        await call('set_greeting', {'job_id': 'fake:1', 'greeting': 'PRIVATE', 'confirm': 'true'}, error=True)
                        self.assertEqual(len(requests), before)
                        await call('set_greeting', {'job_id': 'fake:1', 'greeting': 'PRIVATE', 'confirm': True})
                        self.assertTrue((await call('setup_check'))['ok'])
                        self.assertEqual((await call('resume_import', {'path': str(resume)}))['text'], 'PRIVATE-RESUME')
                        self.assertEqual((await call('briefing'))['todos']['pending_confirmation']['count'], 1)
                        self.assertEqual((await call('briefing', {'job_id': 'fake:1'}))['job']['jd'], 'PRIVATE-JD')
            finally:
                await asyncio.to_thread(http.shutdown)
                http.server_close()
                worker.join()
            self.assertTrue(all(method == 'GET' or path in {'/api/agent/evaluations', '/api/jobs/fake%3A1/greeting'}
                                for method, path, body in requests))
            pending_path = next(p for method, p, body in requests if p.startswith('/api/agent/evaluations/pending'))
            self.assertEqual(parse_qs(urlsplit(pending_path).query), {'limit': ['2'], 'include_resume': ['true']})
            greeting = next(body for method, path, body in requests if path.endswith('/greeting'))
            self.assertEqual(greeting, {'greeting': 'PRIVATE', 'confirmed': True})
            raw = Path(home, 'activity.jsonl').read_text()
            self.assertNotIn('PRIVATE', raw)
            self.assertNotIn('example.invalid', raw)
            rows = [json.loads(line) for line in raw.splitlines()]
            self.assertEqual(len(rows), count)
            self.assertEqual(sum(row['result']['status'] == 'error' for row in rows), 3)
            self.assertTrue(all(row['caller'] == 'synthetic-v1-stdio' for row in rows))
