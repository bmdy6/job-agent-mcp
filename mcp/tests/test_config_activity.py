import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server
from config import load_config, job_agent_home


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, {'JOB_AGENT_HOME': self.temp.name}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_defaults_and_yaml(self):
        self.assertEqual(job_agent_home(), Path(self.temp.name).resolve())
        self.assertEqual(load_config().directions, [])
        self.assertEqual(load_config().bosshunter.port, 8686)
        Path(self.temp.name, 'config.yaml').write_text(
            'directions: [设计]\ncities: [杭州]\nplatforms: [liepin]\nbosshunter:\n  port: 9123\nbrowser:\n  name: ego-lite\n  cdp_url: http://127.0.0.1:9339\n')
        cfg = load_config()
        self.assertEqual(cfg.directions, ['设计'])
        self.assertEqual(cfg.browser.cdp_url, 'http://127.0.0.1:9339')
        with patch.dict(os.environ, {'BOSSHUNTER_PORT': '9456'}):
            self.assertEqual(load_config().bosshunter.port, 9456)

    def test_invalid_config(self):
        for content in ['cities: Shanghai', 'platforms: [other]', 'bosshunter: {port: 0}',
                        'browser: {cdp_url: https://remote.example}', 'directions: [""]', 'bogus: true', '[1, 2]']:
            with self.subTest(content=content):
                Path(self.temp.name, 'config.yaml').write_text(content)
                with self.assertRaises(ValueError):
                    load_config()


class AuditTest(unittest.IsolatedAsyncioTestCase):
    async def test_every_call_and_no_payloads(self):
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {'JOB_AGENT_HOME': home, 'JOB_AGENT_CALLER': 'unit'}):
            with patch.object(server, 'api', return_value={'items': [{'id': 'fake', 'jd': 'PRIVATE-JD'}], 'total': 1}):
                await server.mcp.call_tool('search_jobs', {'query': 'PRIVATE-RESUME'})
            for name, args in [('start_scoring', {'job_ids': ['fake']}),
                               ('start_collection', {'platforms': ['boss'], 'confirm': 'PRIVATE-RESUME'}),
                               ('get_job', {'job_id': '../PRIVATE-RESUME'}),
                               ('not_a_tool', {'jd': 'PRIVATE-JD'})]:
                with self.assertRaises(Exception):
                    await server.mcp.call_tool(name, args)
            with patch.object(server, 'api', side_effect=RuntimeError('PRIVATE-JD')):
                with self.assertRaises(Exception):
                    await server.mcp.call_tool('status', {})
            raw = Path(home, 'activity.jsonl').read_text()
            self.assertNotIn('PRIVATE', raw)
            lines = [json.loads(line) for line in raw.splitlines()]
            self.assertEqual(len(lines), 6)
            self.assertTrue(all(row['caller'] == 'unit' and row['time'] for row in lines))
            self.assertEqual(lines[0]['result']['status'], 'ok')
            self.assertTrue(all(row['result']['status'] == 'error' for row in lines[1:]))
            self.assertEqual(Path(home, 'activity.jsonl').stat().st_mode & 0o777, 0o600)

    async def test_unknown_caller_and_cancellation(self):
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {'JOB_AGENT_HOME': home}, clear=True):
            with patch.object(server, 'api', side_effect=asyncio.CancelledError):
                with self.assertRaises(asyncio.CancelledError):
                    await server.mcp.call_tool('status', {})
            line = json.loads(Path(home, 'activity.jsonl').read_text())
            self.assertEqual(line['caller'], 'unknown')
            self.assertEqual(line['result']['status'], 'cancelled')

    async def test_log_unavailable_prevents_tool_execution(self):
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {'JOB_AGENT_HOME': home}):
            Path(home, 'activity.jsonl').mkdir()
            with patch.object(server, 'api') as api:
                with self.assertRaises(OSError):
                    await server.mcp.call_tool('status', {})
                api.assert_not_called()

    async def test_original_read_only_tools_and_jd_dispatch(self):
        state = {'preferences': {'keywords': ['synthetic']}, 'ai': {'configured': False},
                 'task': None, 'last_task': {'id': 'example', 'status': 'completed', 'secret': 'hidden'}}
        detail = {'id': 'example:1', 'title': '合成岗位', 'jd': '任职要求：\n熟悉 SQL。',
                  'company': '合成公司', 'city': '杭州', 'source_platform': 'liepin',
                  'url': 'https://example.invalid/job'}
        def api(path, body=None):
            if path == '/api/agent/state':
                return state
            if path == '/api/jobs/example%3A1':
                return detail
            if path.endswith('/score-trace'):
                return {'trace': 'synthetic'}
            if path == '/api/scoring/runs':
                return []
            if path == '/api/workbench/preflight':
                self.assertEqual(body, {'mode': 'collect', 'options': {'platform_order': ['liepin'], 'auto_score': False}})
                return {'ok': True}
            if path.startswith('/api/jobs/search?'):
                return {'total': 1, 'items': [detail]}
            self.fail(path)
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {'JOB_AGENT_HOME': home}, clear=True):
            with patch.object(server, 'api', side_effect=api):
                for name, arguments in [('status', {}), ('get_job', {'job_id': 'example:1'}),
                                        ('scoring_runs', {}), ('check_collection', {'platforms': ['liepin']}),
                                        ('jd_requirements', {'limit': 1})]:
                    result = await server.mcp.call_tool(name, arguments)
                    self.assertIsInstance(result, tuple)
                    if name == 'get_job':
                        self.assertEqual(result[1]['jd'], detail['jd'])
                    if name == 'status':
                        self.assertIn('job_agent', result[1])
                        self.assertNotIn('secret', result[1]['last_task'])
                    if name == 'jd_requirements':
                        self.assertEqual(result[1]['items'][0]['text'], '熟悉 SQL')
                for args in [{'limit': 0}, {'limit': 101}, {'platforms': ['unknown']}]:
                    with self.assertRaises(Exception):
                        await server.mcp.call_tool('jd_requirements', args)
            self.assertEqual(len(Path(home, 'activity.jsonl').read_text().splitlines()), 8)
