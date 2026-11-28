from state_helpers import read_state_file, write_state_file
import json
import logging
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


class ObservabilityTest(unittest.TestCase):
    def test_context_inherits_and_resets(self):
        from job_agent.observability import run_scope, current_run_id
        self.assertIsNone(current_run_id())
        with run_scope('daily') as outer:
            with run_scope('deliver') as inner:
                self.assertEqual(outer, inner)
            with run_scope('monitor', fresh=True) as other:
                self.assertNotEqual(outer, other)
            self.assertEqual(current_run_id(), outer)
        self.assertIsNone(current_run_id())

    def test_bounded_private_error(self):
        from job_agent.observability import error_details
        try:
            raise RuntimeError('connection refused https://private.test/path token=SECRET hr_name=PRIVATE body=PRIVATE')
        except RuntimeError as error:
            detail=error_details(error, platform='boss', stage='fetch')
        self.assertEqual(detail['platform'],'boss')
        self.assertEqual(detail['stage'],'fetch')
        self.assertEqual(detail['error_type'],'RuntimeError')
        self.assertTrue(0<len(detail['frames'])<=3)
        self.assertRegex(detail['frames'][-1],r'test_observability.py:\d+$')
        self.assertLessEqual(len(detail['error_message']),300)
        self.assertNotIn('SECRET',json.dumps(detail))
        self.assertNotIn('PRIVATE',json.dumps(detail))
        self.assertNotIn('private.test',json.dumps(detail))

    def test_receipts_deduplicate_across_runs(self):
        from job_agent.observability import run_scope, report_error
        from job_agent.config import Settings
        config=Settings(notify={'enabled':True,'channels':{'tg':{'type':'telegram','enabled':True,'chat_id':'test'}}})
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ,JOB_AGENT_HOME=tmp), patch('job_agent.notify.send_one',return_value={'success':True}) as send:
            for _ in range(2):
                with run_scope('daily'):
                    try: raise RuntimeError('Daily round already running')
                    except RuntimeError as error: report_error(error,platform='boss',stage='collection',config=config)
            self.assertEqual(send.call_count,1)
            self.assertIn('建议',send.call_args.args[2])
            self.assertEqual(len(json.loads(read_state_file(Path(tmp,'notified.jsonl')))),1)

    def test_early_daily_failure_is_persisted(self):
        from job_agent.service.daily import daily_once
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ,JOB_AGENT_HOME=tmp), patch('job_agent.service.daily.load_config',side_effect=RuntimeError('configuration unavailable')):
            with self.assertRaises(RuntimeError): daily_once()
            state=json.loads(read_state_file(Path(tmp,'daily-last-run.json')))
            self.assertEqual(state['status'],'failed')
            self.assertEqual(state['error_type'],'RuntimeError')
            self.assertEqual(len(state['run_id']),32)
            self.assertTrue(state['finished_at'])

    def test_daily_failure_has_one_alert_origin_and_running_state(self):
        from job_agent.service.daily import daily_once
        from job_agent.config import Settings
        cfg=Settings(daily={'enabled':True,'deliver_enabled':False},notify={'enabled':True,'channels':{'tg':{'type':'telegram','enabled':True,'chat_id':'test'}}})
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ,JOB_AGENT_HOME=tmp), patch('job_agent.notify.send_one',return_value={'success':True}) as send:
            def collect():
                self.assertEqual(json.loads(read_state_file(Path(tmp,'daily-last-run.json')))['status'],'running')
                error=RuntimeError('connection refused')
                error.platform='boss'; error.stage='fetch'
                raise error
            with self.assertLogs('job_agent.observability',level='ERROR') as logs:
                for _ in range(2):
                    with self.assertRaises(RuntimeError): daily_once(config=cfg,collect_fn=collect)
            self.assertEqual(send.call_count,1)
            state=json.loads(read_state_file(Path(tmp,'daily-last-run.json')))
            self.assertEqual(state['status'],'failed')
            self.assertEqual(state['platform'],'boss')
            self.assertEqual(state['stage'],'fetch')
            self.assertEqual(state['error_message'],'connection refused')
            self.assertIn('test_observability.py',str(state['frames']))
            self.assertIn('connection refused',logs.output[0])
            self.assertEqual(len(logs.output),2)

    def test_lock_contention_preserves_owner_state(self):
        from job_agent.service.daily import daily_once, run_lock
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ,JOB_AGENT_HOME=tmp):
            path=Path(tmp,'daily-last-run.json')
            write_state_file(path,'{"status":"running","run_id":"owner"}')
            with run_lock(Path(tmp)), patch('job_agent.observability._alert'):
                with self.assertRaisesRegex(RuntimeError,'already running'):
                    daily_once()
            self.assertEqual(json.loads(read_state_file(path)),{'status':'running','run_id':'owner'})

    def test_database_start_failure_and_corrupt_previous_state(self):
        from job_agent.service.daily import daily_once
        from job_agent.config import Settings
        cfg=Settings(daily={'enabled':True,'deliver_enabled':False})
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ,JOB_AGENT_HOME=tmp):
            with patch('job_agent.service.daily.SQLiteDeliveryStore',side_effect=RuntimeError('database is locked')):
                with self.assertRaises(RuntimeError): daily_once(config=cfg)
            self.assertEqual(json.loads(read_state_file(Path(tmp,'daily-last-run.json')))['status'],'failed')
            with patch('job_agent.service.daily.read_run',side_effect=ValueError('corrupt run payload')):
                with self.assertRaises(ValueError): daily_once(config=cfg)
            self.assertEqual(json.loads(read_state_file(Path(tmp,'daily-last-run.json')))['status'],'failed')

    def test_audit_json_preserves_resume_selection(self):
        from job_agent.observability import run_scope
        from job_agent.store import Store
        with tempfile.TemporaryDirectory() as tmp, Store(Path(tmp)/'jobs.db') as store:
            ident=store.upsert_job(platform='boss',platform_job_id='fixture')
            selection={'selected':{'name':'fixture','attachment':1}}
            with run_scope('delivery') as run_id:
                key=store.record_delivery(ident,status='sent',error_detail=json.dumps({'resume_selection':selection}))
            audit=json.loads(store.get_delivery(key)['error_detail'])
            self.assertEqual(audit['run_id'],run_id)
            self.assertEqual(audit['resume_selection'],selection)

    def test_daily_trends_deduplicate_markers_and_use_real_quota(self):
        from job_agent.store import Store
        from job_agent.store.reporting import daily_trends
        from job_agent.config import Settings
        from datetime import datetime
        with tempfile.TemporaryDirectory() as tmp, Store(Path(tmp)/'jobs.db') as store:
            a=store.upsert_job(platform='boss',platform_job_id='a')
            b=store.upsert_job(platform='boss',platform_job_id='b')
            for status,reason in [('unverified','interrupted'),('sent','')]:
                store.record_delivery(a,status=status,reason_code=reason,created_at='2026-11-27T16:10:00+00:00')
            store.record_delivery(b,status='failed',reason_code='precheck_error',created_at='2026-11-28T00:10:00+08:00')
            store.record_delivery(b,status='sent',kind='legacy_snapshot',created_at='2026-11-28T00:11:00+08:00')
            from job_agent.store.automation import AutomationRepository
            AutomationRepository(store)
            at=datetime.fromisoformat('2026-11-28T00:10:00+08:00').timestamp()
            store.connection.execute("INSERT INTO operation_rate VALUES('boss','delivery',?)",(at,))
            store.connection.execute('INSERT INTO delivery_attempts VALUES(?,?)',(a,at))
            store.connection.execute('INSERT INTO delivery_attempts VALUES(?,?)',(b,at-60))
            row=daily_trends(store.connection,Settings(),cutoff='2026-11-28')['rows'][0]
            self.assertEqual(row['day'],'2026-11-28')
            self.assertEqual((row['success'],row['unverified'],row['failed']),(1,0,1))
            self.assertEqual(row['quota_used'],2)
            self.assertEqual(row['top_reasons'],[{'reason':'precheck_error','count':1}])

    def test_notification_suffix_survives_truncation_and_replay(self):
        from job_agent.notify import bounded, send_one
        from job_agent.config import TelegramSettings
        from job_agent.observability import run_scope
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ,JOB_AGENT_HOME=tmp), patch('job_agent.notify.secret',return_value='fixture'):
            with run_scope('daily') as original, patch('job_agent.notify._attempts',return_value={'success':False,'error_code':'transport'}):
                send_one(TelegramSettings(enabled=True,chat_id='fixture'),'title','x'*5000)
            entries=[json.loads(l) for l in read_state_file(Path(tmp,'notify-outbox.jsonl')).splitlines()]
            with run_scope('heartbeat'):
                rendered=bounded(entries[0]['title'],entries[0]['text'],None,4096,'utf-16-le')
            self.assertTrue(rendered.endswith('运行编号：'+original[:8]))
            self.assertLessEqual(len(rendered.encode('utf-16-le'))//2,4096)

    def test_all_log_records_carry_context(self):
        from job_agent.observability import run_scope
        with run_scope('scoring',platform='boss') as ident, self.assertLogs('fixture',level='INFO') as logs:
            logging.getLogger('fixture').info('scoring complete')
        self.assertEqual(logs.records[0].run_id,ident)
        self.assertEqual(logs.records[0].platform,'boss')
        self.assertEqual(logs.records[0].stage,'scoring')

    def test_platform_alert_uses_existing_receipts(self):
        from job_agent.service.safety import telegram_alert
        from job_agent.config import Settings
        cfg=Settings(notify={'enabled':True,'channels':{'tg':{'type':'telegram','enabled':True,'chat_id':'test'}}})
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ,JOB_AGENT_HOME=tmp), patch('job_agent.notify.send_one',return_value={'success':True}) as send:
            telegram_alert(cfg,'boss','browser_error')
            telegram_alert(cfg,'boss','browser_error')
            telegram_alert(cfg,'boss','login_required')
            self.assertEqual(send.call_count,2)

    def test_cli_unhandled_error_returns_failure_without_raw_traceback(self):
        from job_agent.cli import main
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ,JOB_AGENT_HOME=tmp), patch('job_agent.cli._setup_logging'), patch('job_agent.service.heartbeat.run_once',side_effect=RuntimeError('PRIVATE body')):
            with self.assertLogs('job_agent.observability',level='ERROR') as captured:
                self.assertEqual(main(['heartbeat','--once']),1)
            self.assertNotIn('PRIVATE',str(captured.output))

    def test_latest_failure_remains_visible_after_recovery(self):
        from job_agent.service.daily import daily_once
        from job_agent.service.read_tools import daily_status, status, setup_checks
        from job_agent.config import Settings
        cfg=Settings(daily={'enabled':True,'collect_enabled':False,'deliver_enabled':False})
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ,JOB_AGENT_HOME=tmp):
            with patch('job_agent.service.daily.load_config',side_effect=RuntimeError('configuration unavailable')):
                with self.assertRaises(RuntimeError): daily_once()
            failed_id=daily_status()['run_id']
            daily_once(config=cfg)
            self.assertEqual(daily_status()['status'],'completed')
            self.assertEqual(daily_status()['last_failure']['run_id'],failed_id)
            with patch('job_agent.service.read_tools.load_config',return_value=cfg), patch('job_agent.service.scoring.setup_checks',return_value={}):
                self.assertEqual(status()['daily']['last_failure']['run_id'],failed_id)
                self.assertEqual(setup_checks()['daily_last_run']['last_failure']['run_id'],failed_id)

    def test_explicit_dry_run_failure_does_not_send_notifications(self):
        from job_agent.collect.runtime import run
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ,JOB_AGENT_HOME=tmp), patch('job_agent.collect.runtime.load_config',side_effect=RuntimeError('configuration unavailable')), patch('job_agent.observability._alert') as alert:
            with self.assertLogs('job_agent.observability',level='ERROR') as logs:
                self.assertEqual(run(dry_run=True),1)
            alert.assert_not_called()
            self.assertNotEqual(logs.records[0].run_id,'-')


