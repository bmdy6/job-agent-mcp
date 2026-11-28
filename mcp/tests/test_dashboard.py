from state_helpers import read_state_file, write_state_file
"""Offline dashboard: actual SQLite snapshots, privacy and shared metric contracts."""
import asyncio
from datetime import datetime
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from job_agent.config import load_config
from job_agent.store import Store
from job_agent.store.signals import save_classification
from job_agent.service import read_tools
from job_agent.service.dashboard import collect, generate
from job_agent.dashboard_html import render

NOW = datetime(2026, 11, 26, 12, tzinfo=ZoneInfo('Asia/Shanghai'))


class Document(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.links = []
        self.remote_assets = []
        self.metrics = {}
        self.metric = None
        self.div_depth = 0
        self.metrics_depth = None
        self.heading_in_metrics = False
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'div':
            self.div_depth += 1
            if attrs.get('class') == 'metrics':
                self.metrics_depth = self.div_depth
        if tag == 'h2' and self.metrics_depth is not None:
            self.heading_in_metrics = True
        if 'data-metric' in attrs:
            self.metric = attrs['data-metric']
        for name in ('href', 'src', 'action', 'poster', 'srcset'):
            if name in attrs:
                if tag == 'a' and name == 'href':
                    self.links.append(attrs[name])
                else:
                    self.remote_assets.append(attrs[name])

    def handle_endtag(self, tag):
        if tag == 'div':
            if self.div_depth == self.metrics_depth:
                self.metrics_depth = None
            self.div_depth -= 1
        if tag == 'strong':
            self.metric = None

    def handle_data(self, data):
        if self.metric:
            self.metrics[self.metric] = data


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.env = patch.dict(os.environ, {'JOB_AGENT_HOME': str(self.home)}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        (self.home / 'config.yaml').write_text('engine:\n  backend: own\n  timezone: Asia/Shanghai\n  platforms:\n    boss:\n      daily_limit: 7\n')
        with Store() as store:
            self.a = store.upsert_job(platform='boss', platform_job_id='a', company='甲 公司', title='<工程师>', url='https://example.invalid/job', salary='20-30K', city='上海')
            self.b = store.upsert_job(platform='zhilian', platform_job_id='b', company='乙公司', legacy_status='sent')
            self.old = store.upsert_job(platform='liepin', platform_job_id='old', company='旧公司', legacy_status='sent')
            self.bad = store.upsert_job(platform='51job', platform_job_id='bad', company='失败公司', url='javascript:alert(1)')
            store.record_delivery(self.a, status='sent', greeting='您好', created_at='2026-11-25T16:01:00Z')
            store.record_delivery(self.a, status='sent', greeting='您好', created_at='2026-11-26T01:00:00Z')
            store.record_delivery(self.b, status='sent', kind='legacy_event', created_at='2026-11-25T15:59:00Z')
            store.record_delivery(self.bad, status='unverified', created_at='2026-11-26T00:00:00Z')
            for i in range(2):
                reply = store.add_reply(self.a, message_id=f'in{i}', direction='inbound', body='面试' + '字' * 38 + '私密后缀<script>alert(1)</script>', occurred_at=f'2026-11-26T0{i+2}:00:00Z', chat_url='https://example.invalid/chat')
                save_classification(store, reply, dict(category='interview', confidence=1, source='rule', time_text='明天', form='online'))
            store.add_reply(self.a, message_id='out', direction='outbound', body='OUTBOUND_SECRET')
            store.add_reply(self.b, message_id='system', direction='system', body='SYSTEM_SECRET')
            store.add_reply(self.b, message_id='synthetic', direction='inbound', body='SYNTHETIC_SECRET', id_origin='source_history')
            store.pause_platform('boss', 'risk_control')
            store.connection.execute('INSERT INTO delivery_attempts VALUES(?,?)', (self.a, NOW.timestamp() - 300))
            store.connection.execute('CREATE TABLE operation_rate(platform TEXT,scope TEXT,at REAL)')
            store.connection.execute('INSERT INTO operation_rate VALUES(?,?,?)', ('boss', 'delivery', NOW.timestamp() - 300))
        write_state_file(self.home / 'monitor-runtime.json', json.dumps({'state': 'running', 'last_poll_at': NOW.timestamp(), 'last_success_at': NOW.timestamp(), 'result': {'platforms': {'boss': {'status': 'risk_control_paused', 'reason_code': 'risk_control', 'stage': 'fetch'}, 'zhilian': {'status': 'login_paused', 'reason_code': 'login_required'}}}}))

    def data(self):
        with read_tools.snapshot() as store:
            return collect(store, load_config(), self.home, now=NOW)

    def test_metrics_share_funnel_and_greeting_contract(self):
        data = self.data()
        expected = read_tools.funnel_stats()
        self.assertEqual(data['funnel']['total'], expected['total'])
        self.assertEqual(data['greetings'], expected['greeting_stats'])
        self.assertEqual(data['companies'], 3)
        self.assertEqual(data['today'], 1)
        doc = Document(render(data))
        self.assertEqual(doc.metrics, dict(applied='3', companies='3', today='1', replied='1', interview_invited='1', reply_rate='33.3%'))
        self.assertEqual(data['undated']['applied'], 1)
        self.assertEqual(sum(r['replied'] for r in data['trend']), 1)
        self.assertEqual(len(data['trend']), 14 * 4)
        self.assertEqual(data['platforms'][0]['used'], 1)
        self.assertEqual(data['platforms'][0]['limit'], 7)
        self.assertEqual(data['platforms'][0]['pause'], 'risk_control')
        self.assertEqual(data['platforms'][1]['pause'], 'login_required')
        self.assertEqual(data['heartbeat'], {})

    def test_private_offline_escaped_document_and_readonly_snapshot(self):
        path = self.home / 'jobs.sqlite3'
        before = path.read_bytes()
        with patch('socket.create_connection', side_effect=AssertionError('offline only')):
            output = generate(self.home / 'nested' / 'report.html', now=NOW)
        self.assertEqual(path.read_bytes(), before)
        html = Path(output).read_text()
        doc = Document(html)
        self.assertFalse(doc.heading_in_metrics, 'Content sections must be outside the metric grid')
        self.assertEqual(doc.div_depth, 0, 'HTML divs must be balanced')
        self.assertEqual(doc.remote_assets, [])
        self.assertTrue(set(doc.links) <= {'https://example.invalid/job', 'https://example.invalid/chat'})
        self.assertNotIn('url(', html)
        self.assertNotIn('<script', html)
        for secret in ('私密后缀', 'OUTBOUND_SECRET', 'SYSTEM_SECRET', 'SYNTHETIC_SECRET', 'javascript:'):
            self.assertNotIn(secret, html)
        self.assertIn('&lt;工程师&gt;', html)
        self.assertIn('沟通回执', html)
        self.assertIn('面试邀约', html)
        self.assertIn('样本不足', html)
        self.assertEqual(Path(output).stat().st_mode & 0o777, 0o600)
        with read_tools.snapshot() as store:
            with self.assertRaises(sqlite3.OperationalError):
                store.connection.execute("DELETE FROM jobs")

    def test_output_cannot_overwrite_inputs_missing_db_never_created(self):
        for filename in ('jobs.sqlite3', 'config.yaml', 'monitor-runtime.json'):
            with self.assertRaises(ValueError):
                generate(self.home / filename)
        alias = self.home / 'alias.html'
        alias.hardlink_to(self.home / 'jobs.sqlite3')
        with self.assertRaises(ValueError):
            generate(alias)
        (self.home / 'jobs.sqlite3').unlink()
        with self.assertRaises(sqlite3.OperationalError):
            generate()
        self.assertFalse((self.home / 'jobs.sqlite3').exists())
        self.assertFalse((self.home / 'dashboard.html').exists())

    def test_empty_database_and_missing_runtime(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'JOB_AGENT_HOME': tmp}):
            with Store():
                pass
            output = generate(now=NOW)
            html = Path(output).read_text()
            doc = Document(html)
            self.assertEqual(doc.metrics['applied'], '0')
            self.assertEqual(doc.metrics['reply_rate'], '—')
            self.assertIn('暂无投递记录', html)
            self.assertIn('暂无 HR 回复', html)
            self.assertIn('monitor_missing', html)

    def test_local_events_and_prior_invitation_still_share_funnel_population(self):
        with Store() as store:
            # A later uncertain queue result must not erase an existing invitation.
            store.connection.execute("INSERT INTO delivery_queue(job_id,status) VALUES(?,'unverified')", (self.a,))
            store.record_delivery(self.a, status='unverified', created_at='2026-11-26T04:00:00Z')
            ident = store.upsert_job(platform='boss', platform_job_id='manual', company='甲 公司')
            event = dict(job_id=str(ident), event='manual_applied', time='2026-11-25T16:00:00Z', platform='boss')
            for i in range(2):
                store.connection.execute('INSERT INTO source_history VALUES(?,?,?,?,?)',
                    (f'manual-{i}',ident,'job_agent_application_event',json.dumps(event),event['time']))
        data = self.data()
        self.assertEqual(data['funnel']['total'], read_tools.funnel_stats()['total'])
        self.assertEqual(data['funnel']['total']['applied'], 4)
        self.assertEqual(data['funnel']['total']['interview_invited'], 1)
        self.assertEqual(data['companies'], 3)
        self.assertEqual(data['today'], 2)
        item = next(r for r in data['deliveries'] if r['job']['id'] == str(ident))
        self.assertEqual(item['status'], 'recorded_applied')
        self.assertEqual(sum(r['applied'] for r in data['trend']), 3)

    def test_quota_uses_rate_policy_and_monitor_recovery(self):
        config = load_config()
        config.engine.platforms['boss'].rate.daily_limit = 11
        config.engine.platforms['boss'].rate.timezone = 'UTC'
        with Store() as store:
            # UTC yesterday, although it is today in Shanghai.
            store.connection.execute('INSERT INTO delivery_attempts VALUES(?,?)',
                (self.a, datetime.fromisoformat('2026-11-25T16:00:00+00:00').timestamp()))
            store.connection.execute("INSERT INTO operation_rate VALUES('boss','delivery',?)", (NOW.timestamp()-60,))
        write_state_file(self.home/'monitor-runtime.json', json.dumps({'state':'running','last_poll_at':NOW.timestamp()-3600,
            'result':{'platforms':{'boss':{'status':'ok','stage':'complete','reason_code':'ok'}}}}))
        with read_tools.snapshot() as store:
            data = collect(store, config, self.home, now=NOW)
        self.assertEqual(data['platforms'][0]['limit'], 11)
        self.assertEqual(data['platforms'][0]['used'], 2)
        self.assertEqual(data['platforms'][0]['pause'], 'risk_control')
        self.assertIn('monitor_stale', data['heartbeat'])

    def test_legacy_snapshot_time_is_never_used_as_send_date(self):
        with Store() as store:
            store.record_delivery(self.old, status='sent', kind='legacy_snapshot', created_at=NOW.isoformat())
        data = self.data()
        self.assertEqual(data['today'], 1)
        self.assertEqual(data['undated']['applied'], 1)
        item = next(r for r in data['deliveries'] if r['job']['id'] == str(self.old))
        self.assertIsNone(item['time'])

    def test_schema_mismatch_and_symlink_cannot_damage_inputs(self):
        output = self.home/'alias.html'
        output.symlink_to(self.home/'config.yaml')
        with self.assertRaises(ValueError):
            generate(output)
        with sqlite3.connect(self.home/'jobs.sqlite3') as db:
            db.execute('PRAGMA user_version=999')
        with self.assertRaises(ValueError):
            generate()
        self.assertFalse((self.home/'dashboard.html').exists())

    def test_mcp_stdio_returns_generated_path_without_http(self):
        import sys
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        async def run():
            params = StdioServerParameters(command=sys.executable,
                args=['-m', 'job_agent.server'], env=dict(os.environ))
            async with stdio_client(params) as (reader, writer):
                async with ClientSession(reader, writer) as client:
                    await client.initialize()
                    tool = next(t for t in (await client.list_tools()).tools if t.name == 'dashboard')
                    self.assertFalse(tool.annotations.readOnlyHint)
                    self.assertFalse(tool.annotations.openWorldHint)
                    response = await client.call_tool('dashboard', {'out':str(self.home/'stdio.html')})
                    self.assertFalse(response.isError)
                    return response.structuredContent
        result = asyncio.run(run())
        self.assertTrue(Path(result['path']).is_file())
        self.assertEqual(Path(result['path']).name, 'stdio.html')

    def test_cli_and_mcp_generate_local_file(self):
        import job_agent.cli as cli
        import job_agent.server as server
        with patch('job_agent.cli._setup_logging') as logging_setup, patch('builtins.print') as output:
            self.assertEqual(cli.main(['dashboard', '--out', str(self.home / 'cli.html')]), 0)
            logging_setup.assert_not_called()
        self.assertEqual(json.loads(output.call_args.args[0])['path'], str(self.home / 'cli.html'))
        result = asyncio.run(server.mcp.call_tool('dashboard', {}))[1]
        self.assertEqual(result['path'], str((self.home / 'dashboard.html').resolve()))
        self.assertTrue(Path(result['path']).is_file())
