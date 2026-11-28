from state_helpers import write_state_file
import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from job_agent.config import Settings
from job_agent.service.daily import daily_once


class BootResumeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        env = patch.dict(os.environ, JOB_AGENT_HOME=self.tmp.name)
        env.start(); self.addCleanup(env.stop)
        self.cfg = Settings(daily={'enabled': True, 'collect_enabled': False})

    def test_completed_today_skips_all_effects(self):
        report = dict(status='completed', started_at=datetime.now().astimezone().isoformat())
        write_state_file(self.home/'daily-last-run.json', json.dumps(report))
        collect = Mock()
        result = daily_once(config=self.cfg, collect_fn=collect)
        self.assertTrue(result['already_completed'])
        collect.assert_not_called()

    def test_schedule_waits_until_active_hours(self):
        from job_agent.service.daily_schedule import wait_until_active
        clock = [datetime(2026, 11, 27, 8).timestamp()]
        # Explicit local zone for this synthetic clock.
        policies = []
        def sleep(seconds): clock[0] += seconds
        with patch('job_agent.service.daily_schedule.policies', return_value=policies):
            wait_until_active(self.cfg, clock=lambda: clock[0], sleep=sleep)
        self.assertEqual(datetime.fromtimestamp(clock[0]).hour, 9)

    def test_interrupted_report_resumes(self):
        write_state_file(self.home/'daily-last-run.json', json.dumps(dict(
            status='running', started_at=datetime.now().astimezone().isoformat(), current_step='delivery')))
        result = daily_once(config=self.cfg)
        self.assertEqual(result['status'], 'completed')
        self.assertTrue(result['resumed'])

    def test_scheduled_wait_holds_lock(self):
        from job_agent.service.daily import run_lock
        def wait(config):
            with self.assertRaisesRegex(RuntimeError, 'already running'):
                with run_lock(self.home): pass
        with patch('job_agent.service.daily.wait_until_active', side_effect=wait) as waiting:
            daily_once(config=self.cfg, scheduled=True)
        waiting.assert_called_once()

    def test_resume_only_selects_unattempted_queue(self):
        from job_agent.store import SQLiteDeliveryStore
        self.cfg.daily.deliver_enabled = True
        self.cfg.engine.dry_run = False
        self.cfg.engine.live_send_enabled = True
        p = self.cfg.engine.platforms['boss']
        p.greeting_max_length = 10
        p.greeting = '你好'
        p.daily_limit = 10
        p.enabled = True
        with SQLiteDeliveryStore(self.home/'jobs.sqlite3', dry_run=False) as db:
            for key, status in [('sent', 'sent'), ('unknown', 'unverified'), ('fresh', 'pending')]:
                jid = db.upsert_job(platform='boss',platform_job_id=key,url='https://example.test/'+key,company=key,title=key)
                db.connection.execute('INSERT INTO delivery_queue(job_id,status) VALUES(?,?)',(jid,status))
                if status != 'pending': db.record_delivery(jid,status=status)
        write_state_file(self.home/'daily-last-run.json', json.dumps(dict(status='running',started_at=datetime.now().astimezone().isoformat())))
        seen=[]
        def confirm(db,cfg,token,**kwargs):
            seen.extend(j.job_id for j in db.batch_jobs(token))
        result=daily_once(config=self.cfg,confirm_fn=confirm)
        self.assertEqual(seen,['fresh'])
        self.assertEqual(result['status'],'deferred')


