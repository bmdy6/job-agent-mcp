from state_helpers import read_state_file, write_state_file
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from job_agent.config import Settings
from job_agent.monitor.notifications import poll_replies
from job_agent.store import Store


class BackfillTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.env = patch.dict(os.environ, JOB_AGENT_HOME=self.temp.name)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.config = Settings.model_validate({'notify': {'enabled': True, 'channels': {
            'tg': {'type': 'telegram', 'enabled': True, 'chat_id': 'fake'}}}})
        self.store = Store()
        self.addCleanup(self.store.close)
        self.job = self.store.upsert_job(platform='boss', platform_job_id='j1', company='公司', title='岗位')
        self.store.record_delivery(self.job, status='sent')
        self.sender = patch('job_agent.notify.send_one', return_value={'success': True}).start()
        self.addCleanup(patch.stopall)

    def add(self, ident, stamp):
        from datetime import datetime, timezone
        self.store.add_reply(self.job, message_id=ident, direction='inbound', body=ident,
            occurred_at=datetime.fromtimestamp(stamp, timezone.utc).isoformat())

    def test_window_boundary_and_restart_uses_fixed_cutoff(self):
        now = float(int(time.time())) # exact second: datetime stores only microseconds
        self.add('old', now-48*3600-1)
        self.add('boundary', now-48*3600)
        self.add('recent', now-1)
        with patch('job_agent.monitor.notifications.time.time', return_value=now):
            result = poll_replies(self.store, self.config)
        self.assertEqual(result['sent'], 2)
        self.assertEqual(result['suppressed'], 1)
        self.assertEqual(len(self.store.list_replies()), 3)
        self.assertEqual(poll_replies(self.store, self.config)['sent'], 0)
        self.add('late_fetch', now-48*3600+1)
        with patch('job_agent.monitor.notifications.time.time', return_value=now+3*86400):
            self.assertEqual(poll_replies(self.store, self.config)['sent'], 1)
        self.assertNotIn('old', [c.args[2].split('HR 回复：\n')[1] for c in self.sender.call_args_list])

    def test_configurable_window_and_invalid_timestamp_fail_closed(self):
        self.config.engine.monitor.backfill_hours = 1
        self.add('two_hours', time.time()-7200)
        self.store.add_reply(self.job, message_id='invalid', direction='inbound', body='invalid', occurred_at='unknown')
        result = poll_replies(self.store, self.config)
        self.assertEqual(result['sent'], 0)
        self.assertEqual(result['suppressed'], 2)

    def test_failed_send_survives_restart(self):
        self.add('pending', time.time()-1)
        self.sender.return_value = {'success': False}
        self.assertEqual(poll_replies(self.store, self.config)['failed'], 1)
        self.sender.return_value = {'success': True}
        self.assertEqual(poll_replies(self.store, self.config)['sent'], 1)


class ServiceTests(unittest.TestCase):
    def test_mcp_control_ignores_delivery_backend_and_preserves_confirm(self):
        import asyncio
        import job_agent.server as server
        async def check():
            with patch('job_agent.service.monitor.start', return_value={'running': True}) as start, \
                 patch('job_agent.service.monitor.stop', return_value={'stopped': True}) as stop, \
                 patch('job_agent.service.monitor.status', return_value={'running': True, 'last_poll_at': 123}) as status:
                with self.assertRaises(Exception):
                    await server.mcp.call_tool('start_reply_monitor', {'confirm': False})
                start.assert_not_called()
                self.assertTrue((await server.mcp.call_tool('start_reply_monitor', {'confirm': True}))[1]['running'])
                self.assertTrue((await server.mcp.call_tool('stop_reply_monitor', {}))[1]['stopped'])
                self.assertEqual((await server.mcp.call_tool('reply_monitor_status', {}))[1]['last_poll_at'], 123)
                start.assert_called_once()
                stop.assert_called_once()
        asyncio.run(check())

    def test_launchd_start_stop_and_status_are_scoped(self):
        from job_agent.service import monitor
        from subprocess import CompletedProcess
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, JOB_AGENT_HOME=directory), \
             patch('job_agent.service.monitor.plist_path', return_value=Path(directory)/'monitor.plist'), \
             patch('job_agent.service.monitor.subprocess.run') as run:
            self.assertFalse(monitor.status()['running'])
            run.assert_not_called()
            import plistlib
            Path(directory, 'monitor.plist').write_bytes(plistlib.dumps({'EnvironmentVariables': {'JOB_AGENT_HOME': directory}}))
            run.return_value = CompletedProcess([], 0, 'state = running\n pid = 123\n', '')
            self.assertEqual(monitor.status()['pid'], 123)
            monitor.start()
            self.assertFalse(any('kickstart' in c.args[0] for c in run.call_args_list))
            run.reset_mock()
            run.return_value = CompletedProcess([], 0, 'state = exited\n', '')
            monitor.start()
            self.assertIn('kickstart', run.call_args_list[-2].args[0])
            self.assertTrue(monitor.stop()['stopped'])
            self.assertIn('bootout', run.call_args.args[0])


