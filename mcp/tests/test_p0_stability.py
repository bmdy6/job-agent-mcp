"""Issue #85 regressions; isolated SQLite and loopback notifications only."""
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import get_args
import unittest
from unittest.mock import patch

from job_agent.config import Settings
from job_agent.store import Store


class PlatformValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_platforms_validate_through_mcp(self):
        import job_agent.server as server
        from mcp.server.fastmcp.exceptions import ToolError
        platforms = list(get_args(server.Platform))
        self.assertEqual(len(platforms), 6)
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, JOB_AGENT_HOME=home), \
                patch.object(server.own, 'search_jobs', return_value={'items': [], 'total': 0}) as read:
            for tool in ('search_jobs', 'jd_requirements'):
                await server.mcp.call_tool(tool, {'platforms': platforms})
                self.assertIn('nowcoder', read.call_args.kwargs['platforms'])
                self.assertIn('maimai', read.call_args.kwargs['platforms'])
                for invalid in (platforms + ['boss'], ['unknown']):
                    with self.assertRaises(ToolError):
                        await server.mcp.call_tool(tool, {'platforms': invalid})
            await server.mcp.call_tool('jd_requirements', {'platforms': []})


class StoreStabilityTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name)/'jobs.sqlite3'

    def test_wal_and_connection_pragmas(self):
        with Store(self.path) as store:
            for pragma, expected in [('journal_mode', 'wal'), ('foreign_keys', 1),
                                     ('recursive_triggers', 1), ('busy_timeout', 30000)]:
                self.assertEqual(store.connection.execute('PRAGMA '+pragma).fetchone()[0], expected)

    def test_current_store_initializes_without_transaction_ddl_or_dml(self):
        with Store(self.path) as store:
            jid = store.upsert_job(platform='boss', platform_job_id='1')
            store.connection.execute("INSERT INTO delivery_queue(job_id,status,attempted_at) VALUES(?,'sent',1)", (jid,))
        connect = sqlite3.connect
        statements = []
        def readonly_connection(*args, **kwargs):
            conn = connect(*args, **kwargs)
            conn.set_trace_callback(statements.append)
            def authorize(action, *_):
                return sqlite3.SQLITE_OK if action in {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ,
                    sqlite3.SQLITE_PRAGMA, sqlite3.SQLITE_FUNCTION} else sqlite3.SQLITE_DENY
            conn.set_authorizer(authorize)
            return conn
        with patch('job_agent.store.database.sqlite3.connect', side_effect=readonly_connection), Store(self.path) as store:
            self.assertEqual(store.connection.execute('SELECT count(*) FROM delivery_attempts').fetchone()[0], 0)
        self.assertFalse(any(s.upper().startswith(('BEGIN', 'CREATE', 'INSERT', 'UPDATE')) for s in statements))

    def test_current_store_can_open_while_another_connection_is_writing(self):
        with Store(self.path) as writer:
            with writer.transaction():
                connect = sqlite3.connect
                def short_timeout(*args, **kwargs):
                    return connect(*args, **{**kwargs, 'timeout': 0.05})
                with patch('job_agent.store.database.sqlite3.connect', side_effect=short_timeout), Store(self.path) as reader:
                    self.assertEqual(reader.schema_version, 7)

    def test_old_daily_extension_backfills_attempts_once(self):
        with Store(self.path) as store:
            jid = store.upsert_job(platform='boss', platform_job_id='1')
            store.connection.execute("UPDATE schema_extensions SET version=1 WHERE name='daily'")
            store.connection.execute('DROP TABLE delivery_attempts')
            store.connection.execute('DROP TABLE platform_pauses')
            store.connection.execute("INSERT INTO delivery_queue(job_id,status,attempted_at) VALUES(?,'sent',123)", (jid,))
        for _ in range(2):
            with Store(self.path) as store:
                self.assertEqual([tuple(r) for r in store.connection.execute('SELECT * FROM delivery_attempts')], [(jid, 123)])
                self.assertEqual(store.paused_platforms(), {})
                self.assertEqual(store.connection.execute("SELECT version FROM schema_extensions WHERE name='daily'").fetchone()[0], 2)

    def test_missing_job_is_value_error_and_leaves_no_effect(self):
        with Store(self.path) as store:
            jid = store.upsert_job(platform='boss', platform_job_id='1')
            for ident in (jid, 999):
                with patch.object(store, 'get_job', return_value=None):
                    for status in ('sent', 'pending', 'failed', 'unverified', 'skipped'):
                        with self.assertRaisesRegex(ValueError, 'job.*found|Job.*found'):
                            store.record_delivery(ident, status=status)
                    with self.assertRaisesRegex(ValueError, 'job.*found|Job.*found'):
                        store.add_reply(ident, message_id='1', direction='inbound', body='hello')
            self.assertEqual(store.list_deliveries(), [])
            self.assertEqual(store.list_replies(), [])


class NotificationStabilityTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        env = patch.dict(os.environ, JOB_AGENT_HOME=temp.name, P0_TG_TOKEN='fake-token')
        env.start()
        self.addCleanup(env.stop)
        self.received = []
        received = self.received
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_POST(self):
                received.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                self.send_response(200)
                self.send_header('Content-Length', '2')
                self.end_headers()
                self.wfile.write(b'{}')
        http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(http.server_close)
        self.addCleanup(http.shutdown)
        os.environ['P0_HOOK_URL'] = f'http://127.0.0.1:{http.server_port}/notify'
        self.config = Settings(notify={'enabled': True, 'channels': {
            'hook': {'type': 'webhook', 'enabled': True, 'url_env': 'P0_HOOK_URL'},
            'disabled': {'type': 'webhook', 'enabled': False, 'url_env': 'MISSING_SECRET'},
        }})

    def test_webhook_receives_manual_daily_and_monitor_alert_recovery(self):
        from job_agent.service.deliver import send_summary
        from job_agent.service.daily import send
        from job_agent.monitor import Monitor, MonitorSettings
        from job_agent.browser.base import FakeBrowser
        send_summary(self.config, '手动汇总')
        self.assertEqual(send(self.config, '每日投递汇总', '每日正文'), ['hook'])
        store = Store(); self.addCleanup(store.close)
        monitor = Monitor(FakeBrowser(), [], store, self.config, MonitorSettings(failure_threshold=1))
        monitor.state = {}
        monitor._failure('boss', 'fetch')
        monitor._failure('boss', 'fetch')  # Per-channel alert receipt prevents repeats.
        monitor._success('boss')
        monitor._success('boss')
        self.assertEqual([p['title'] for p in self.received], [
            'job-agent 投递汇总', '每日投递汇总', 'job-agent 监听告警', 'job-agent 监听已恢复'])
        self.assertEqual(self.received[0]['text'], '手动汇总')
        self.assertEqual(monitor.state['boss']['recovery_pending'], [])

    def test_disabled_notifications_send_nothing(self):
        from job_agent.service.deliver import send_summary
        from job_agent.service.daily import send
        from job_agent.monitor import Monitor
        from job_agent.browser.base import FakeBrowser
        self.config.notify.enabled = False
        send_summary(self.config, '手动汇总')
        self.assertEqual(send(self.config, '每日汇总', '正文'), [])
        store = Store(); self.addCleanup(store.close)
        monitor = Monitor(FakeBrowser(), [], store, self.config)
        monitor.state = {}
        monitor._failure('boss', 'login')
        monitor._success('boss')
        self.assertEqual(self.received, [])

    def test_webhook_alert_and_recovery_retry_only_until_delivered(self):
        import job_agent.notify as notify
        from job_agent.monitor import Monitor
        from job_agent.browser.base import FakeBrowser
        store = Store(); self.addCleanup(store.close)
        monitor = Monitor(FakeBrowser(), [], store, self.config)
        monitor.state = {}
        with patch.object(notify.WebhookAdapter, 'send', side_effect=RuntimeError('PRIVATE')), \
                patch.object(notify.time, 'sleep'), self.assertLogs('job_agent.notify', level='WARNING'):
            monitor._failure('boss', 'login')
        self.assertEqual(monitor.state['boss']['notified'], [])
        monitor._failure('boss', 'login')
        monitor._failure('boss', 'login')
        self.assertEqual(len(self.received), 2)
        with patch.object(notify.WebhookAdapter, 'send', side_effect=RuntimeError('PRIVATE')), \
                patch.object(notify.time, 'sleep'), self.assertLogs('job_agent.notify', level='WARNING'):
            monitor._success('boss')
        self.assertEqual(monitor.state['boss']['recovery_pending'], ['hook'])
        monitor._success('boss')
        monitor._success('boss')
        self.assertEqual(len(self.received), 4)
        self.assertEqual(monitor.state['boss']['recovery_pending'], [])

    def test_summary_telegram_sends_one_bounded_message_and_webhook(self):
        import job_agent.notify as notify
        from job_agent.config import TelegramSettings
        from job_agent.service.deliver import send_summary
        self.config.notify.channels = {'tg': TelegramSettings(enabled=True, chat_id='fake', bot_token_env='P0_TG_TOKEN'),
                                      **self.config.notify.channels}
        text = ('岗位😀公司\n' * 1000) + '😀' * 4000
        messages = []
        def telegram_post(url, payload, headers=None):
            messages.append(payload['text'])
            return {'ok': True}
        with patch.object(notify, 'post', side_effect=telegram_post):
            send_summary(self.config, text)
        telegram = messages[:-1]  # The final send is the generic Webhook.
        self.assertEqual(len(telegram), 1)
        self.assertTrue(all(len(msg.encode('utf-16-le'))//2 <= 4096 for msg in telegram))
        self.assertTrue(text.startswith(telegram[0].split('\n\n',1)[1]))

    def test_failed_telegram_is_sanitized_and_webhook_still_receives(self):
        import job_agent.notify as notify
        from job_agent.config import TelegramSettings
        from job_agent.service.deliver import send_summary
        self.config.notify.channels = {'tg': TelegramSettings(enabled=True, chat_id='fake',bot_token_env='P0_TG_TOKEN'),
                                      **self.config.notify.channels}
        with patch.object(notify.TelegramAdapter, 'send', side_effect=RuntimeError('PRIVATE credential-url')), \
                patch.object(notify.time, 'sleep'), self.assertLogs('job_agent.notify', level='WARNING') as logs:
            send_summary(self.config, '正文')
        self.assertNotIn('PRIVATE', str(logs.output))
        self.assertEqual(len(self.received), 1)