class EgoStartupTest(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        env=patch.dict(os.environ,JOB_AGENT_HOME=tmp.name)
        env.start(); self.addCleanup(env.stop)

    def browser(self):
        from job_agent.browser.ego import EgoBrowser
        return EgoBrowser(keepalive_seconds=0, startup_timeout_s=.05, on_event=Mock())

    def test_launch_then_readiness_then_resolve(self):
        import subprocess
        from job_agent.browser.ego import EgoConnectionError
        browser=self.browser()
        with patch.object(browser,'_call',side_effect=[EgoConnectionError('offline'),dict(space_id=1,anchor='a')]), \
             patch.object(browser,'_execute',return_value=True), \
             patch('job_agent.browser.ego.subprocess.run',return_value=subprocess.CompletedProcess([],0)) as launch:
            self.assertEqual(browser.space('test').space_id,1)
        self.assertEqual(launch.call_args.args[0],['open','-g','-a','ego lite'])

    def test_launch_failure_alerts_and_cools_down(self):
        import subprocess
        from job_agent.browser.ego import EgoConnectionError,EgoStartupError
        browser=self.browser()
        with patch.object(browser,'_call',side_effect=EgoConnectionError('offline')), \
             patch('job_agent.browser.ego.subprocess.run',return_value=subprocess.CompletedProcess([],1)) as launch:
            for _ in range(2):
                with self.assertRaises(EgoStartupError): browser.space('test')
        self.assertEqual(launch.call_count,1)
        browser.on_event.assert_called_once_with('test','ego_launch_failed')

    def test_readiness_timeout_has_reason_and_alert(self):
        import subprocess
        from job_agent.browser.ego import EgoConnectionError,EgoStartupError
        browser=self.browser()
        with patch.object(browser,'_call',side_effect=EgoConnectionError('offline')), \
             patch.object(browser,'_execute',side_effect=EgoConnectionError('offline')), \
             patch('job_agent.browser.ego.subprocess.run',return_value=subprocess.CompletedProcess([],0)):
            with self.assertRaisesRegex(EgoStartupError,'ego_ready_timeout'): browser.space('test')
        browser.on_event.assert_called_once_with('test','ego_ready_timeout')

    def test_ownership_failure_never_launches(self):
        from job_agent.browser.base import SpaceUnavailable
        browser=self.browser()
        with patch.object(browser,'_call',side_effect=SpaceUnavailable('owned by user')), \
             patch('job_agent.browser.ego.subprocess.run') as launch:
            with self.assertRaises(SpaceUnavailable): browser.space('test')
        launch.assert_not_called()

class NotStartedResumeTest(unittest.TestCase):
    def test_proven_not_started_resumes_but_receipt_unknown_does_not(self):
        from job_agent.store import SQLiteDeliveryStore
        from job_agent.engine.models import Job,Status,Reason
        cfg=Settings(daily={'enabled':True,'deliver_enabled':True},engine={'dry_run':False,'live_send_enabled':True,
            'platforms':{'boss':{'enabled':True,'greeting':'你好','greeting_max_length':10}}})
        with tempfile.TemporaryDirectory() as home, SQLiteDeliveryStore(Path(home)/'jobs.sqlite3',dry_run=False) as db:
            ids=[]
            for key,reason in [('no-effect',Reason.INTERRUPTED),('unknown',Reason.RECEIPT_UNKNOWN)]:
                jid=db.upsert_job(platform='boss',platform_job_id=key,url='https://example.test/'+key,company=key,title=key)
                ids.append(jid)
                job=Job('boss',key,'https://example.test/'+key)
                token=db.create_batch([job],{'boss':'你好'},'',cfg.engine.model_dump())
                db.confirm_batch(token,config=cfg.engine.model_dump(),daily=True)
                db.prepare_run(cfg.engine.model_dump(),batch_token=token)
                db.update(job,Status.UNVERIFIED,reason,1800000000)
                db.update(job,Status.SKIPPED,Reason.NOT_STARTED)
            selected=db.daily_candidates(cfg.engine,cutoff=ids[-1])
            self.assertEqual([r['id'] for r in selected],[ids[0]])
            job=Job('boss','no-effect','https://example.test/no-effect')
            token=db.create_batch([job],{'boss':'你好'},'',cfg.engine.model_dump())
            db.confirm_batch(token,config=cfg.engine.model_dump(),daily=True)
            self.assertEqual(db.records()[0].status,Status.PENDING)

class ScheduleBoundaryTest(unittest.TestCase):
    def test_custom_and_overnight_policy(self):
        from job_agent.engine.rate import RatePolicy
        from job_agent.service.daily_schedule import active
        from zoneinfo import ZoneInfo
        cfg=Settings()
        def at(hour): return datetime(2026,11,27,hour,tzinfo=ZoneInfo('UTC')).timestamp()
        with patch('job_agent.service.daily_schedule.policies',return_value=[RatePolicy(timezone='UTC',allowed_hours=('21:00','03:00'))]):
            self.assertTrue(active(cfg,at(22)))
            self.assertTrue(active(cfg,at(2)))
            self.assertFalse(active(cfg,at(9)))

    def test_cdp_protocol_connection_error_launches(self):
        import subprocess
        from job_agent.browser.ego import EgoBrowser
        def response(value,ok=True):
            payload={'ok':True,'value':value} if ok else {'ok':False,'code':'connection_error'}
            return subprocess.CompletedProcess([],0,'__JOB_AGENT_RESULT__'+json.dumps(payload),'')
        with tempfile.TemporaryDirectory() as home,patch.dict(os.environ,JOB_AGENT_HOME=home), \
             patch('job_agent.browser.ego.subprocess.run',side_effect=[response(None,False),subprocess.CompletedProcess([],0),response(True),response(dict(space_id=1,anchor='p'))]) as run:
            browser=EgoBrowser(keepalive_seconds=0)
            self.assertEqual(browser.space('test').space_id,1)
            self.assertEqual(run.call_args_list[1].args[0],['open','-g','-a','ego lite'])

class ResumeProgressTest(BootResumeTest):
    def test_finished_collection_is_not_repeated(self):
        self.cfg.daily.collect_enabled=True
        started=datetime.now().astimezone().isoformat()
        previous=dict(started_at=started,status='running',collection_complete=True,
                      collection={'platforms':{'boss':{'pages':1}}},old_cutoff_id=0)
        write_state_file(self.home/'daily-last-run.json', json.dumps(previous))
        collect=Mock(side_effect=AssertionError('must not recollect'))
        result=daily_once(config=self.cfg,collect_fn=collect)
        self.assertEqual(result['collection'],previous['collection'])
        self.assertEqual(result['started_at'],started)
        collect.assert_not_called()

    def test_failed_collection_keeps_day_unfinished_and_progress_clock(self):
        self.cfg.daily.collect_enabled=True
        started=datetime.now().astimezone().replace(hour=0,minute=0,second=0).isoformat()
        write_state_file(self.home/'daily-last-run.json', json.dumps(dict(started_at=started,status='running')))
        result=daily_once(config=self.cfg,collect_fn=lambda:{'failed':1,'platforms':{'boss':{'error':'network_error'}}})
        self.assertEqual(result['status'],'failed')
        self.assertEqual(result['last_progress_at'],started)
        self.assertEqual(result['reason_code'],'network_error')
