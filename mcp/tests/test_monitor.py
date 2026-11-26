import os
import time
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from browser.base import FakeBrowser
from config import Settings
from platforms.base import ReplyEvent
from store import Store
from monitor import Monitor, MonitorSettings

class Source:
    def __init__(self, platform, events=(), error=None):
        self.platform, self.events, self.error = platform, list(events), error
        self.calls = []
    def fetch(self, space, *, since=None, cursor=None):
        self.calls.append(space.name)
        if self.error:
            raise self.error
        return self.events

def event(platform='boss', message='m1', job='j1', direction='incoming', body='原文'):
    return ReplyEvent(platform, message, job, '公司', '岗位', direction, body, time.time(), 'https://example.com/chat')

class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        env = patch.dict(os.environ, JOB_AGENT_HOME=str(self.home))
        env.start()
        self.addCleanup(env.stop)
        self.store = Store()
        self.addCleanup(self.store.close)
        self.jid = self.store.upsert_job(platform='boss', platform_job_id='j1', company='公司', title='岗位')
        self.store.record_delivery(self.jid, status='sent')
        self.config = Settings.model_validate({'notify': {'enabled': True, 'channels': {
            'tg': {'type': 'telegram', 'enabled': True, 'chat_id': 'fake'}}}})
        self.browser = FakeBrowser()
        self.sender = patch('notify.send_one', return_value={'success': True}).start()
        self.addCleanup(patch.stopall)
    def monitor(self, sources):
        return Monitor(self.browser, sources, self.store, self.config, MonitorSettings(failure_threshold=2))
    def test_dedup_restart_filters_and_platform_key(self):
        other = self.store.upsert_job(platform='boss', platform_job_id='j2')
        self.store.record_delivery(other, status='sent')
        lp = self.store.upsert_job(platform='liepin', platform_job_id='j1')
        self.store.record_delivery(lp, status='sent')
        un = self.store.upsert_job(platform='boss', platform_job_id='unsent')
        self.store.record_delivery(un, status='unverified')
        source = Source('boss', [event(), event(), event(job='j2'), event(message='out', direction='outgoing'),
            event(message='sys', direction='system'), event(message='unknown', job='unknown'), event(message='un', job='unsent')])
        lp_source = Source('liepin', [event(platform='liepin')])
        self.assertEqual(self.monitor([source, lp_source]).poll_once()['sent'], 2)
        self.assertEqual(len(self.store.list_replies()), 2)
        self.assertEqual(self.monitor([source, lp_source]).poll_once()['sent'], 0)
        self.assertEqual(self.sender.call_count, 2)
        self.assertEqual(source.calls, ['job-agent 监听'] * 2)
        self.assertEqual(self.browser.shut, [])
    def test_notification_failure_retries_from_database(self):
        self.sender.return_value = {'success': False}
        self.assertEqual(self.monitor([Source('boss', [event()])]).poll_once()['failed'], 1)
        self.sender.return_value = {'success': True}
        self.assertEqual(self.monitor([]).poll_once()['sent'], 1)
        self.assertEqual(self.monitor([]).poll_once()['sent'], 0)
    def test_failure_isolation_persisted_throttle_and_recovery(self):
        bad = Source('liepin', error=RuntimeError('secret'))
        good = Source('boss', [event()])
        monitor = self.monitor([bad, good])
        monitor.poll_once()
        monitor.poll_once()
        self.assertEqual(self.sender.call_count, 2)
        self.monitor([bad]).poll_once()
        self.assertEqual(self.sender.call_count, 2)
        bad.error = None
        self.monitor([bad]).poll_once()
        bad.error = RuntimeError()
        self.monitor([bad]).poll_once()
        self.monitor([bad]).poll_once()
        self.assertEqual(self.sender.call_count, 4)  # Includes one recovery notice.
        self.assertIn('已恢复', self.sender.call_args_list[2].args[2])
        self.assertNotIn('secret', str(self.sender.call_args_list))
    def test_login_immediate_platform_pause(self):
        class LoginError(RuntimeError):
            code = 'not_logged_in'
        source = Source('boss', error=LoginError())
        monitor = self.monitor([source])
        monitor.poll_once()
        monitor.poll_once()
        self.assertEqual(len(source.calls), 1)
        self.assertEqual(self.sender.call_count, 1)
        self.assertNotIn('boss',self.store.paused_platforms())  # Login is not a risk pause.

    def test_shared_risk_pauses_block_monitor_before_access_even_after_restart(self):
        self.store.pause_platform('liepin')
        self.store.pause_platform('51job')
        lp,job51,good=Source('liepin'),Source('51job'),Source('boss')
        pauses=self.store.paused_platforms()
        for _ in range(2):
            result=self.monitor([lp,job51,good]).poll_once()
            for platform in ('liepin','51job'):
                self.assertEqual(result['platforms'][platform]['status'],'risk_control_paused')
                self.assertEqual(result['platforms'][platform]['fetched'],0)
            self.assertEqual(result['platforms']['boss']['status'],'ok')
            self.assertEqual(result['failed'],0)
        self.assertEqual(lp.calls,[])
        self.assertEqual(job51.calls,[])
        self.assertEqual(len(good.calls),2)
        self.assertEqual(self.store.paused_platforms(),pauses)
        self.sender.assert_not_called()

    def test_all_paused_never_opens_browser_space(self):
        self.store.pause_platform('liepin')
        self.store.pause_platform('51job')
        lp,job51=Source('liepin'),Source('51job')
        with patch.object(self.browser,'space',side_effect=AssertionError('No access for paused platforms')) as access:
            result=self.monitor([lp,job51]).poll_once()
        access.assert_not_called()
        self.assertEqual(lp.calls,[])
        self.assertEqual(job51.calls,[])
        self.assertEqual(result['platforms']['liepin']['status'],'risk_control_paused')

    def test_monitor_risk_signal_persists_shared_pause_without_retry(self):
        class RiskError(RuntimeError):
            code='risk_control'
        source=Source('liepin',error=RiskError('redacted'))
        result=self.monitor([source]).poll_once()
        self.assertEqual(result['platforms']['liepin']['status'],'risk_control_paused')
        self.assertEqual(self.store.paused_platforms()['liepin'],'risk_control')
        self.assertEqual(result['failed'],1)
        source.error=None
        # Opening another worker is not permission to repeat access.
        self.store.close()
        self.store=Store()
        self.addCleanup(self.store.close)
        result=self.monitor([source]).poll_once()
        self.assertEqual(result['platforms']['liepin']['status'],'risk_control_paused')
        self.assertEqual(len(source.calls),1)
        self.assertEqual(self.sender.call_count,1)

    def test_new_pause_during_poll_blocks_next_source(self):
        store=self.store
        class PauseOther(Source):
            def fetch(self,space,**kwargs):
                store.pause_platform('liepin')
                return []
        later=Source('liepin')
        result=self.monitor([PauseOther('boss'),later]).poll_once()
        self.assertEqual(later.calls,[])
        self.assertEqual(result['platforms']['liepin']['status'],'risk_control_paused')

    def test_space_unavailable_stops_and_alerts_once(self):
        self.browser.unavailable.add('job-agent 监听')
        source = Source('boss')
        monitor = self.monitor([source])
        monitor.poll_once()
        self.monitor([source]).poll_once()
        self.assertEqual(source.calls, [])
        self.assertEqual(self.sender.call_count, 1)
        self.assertTrue(monitor.stopped)
    def test_full_body_chunks_and_watch_shared_receipts(self):
        from watch import poll_once
        body = '很长的回复😀' * 1000
        self.monitor([Source('boss', [event(body=body)])]).poll_once()
        self.assertGreater(self.sender.call_count, 1)
        chunks = [call.args[2].split('HR 回复：\n', 1)[1] for call in self.sender.call_args_list]
        self.assertEqual(''.join(chunks), body)
        self.assertEqual(poll_once(self.config, source='replies', store=self.store)['sent'], 0)
    def test_shutdown_on_cli_once_and_keyboard_interrupt(self):
        from cli import main
        from monitor.runtime import run
        with patch('monitor.runtime.create_browser', return_value=self.browser), patch('monitor.runtime.create_sources', return_value=[]):
            self.assertEqual(main(['monitor', '--once']), 0)
        self.assertEqual(self.browser.shut, ['job-agent 监听'])
        with patch('monitor.runtime.create_browser', return_value=self.browser), patch('monitor.runtime.create_sources', return_value=[]), patch('monitor.runtime.time.sleep', side_effect=KeyboardInterrupt):
            self.assertEqual(run(once=False), 0)
        self.assertEqual(len(self.browser.shut), 2)

    def test_config_and_watch_cli_use_owned_replies(self):
        from cli import main
        from monitor.config import load_settings
        import contextlib
        import io
        (self.home/'config.yaml').write_text('engine:\n  monitor:\n    interval_minutes: 12\n    failure_threshold: 4\n    platforms: [boss]\n')
        config, settings = load_settings()
        self.assertEqual(settings.interval_minutes, 12)
        self.assertEqual(settings.failure_threshold, 4)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(['watch', '--source', 'replies', '--once']), 0)
        (self.home/'config.yaml').write_text('engine:\n  monitor:\n    task_space_name: job-agent 投递\n')
        with self.assertRaises(ValueError):
            load_settings()

    def test_disabled_notifications_still_collect(self):
        self.config.notify.enabled = False
        result = self.monitor([Source('boss', [event()])]).poll_once()
        self.assertEqual(result['added'], 1)
        self.sender.assert_not_called()
        self.assertEqual(len(self.store.list_replies()), 1)

    def test_runtime_lock_overlap_never_opens_or_shutdowns_space(self):
        import fcntl
        from monitor.runtime import run
        fd = os.open(self.home/'monitor-run.lock', os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch('monitor.runtime.create_browser') as factory:
                self.assertEqual(run(once=True), 0)
                factory.assert_not_called()
        finally:
            os.close(fd)

    def test_sigterm_shutdown_and_handler_restored(self):
        import signal
        from monitor.runtime import run
        previous = signal.getsignal(signal.SIGTERM)
        def terminate(_):
            signal.raise_signal(signal.SIGTERM)
        with patch('monitor.runtime.create_browser', return_value=self.browser), patch('monitor.runtime.create_sources', return_value=[]), patch('monitor.runtime.time.sleep', side_effect=terminate):
            self.assertEqual(run(), 0)
        self.assertEqual(self.browser.shut, ['job-agent 监听'])
        self.assertEqual(signal.getsignal(signal.SIGTERM), previous)

    def test_missing_adapter_isolated_and_alert_retry(self):
        from monitor.runtime import create_sources
        sources = create_sources(self.browser, MonitorSettings(platforms=['boss']))
        self.sender.return_value = {'success': False}
        monitor = self.monitor(sources)
        monitor.poll_once()
        monitor.poll_once()
        self.assertEqual(self.sender.call_count, 1)
        self.sender.return_value = {'success': True}
        monitor.poll_once()
        monitor.poll_once()
        self.assertEqual(self.sender.call_count, 2)

    def test_database_reopen_and_per_channel_receipts(self):
        second_channel = self.config.notify.channels['tg'].model_copy()
        self.config.notify.channels['second'] = second_channel
        def send(channel, *args):
            return {'success': channel is not second_channel}
        self.sender.side_effect = send
        self.assertEqual(self.monitor([Source('boss', [event()])]).poll_once()['sent'], 1)
        self.store.close()
        self.store = Store()
        self.addCleanup(self.store.close)
        self.sender.side_effect = None
        self.assertEqual(self.monitor([Source('boss', [event()])]).poll_once()['sent'], 1)
        self.assertEqual(self.monitor([]).poll_once()['sent'], 0)
        self.assertEqual(len(self.store.list_replies()), 1)
