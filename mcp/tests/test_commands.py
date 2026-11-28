"""Offline owner command contract, using synthetic data only."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from job_agent.config import Settings
from job_agent.store import Store


class CommandsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.env = patch.dict(os.environ, JOB_AGENT_HOME=str(self.home)); self.env.start(); self.addCleanup(self.env.stop)
        self.store = Store(); self.addCleanup(self.store.close)
        self.config = Settings(daily={'enabled': True, 'deliver_enabled': True}, engine={'platforms': {'boss': {'enabled': True, 'greeting': '您好', 'greeting_max_length': 10, 'daily_limit': 2}}})

    def command(self, action):
        from job_agent.service.commands import command
        return command(action, config=self.config)

    def test_modes_persist_and_resume_preserves_risk_pause(self):
        from job_agent.service.automation import allowed
        self.store.connection.execute("INSERT INTO platform_pauses VALUES('boss','risk_control','fixture')")
        for action, mode, daily, monitor in [('pause','paused',False,False), ('only_reply','only_reply',False,True), ('resume','running',True,True)]:
            result = self.command(action)
            self.assertEqual(result['mode'], mode)
            self.assertEqual(allowed('daily'), daily)
            self.assertEqual(allowed('followup'), daily)
            self.assertEqual(allowed('monitor'), monitor)
            self.assertEqual(self.store.paused_platforms(), {'boss':'risk_control'})
        self.assertEqual(self.store.schema_version, 7)
        self.assertFalse((self.home/'config.yaml').exists())

    def test_status_china_day_deduplicates_and_redacts(self):
        from job_agent.service.commands import command
        # UTC 16:00 is the next local day in Asia/Shanghai.
        job = self.store.upsert_job(platform='boss', platform_job_id='fixture', company='测试公司', title='AI', url='https://private.invalid', hr_name='PRIVATE-HR')
        for key, stamp, kind in [('a','2026-11-27T16:00:00Z','attempt'), ('b','2026-11-28T00:00:00Z','attempt'), ('old','2026-11-27T15:59:59Z','attempt')]:
            self.store.record_delivery(job, status='sent', source_key=key, created_at=stamp, kind=kind)
        other = self.store.upsert_job(platform='boss', platform_job_id='imported')
        self.store.record_delivery(other, status='sent', source_key='reconcile', created_at='2026-11-28T01:00:00Z', kind='reconcile')
        for mid in ['1','2']:
            self.store.add_reply(job, message_id=mid, direction='inbound', body='PRIVATE-BODY', occurred_at='2026-11-28T01:00:00Z')
        self.store.add_reply(job, message_id='out', direction='outbound', body='PRIVATE-BODY', occurred_at='2026-11-28T01:00:00Z')
        result = command('status', config=self.config, clock=lambda: 1795824000)  # 2026-11-28 UTC
        self.assertEqual(result['platforms']['boss'], {'sent':1,'replies':1,'interviews':0})
        self.assertNotIn('PRIVATE', json.dumps(result)); self.assertNotIn('https://', json.dumps(result))

    def test_queries_readonly_and_missing_database_does_not_initialize(self):
        before = self.store.connection.total_changes
        self.command('status'); self.command('today')
        self.assertEqual(self.store.connection.total_changes, before)
        with patch.dict(os.environ, JOB_AGENT_HOME=str(self.home/'missing')):
            self.assertIn('未启用', self.command('today')['message'])
            self.command('status')
            self.assertFalse((self.home/'missing').exists())

    def test_today_reuses_planner_and_returns_bounded_ids_only(self):
        job = self.store.upsert_job(platform='boss',platform_job_id='target',url='https://private.invalid',company='测试公司',title='PRIVATE-HR https://private.invalid',hr_name='PRIVATE-HR')
        result = self.command('today')
        self.assertEqual(result['delivery']['job_ids'], [job])
        self.assertIn('测试公司',result['message'])
        self.assertEqual(result['followup']['status'], 'disabled')
        self.assertNotIn('PRIVATE', json.dumps(result)); self.assertNotIn('https://', json.dumps(result))
        self.command('pause')
        self.assertEqual(self.command('today')['delivery']['job_ids'], [])

    def test_cli_all_commands_and_invalid_input(self):
        from job_agent.cli import main
        with patch('job_agent.service.commands.load_config', return_value=self.config):
            for action in ['status','today','pause','resume','only_reply']:
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertEqual(main(['command',action,'--json']),0)
                self.assertEqual(json.loads(out.getvalue())['command'], action)
            out = io.StringIO()
            with contextlib.redirect_stdout(out): main(['command','status'])
            self.assertIn('今日', out.getvalue())
        with self.assertRaises(ValueError): self.command('shell')

    def test_paused_daily_and_monitor_do_not_construct_browser(self):
        self.command('pause')
        from job_agent.service.daily import daily_once
        with patch('job_agent.service.daily.load_config', side_effect=AssertionError('config not needed')):
            self.assertEqual(daily_once()['reason_code'], 'automation_paused')
        from job_agent.monitor.runtime import run
        with patch('job_agent.monitor.runtime.create_browser') as browser:
            self.assertEqual(run(once=True),0)
            browser.assert_not_called()
        from job_agent.collect.runtime import collect_once
        with patch('job_agent.collect.runtime.load_config', side_effect=AssertionError('must skip')):
            self.assertTrue(collect_once()['stopped'])

    def test_only_reply_stops_daily_but_monitor_gate_allows(self):
        self.command('only_reply')
        from job_agent.service.daily import daily_once
        self.assertEqual(daily_once()['reason_code'], 'automation_only_reply')

    def test_resume_reaches_daily_and_pause_is_clean_cli_skip(self):
        from job_agent.service import daily
        from job_agent.cli import main
        self.command('pause')
        with patch('job_agent.service.daily._daily_once') as worker, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(['daily','--once']),0)
            worker.assert_not_called()
        self.command('resume')
        with patch('job_agent.service.daily._daily_once',return_value={'status':'completed'}) as worker:
            self.assertEqual(daily.daily_once(config=self.config)['status'],'completed')
            worker.assert_called_once()

    def test_monitor_running_process_idles_and_resumes_without_restart(self):
        from job_agent.monitor.runtime import run
        self.command('pause')
        fake_browser = Mock()
        fake_monitor = Mock(stopped=False, state={})
        fake_monitor.poll_once.return_value={'platforms':{},'failed':0,'sent':0,'added':0}
        ticks = [0]
        def sleep(_):
            ticks[0] += 1
            if ticks[0] == 1:
                fake_browser.space.assert_not_called()
                self.command('resume')
        def stop():
            return fake_monitor.poll_once.called
        with patch('job_agent.monitor.runtime.load_settings',return_value=(self.config,self.config.engine.monitor)), \
             patch('job_agent.monitor.runtime.create_browser',return_value=fake_browser) as create, \
             patch('job_agent.monitor.runtime.Monitor',return_value=fake_monitor), \
             patch('job_agent.monitor.runtime.time.sleep',side_effect=sleep):
            self.assertEqual(run(should_stop=stop),0)
        create.assert_called_once()
        fake_monitor.poll_once.assert_called_once()
        self.assertGreaterEqual(ticks[0],1)

    def test_paused_monitor_poller_and_confirm_have_no_effects(self):
        from job_agent.monitor.poller import Monitor
        from job_agent.service.deliver import confirm
        self.command('pause')
        browser=Mock()
        monitor=Monitor(browser,[],self.store,self.config)
        self.assertEqual(monitor.poll_once()['reason_code'],'automation_paused')
        browser.space.assert_not_called()
        store=Mock()
        self.assertIn('暂停',confirm(store,self.config,'synthetic',daily=True))
        store.confirm_batch.assert_not_called()
        self.command('only_reply')
        self.assertIn('暂停',confirm(store,self.config,'synthetic',daily=True))
        store.confirm_batch.assert_not_called()

    def test_readonly_queries_do_not_construct_writable_store(self):
        from job_agent.service.commands import command
        with patch('job_agent.service.commands.Store',side_effect=AssertionError('write open')):
            for action in ['status','today']:
                self.assertEqual(command(action,config=self.config)['command'],action)

    def test_interviews_count_by_reply_day_and_safe_pause_reasons(self):
        from job_agent.service.commands import command
        job=self.store.upsert_job(platform='boss',platform_job_id='interview')
        reply=self.store.add_reply(job,message_id='invitation',direction='inbound',body='PRIVATE',occurred_at='2026-11-28T03:00:00Z')
        self.store.connection.execute("INSERT INTO interview_invitations VALUES(?, 'PRIVATE-TIME', 'online', 'pending_confirmation', '2026-11-29T03:00:00Z')",(reply,))
        self.store.connection.execute("INSERT INTO platform_pauses VALUES('boss','PRIVATE-REASON https://private.invalid','fixture')")
        result=command('status',config=self.config,clock=lambda:1795824000)
        self.assertEqual(result['platforms']['boss']['interviews'],1)
        self.assertNotIn('PRIVATE',json.dumps(result))
        self.assertNotIn('https://',json.dumps(result))

    def test_engine_observes_control_before_effect_and_between_jobs(self):
        from job_agent.store import SQLiteDeliveryStore
        from job_agent.engine.queue import DeliveryEngine
        from job_agent.engine.adapters import FakeAdapter
        from job_agent.engine.preflight import FakePreflight
        from job_agent.engine.spaces import FakeTaskSpaces
        from job_agent.engine.models import Job, Status
        from job_agent.config.engine import EngineSettings
        with SQLiteDeliveryStore(self.home/'offline.sqlite3') as queue:
            adapter=FakeAdapter(); spaces=FakeTaskSpaces()
            settings=EngineSettings(platforms={'boss':dict(enabled=True,greeting='您好',greeting_max_length=10,daily_limit=10,interval_seconds=0,attachment_name='resume')})
            engine=DeliveryEngine(queue,settings,{'boss':adapter},FakePreflight(),spaces)
            engine.enqueue([Job('boss',str(i),'https://example.invalid/'+str(i)) for i in range(2)])
            self.command('pause'); engine.run()
            self.assertEqual(adapter.calls,[])
            self.command('resume')
            original=adapter.close
            def pause_after_one():
                original()
                self.command('pause')
            with patch.object(adapter,'close',side_effect=pause_after_one): engine.run()
            self.assertEqual([r.status for r in queue.records()],[Status.SENT,Status.PENDING])
            self.command('resume'); engine.run()
            self.assertEqual([r.status for r in queue.records()],[Status.SENT,Status.SENT])
            self.assertEqual(adapter.calls.count('send_greeting'),2)

    def test_pause_during_collection_does_not_send_summary_and_persists_deferred(self):
        from job_agent.service.daily import daily_once
        from job_agent.store.state import read_run
        self.config.daily.deliver_enabled=False
        def collect():
            self.command('pause')
            return {'platforms':{},'failed':0}
        with patch('job_agent.service.daily.send_summary') as send:
            result=daily_once(config=self.config,collect_fn=collect)
            self.assertEqual(result['reason_code'],'automation_paused')
            send.assert_not_called()
        self.assertEqual(read_run('daily')['status'],'deferred')

    def test_only_reply_runtime_still_polls(self):
        from job_agent.monitor.runtime import run
        self.command('only_reply')
        monitor=Mock(stopped=False,state={})
        monitor.poll_once.return_value={'platforms':{},'failed':0,'added':0,'sent':0}
        with patch('job_agent.monitor.runtime.load_settings',return_value=(self.config,self.config.engine.monitor)), \
             patch('job_agent.monitor.runtime.create_browser',return_value=Mock()), \
             patch('job_agent.monitor.runtime.Monitor',return_value=monitor), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(run(once=True),0)
        monitor.poll_once.assert_called_once()