class AlertRecoveryTest(unittest.TestCase):
    def config(self):
        from job_agent.config import Settings
        return Settings(daily={'enabled':True,'deliver_enabled':False},
            notify={'enabled':True,'channels':{'tg':{'type':'telegram','enabled':True,'chat_id':'fixture'}}})

    def test_platform_and_error_alerts_expire_and_reset_on_recovery(self):
        from job_agent.observability import report_error, run_scope
        from job_agent.service.safety import PlatformSafety, telegram_alert
        from job_agent.platforms.session import SessionResult
        from job_agent.browser.base import FakeBrowser
        from job_agent.store import Store
        cfg=self.config()
        for kind in ('platform','error'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ,JOB_AGENT_HOME=tmp), patch('job_agent.notify.send_one',return_value={'success':True}) as send, patch('time.time',return_value=10000) as now:
                def alert():
                    with run_scope('collection'):
                        if kind=='platform': telegram_alert(cfg,'boss','login_required')
                        else: report_error(RuntimeError('connection refused'),platform='boss',stage='collection',config=cfg)
                alert()
                now.return_value=10000+cfg.observability.heartbeat_cooldown_minutes*60-1
                alert()
                self.assertEqual(send.call_count,1)
                now.return_value+=1
                alert()
                self.assertEqual(send.call_count,2)
                # Recovery survives a new safety object; a durable risk pause stays intact.
                with Store(Path(tmp)/'jobs.sqlite3') as store:
                    store.pause_platform('boss')
                    safety=PlatformSafety(FakeBrowser(),store,cfg,
                        check=lambda b,p,s:SessionResult(p,True,'',True))
                    safety.inspect('boss')
                    self.assertIn('boss',store.paused_platforms())
                alert()
                self.assertEqual(send.call_count,3)

    def test_daily_platform_check_transport_failure_is_queued_without_failure(self):
        self._daily_alert_failure(locked=False)

    def test_daily_notification_lock_waits_then_continues(self):
        self._daily_alert_failure(locked=True)

    def _daily_alert_failure(self, *, locked, code='transport', expired=False):
        import fcntl
        import threading
        import time
        from job_agent.service.daily import daily_once
        from job_agent.service.safety import telegram_alert
        cfg=self.config()
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ,JOB_AGENT_HOME=tmp), patch('job_agent.notify.secret',return_value='fixture'), patch('job_agent.notify._attempts',return_value={'success':False,'error_code':code}) as attempts, patch('job_agent.service.daily.send',return_value=[]):
            releases=[]
            def collect():
                # The direct callback must never raise, even outside inspect's guard.
                if locked:
                    fd=os.open(Path(tmp)/'notified.lock',os.O_CREAT|os.O_WRONLY,0o600)
                    fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    def release():
                        time.sleep(.15)
                        os.close(fd)
                    thread=threading.Thread(target=release)
                    thread.start(); releases.append(thread)
                telegram_alert(cfg,'boss','login_required')
                return {'platforms':{'boss':{'error':'login_required'}},'failed':1}
            result=daily_once(config=cfg,collect_fn=collect)
            for thread in releases: thread.join()
            self.assertEqual(result['status'],'completed')
            self.assertEqual(json.loads(read_state_file(Path(tmp,'daily-last-run.json')))['status'],'completed')
            self.assertEqual(attempts.called, not expired)
            entries=[json.loads(line) for line in read_state_file(Path(tmp,'notify-outbox.jsonl')).splitlines()]
            self.assertEqual(len(entries),1)
            self.assertIn('登录',entries[0]['text'])
            receipt=Path(tmp,'notified.jsonl')
            self.assertFalse(receipt.exists() and receipt.read_text().strip())

    def test_notification_lock_timeout_queues_without_sending(self):
        import fcntl
        from job_agent.service.safety import telegram_alert
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ,JOB_AGENT_HOME=tmp), patch('job_agent.notify.secret',return_value='fixture'), patch('job_agent.notify._attempts') as attempts, patch('job_agent.observability.NOTIFIED_LOCK_TIMEOUT_SECONDS',.05,create=True):
            fd=os.open(Path(tmp)/'notified.lock',os.O_CREAT|os.O_WRONLY,0o600)
            try:
                fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                self.assertFalse(telegram_alert(self.config(),'boss','login_required'))
            finally: os.close(fd)
            attempts.assert_not_called()
            self.assertEqual(len(read_state_file(Path(tmp,'notify-outbox.jsonl')).splitlines()),1)

    def test_daily_interrupts_keep_status_and_resume_checkpoint(self):
        from job_agent.service.daily import daily_once
        from job_agent.service.heartbeat import problems
        from datetime import datetime
        from unittest.mock import Mock
        cfg=self.config()
        for error in (KeyboardInterrupt,SystemExit):
            with self.subTest(error=error), tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ,JOB_AGENT_HOME=tmp), patch('job_agent.notify.send_one',return_value={'success':True}):
                def collect(): raise error()
                with self.assertRaises(error): daily_once(config=cfg,collect_fn=collect)
                path=Path(tmp,'daily-last-run.json')
                state=json.loads(read_state_file(path))
                self.assertEqual(state['status'],'interrupted')
                self.assertEqual(state['last_failure']['error_type'],error.__name__)
                state.update(started_at=datetime(2026,11,27,9).astimezone().isoformat(),
                    last_progress_at=datetime(2026,11,27,9).astimezone().isoformat(),collection_complete=True)
                write_state_file(path,json.dumps(state))
                self.assertIn('daily_stale',problems(cfg,tmp,datetime(2026,11,27,14).timestamp()))
                state['started_at']=datetime.now().astimezone().isoformat()
                write_state_file(path,json.dumps(state))
                collect=Mock(side_effect=AssertionError('checkpoint must skip collection'))
                result=daily_once(config=cfg,collect_fn=collect)
                self.assertTrue(result['resumed'])
                self.assertEqual(result['status'],'completed')
                collect.assert_not_called()


    def test_nontransport_alert_failure_also_stays_in_outbox(self):
        self._daily_alert_failure(locked=False,code='403')

    def test_legacy_alert_receipt_is_not_permanent_and_watch_receipts_stay_readable(self):
        from job_agent.service.safety import telegram_alert
        from job_agent.monitor.receipts import digest
        from job_agent.observability import recover_alerts
        cfg=self.config()
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ,JOB_AGENT_HOME=tmp), patch('job_agent.notify.send_one',return_value={'success':True}) as send:
            key='platform-error:'+digest('boss','login_required',cfg.collect.task_space_name)
            path=Path(tmp,'notified.jsonl')
            path.write_text(json.dumps(dict(event=key,job=key,channel='tg'))+'\n'+
                json.dumps(dict(event='watch-fixture',job='watch-job',channel='tg'))+'\n')
            telegram_alert(cfg,'boss','login_required')
            telegram_alert(cfg,'boss','login_required')
            self.assertEqual(send.call_count,1)
            recover_alerts(cfg,'boss')
            from job_agent.store import Store
            with Store() as store:
                self.assertTrue(store.notification_sent('reply:watch-fixture:tg'))
                self.assertEqual(len([r for r in store.notifications() if r['kind']=='reply']),1)
            self.assertFalse(path.exists())
            telegram_alert(cfg,'boss','login_required')
            self.assertEqual(send.call_count,2)


    def test_daily_notification_lock_timeout_is_nonfatal(self):
        with patch('job_agent.observability.NOTIFIED_LOCK_TIMEOUT_SECONDS',.05):
            self._daily_alert_failure(locked=True,expired=True)

    def test_transport_failure_preserves_platform_check_and_login_stop(self):
        from job_agent.service.safety import PlatformSafety
        from job_agent.platforms.session import SessionResult
        from job_agent.browser.base import FakeBrowser
        from job_agent.engine.rate import RateDeferred
        from job_agent.store import Store
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ,JOB_AGENT_HOME=tmp), patch('job_agent.notify.secret',return_value='fixture'), patch('job_agent.notify._attempts',return_value={'success':False,'error_code':'transport'}), Store(Path(tmp)/'jobs.sqlite3') as store:
            expected=SessionResult('boss',False,'login_required')
            safety=PlatformSafety(FakeBrowser(),store,self.config(),check=lambda *a:expected)
            self.assertIs(safety.inspect('boss'),expected)
            with self.assertRaises(RateDeferred) as error: safety.require('boss')
            self.assertEqual(error.exception.code,'login_required')
            self.assertTrue(error.exception.alert_failed)

    def test_recovery_only_resets_the_recovered_platform(self):
        from job_agent.service.safety import telegram_alert
        from job_agent.observability import recover_alerts
        cfg=self.config()
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ,JOB_AGENT_HOME=tmp), patch('job_agent.notify.send_one',return_value={'success':True}) as send:
            for platform in ('boss','zhilian'): telegram_alert(cfg,platform,'login_required')
            recover_alerts(cfg,'boss')
            for platform in ('boss','zhilian'): telegram_alert(cfg,platform,'login_required')
            self.assertEqual(send.call_count,3)