class RuntimeTests(unittest.TestCase):
    def test_space_takeover_exits_cleanly_without_retry_and_records_status(self):
        from job_agent.browser.base import FakeBrowser
        from job_agent.monitor.runtime import run
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, JOB_AGENT_HOME=home):
            browser = FakeBrowser()
            browser.unavailable.add('job-agent 监听')
            with patch('job_agent.monitor.runtime.create_browser', return_value=browser), patch('job_agent.monitor.runtime.create_sources', return_value=[]):
                self.assertEqual(run(), 0)
            state = json.loads(read_state_file(Path(home, 'monitor-runtime.json')))
            self.assertEqual(state['state'], 'space_unavailable')
            self.assertIn('last_poll_at', state)
            self.assertEqual(state['result']['stopped'], True)

    def test_launchd_template_restarts_crashes_but_not_takeovers(self):
        import plistlib
        template = Path(__file__).resolve().parents[2] / 'examples/job-agent-monitor.plist'
        data = plistlib.loads(template.read_bytes())
        self.assertEqual(data['Label'], 'com.bmdy6.job-agent-monitor')
        self.assertEqual(data['KeepAlive'], {'SuccessfulExit': False})
        self.assertGreaterEqual(data['ThrottleInterval'], 60)


class BossLiveRegressionTests(unittest.TestCase):
    def test_sessions_use_stable_identity_and_real_message_times(self):
        from job_agent.browser.base import FakePage, FakeSpace
        from job_agent.platforms.boss.replies import ReplySource
        class Page(FakePage):
            def click(self, selector):
                raise AssertionError('Virtualized numeric row indices must not be clicked')
        p = Page('p2', {'boss.state': {}, 'boss.list': {'rows': [
            {'encryptJobId': 'job', 'uniqueId': 'session', 'lastMsgId': '123'}]},
            'boss.select_session': True,
            'boss.messages': {'current_job_id': 'job', 'current_conversation_id': 'session',
                'rows': [{'id': '123', 'job_id': 'job', 'conversation_id': 'session',
                'time': 1795505482681, 'body': '原文', 'direction': 'incoming'}]}})
        self.assertEqual(ReplySource().fetch(FakeSpace('test', pages=[p]))[0].body, '原文')
        self.assertTrue(p.closed)


class LiepinLiveRegressionTests(unittest.TestCase):
    def test_system_welcome_without_job_mapping_is_not_hr_reply_or_fetch_failure(self):
        from job_agent.platforms.liepin.pages import parse_messages
        data = {'ready': True, 'items': [
            {'msgId': 'welcome', 'msgTime': 1795505482681, 'msg': '系统欢迎', 'direction': '1',
             'msgType': 'txt', 'extType': 201, 'bizData': {}},
            {'msgId': 'intro', 'msgTime': 1795505482682, 'msg': '系统介绍', 'direction': '1',
             'msgType': 'txt', 'extType': 202, 'bizData': {}}]}
        self.assertEqual(parse_messages(data), [])


class ServiceIsolationTests(unittest.TestCase):
    def test_other_data_home_cannot_control_installed_service(self):
        import plistlib
        from job_agent.service import monitor
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, JOB_AGENT_HOME=root+'/tests'), \
             patch('job_agent.service.monitor.plist_path', return_value=Path(root)/'monitor.plist'), \
             patch('job_agent.service.monitor.subprocess.run') as run:
            Path(root, 'monitor.plist').write_bytes(plistlib.dumps({'EnvironmentVariables': {'JOB_AGENT_HOME': root+'/live'}}))
            self.assertFalse(monitor.status()['installed'])
            self.assertFalse(monitor.stop()['stopped'])
            with self.assertRaises(ValueError):
                monitor.start()
            run.assert_not_called()


