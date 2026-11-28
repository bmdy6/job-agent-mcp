from state_helpers import read_state_file, write_state_file
import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import job_agent.server as server
from job_agent.config import load_config, job_agent_home


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
        self.assertEqual(load_config().engine.backend, 'own')
        Path(self.temp.name, 'config.yaml').write_text(
            'directions: [设计]\ncities: [杭州]\nplatforms: [liepin]\nbrowser:\n  name: ego-lite\n  cdp_url: http://127.0.0.1:9339\n')
        cfg = load_config()
        self.assertEqual(cfg.directions, ['设计'])
        self.assertEqual(cfg.browser.cdp_url, 'http://127.0.0.1:9339')

    def test_invalid_config(self):
        for content in ['cities: Shanghai', 'platforms: [other]',
                        'browser: {cdp_url: https://remote.example}', 'directions: [""]', 'bogus: true', '[1, 2]']:
            with self.subTest(content=content):
                Path(self.temp.name, 'config.yaml').write_text(content)
                with self.assertRaises(ValueError):
                    load_config()


class AuditTest(unittest.IsolatedAsyncioTestCase):
    async def test_every_call_and_no_payloads(self):
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {'JOB_AGENT_HOME': home, 'JOB_AGENT_CALLER': 'unit'}):
            with patch.object(server.own, 'search_jobs', return_value={'items': [{'id': 'fake', 'jd': 'PRIVATE-JD'}], 'total': 1}):
                await server.mcp.call_tool('search_jobs', {'query': 'PRIVATE-RESUME'})
            for name, args in [('start_scoring', {'job_ids': ['fake']}),
                               ('start_collection', {'platforms': ['boss'], 'confirm': 'PRIVATE-RESUME'}),
                               ('get_job', {'job_id': '../PRIVATE-RESUME'}),
                               ('not_a_tool', {'jd': 'PRIVATE-JD'})]:
                with self.assertRaises(Exception):
                    await server.mcp.call_tool(name, args)
            with patch.object(server.own, 'status', side_effect=RuntimeError('PRIVATE-JD')):
                with self.assertRaises(Exception):
                    await server.mcp.call_tool('status', {})
            raw = read_state_file(Path(home, 'activity.jsonl'))
            self.assertNotIn('PRIVATE', raw)
            lines = [json.loads(line) for line in raw.splitlines()]
            self.assertEqual(len(lines), 6)
            self.assertTrue(all(row['caller'] == 'unit' and row['time'] for row in lines))
            self.assertEqual(lines[0]['result']['status'], 'ok')
            self.assertTrue(all(row['result']['status'] == 'error' for row in lines[1:]))
            self.assertEqual(Path(home, 'jobs.sqlite3').stat().st_mode & 0o777, 0o600)

    async def test_unknown_caller_and_cancellation(self):
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {'JOB_AGENT_HOME': home}, clear=True):
            with patch.object(server.own, 'status', side_effect=asyncio.CancelledError):
                with self.assertRaises(asyncio.CancelledError):
                    await server.mcp.call_tool('status', {})
            line = json.loads(read_state_file(Path(home, 'activity.jsonl')))
            self.assertEqual(line['caller'], 'unknown')
            self.assertEqual(line['result']['status'], 'cancelled')

    async def test_log_unavailable_prevents_tool_execution(self):
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {'JOB_AGENT_HOME': home}):
            Path(home, 'activity.jsonl').mkdir()
            with patch.object(server.own, 'search_jobs') as api:
                with self.assertRaises(OSError):
                    await server.mcp.call_tool('status', {})
                api.assert_not_called()