class SpaceAlertTests(unittest.TestCase):
    def test_driver_and_poller_do_not_duplicate_space_unavailable_alert(self):
        from job_agent.browser.base import SpaceUnavailable
        from job_agent.monitor.runtime import create_browser
        from job_agent.monitor import Monitor
        config = Settings.model_validate({'notify': {'enabled': True, 'channels': {
            'tg': {'type': 'telegram', 'enabled': True, 'chat_id': 'fake'}}}})
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, JOB_AGENT_HOME=home):
            browser = create_browser(config)
            with Store() as store, patch.object(browser, '_call', side_effect=SpaceUnavailable('owned')), \
                 patch('job_agent.notify.send_one', return_value={'success': True}) as send:
                monitor = Monitor(browser, [], store, config)
                monitor.poll_once()
                self.assertTrue(monitor.stopped)
                send.assert_called_once()


class InstallerTests(unittest.TestCase):
    def test_only_transient_absent_service_bootstrap_is_retried(self):
        import importlib.util
        from subprocess import CompletedProcess
        path = Path(__file__).resolve().parents[2] / 'scripts/install-monitor.py'
        spec = importlib.util.spec_from_file_location('monitor_installer', path)
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        with patch.object(installer.subprocess, 'run', side_effect=[
            CompletedProcess([], 5), CompletedProcess([], 113), CompletedProcess([], 0)]) as run, \
             patch.object(installer.time, 'sleep') as sleep:
            installer.bootstrap('gui/501', Path('/fake/monitor.plist'))
            self.assertEqual(run.call_count, 3)
            sleep.assert_called_once_with(1)
        with patch.object(installer.subprocess, 'run', side_effect=[
            CompletedProcess([], 5), CompletedProcess([], 0)]) as run:
            with self.assertRaises(RuntimeError):
                installer.bootstrap('gui/501', Path('/fake/monitor.plist'))
            self.assertEqual(run.call_count, 2)


class ServiceBootstrapTests(unittest.TestCase):
    def test_start_retries_transient_bootstrap_after_stop(self):
        from job_agent.service import monitor
        from subprocess import CompletedProcess
        with patch.object(monitor, 'installed', return_value=True), \
             patch.object(monitor, 'status', side_effect=[{'loaded': False}, {'loaded': False}, {'running': True}]), \
             patch.object(monitor, 'command', side_effect=[CompletedProcess([], 5), CompletedProcess([], 0)]) as command, \
             patch.object(monitor.time, 'sleep') as sleep:
            self.assertTrue(monitor.start()['running'])
            self.assertEqual(command.call_count, 2)
            sleep.assert_called_once_with(1)


class ServiceStopTests(unittest.TestCase):
    def test_stop_waits_until_old_process_has_exited(self):
        from job_agent.service import monitor
        from subprocess import CompletedProcess
        with patch.object(monitor, 'status', return_value={'loaded': True, 'running': True, 'pid': 12345}), \
             patch.object(monitor, 'command', return_value=CompletedProcess([], 0)), \
             patch.object(monitor.os, 'kill', side_effect=[None, ProcessLookupError]) as kill, \
             patch.object(monitor.time, 'sleep') as sleep:
            self.assertEqual(monitor.stop(), {'stopped': True, 'service': monitor.LABEL})
            self.assertEqual(kill.call_count, 2)
            sleep.assert_called_once_with(.2)


class MonitorConfigIsolationTests(unittest.TestCase):
    def test_new_collection_delivery_settings_do_not_block_monitor_or_get_rewritten(self):
        from job_agent.monitor.config import load_settings
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ,JOB_AGENT_HOME=home):
            path=Path(home)/'config.yaml'
            content='''daily: {enabled: true}
collect: {refresh_daily: true}
engine:
  cooldown: {days: 30}
  monitor: {interval_minutes: 7}
browser: {keepalive_seconds: 60}
notify: {enabled: false}
'''
            path.write_text(content)
            config,settings=load_settings()
            self.assertEqual(settings.interval_minutes,7)
            self.assertEqual(config.browser.keepalive_seconds,60)
            self.assertFalse(config.notify.enabled)
            self.assertEqual(path.read_text(),content)
            for content in ('engine: []','notify: {unknown_field: true}', 'engine: {monitor: {unknown_field: true}}',
                            'engine: {task_space_name: "job-agent 监听"}'):
                path.write_text(content)
                with self.assertRaises(ValueError):load_settings()
