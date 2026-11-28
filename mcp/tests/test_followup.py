import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from datetime import datetime, timezone
from job_agent.config import Settings
from job_agent.store import SQLiteDeliveryStore


class FollowupFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        env = patch.dict('os.environ', JOB_AGENT_HOME=self.tmp.name); env.start(); self.addCleanup(env.stop)
        self.store = SQLiteDeliveryStore(Path(self.tmp.name)/'jobs.sqlite3'); self.addCleanup(self.store.close)
        self.at = 1800000000

    def config(self, **changes):
        return Settings(followup=dict({"enabled":True, "after_days":[1,1]}, **changes), engine={'platforms':{
            'boss':dict(enabled=True, greeting='原始招呼', greeting_max_length=1000, daily_limit=10, interval_seconds=0)}})

    def job(self, age=2, status='sent', platform='boss', kind='attempt'):
        jid = self.store.upsert_job(platform=platform, platform_job_id=str(len(self.store.list_jobs())),
            url='https://example.invalid/job', company='测试公司', title='工程师')
        stamp = datetime.fromtimestamp(self.at-age*86400, timezone.utc).isoformat()
        self.store.record_delivery(jid, status=status, kind=kind, created_at=stamp, greeting='原始招呼')
        return jid

    def candidates(self, cfg=None):
        from job_agent.store.followup import candidates
        return candidates(self.store, cfg or self.config(), clock=self.at)


class FollowupTests(FollowupFixture):
    def test_time_boundaries(self):
        for age, expected in [(0.99999,False),(1,True),(7,True),(7.00001,False),(-1,False)]:
            with self.subTest(age=age):
                jid = self.job(age)
                self.assertEqual(jid in [r['id'] for r in self.candidates()], expected)

    def test_status_reply_and_platform_conditions(self):
        good = self.job(); confirmed = self.job(kind='reconcile')
        for status in ('pending','failed','unverified','skipped'): self.job(status=status)
        self.job(platform='zhilian')
        replied = self.job()
        self.store.add_reply(replied, message_id='m', direction='inbound', body='问题')
        self.assertEqual([r['id'] for r in self.candidates()], [good, confirmed])
        self.assertEqual(self.candidates(self.config(enabled=False)), [])

    def test_persisted_random_delay_and_first_send_age(self):
        jid = self.job(age=2)
        cfg = self.config(); cfg.followup.after_days = (1,3)
        with patch('job_agent.store.followup.random.randint', return_value=3) as draw:
            self.assertEqual(self.candidates(cfg), [])
            self.assertEqual(self.candidates(cfg), [])
        draw.assert_called_once()
        self.store.record_delivery(jid, status='sent')
        self.assertEqual(self.candidates(cfg), [])

    def test_once_only_even_unknown_and_no_schema_upgrade(self):
        for status in ('sent','unverified','failed','pending','skipped'):
            jid = self.job()
            self.store.record_delivery(jid, status=status, kind='followup')
        self.assertEqual(self.candidates(), [])
        self.assertEqual(self.store.connection.execute('PRAGMA user_version').fetchone()[0],7)

class FollowupFlowTests(FollowupFixture):
    def test_preview_never_enqueues_and_is_once_per_day(self):
        from job_agent.service.followup import run
        self.job()
        with patch('job_agent.service.followup.time.time',return_value=self.at):
            report = run(self.store,self.config())
            again = run(self.store,self.config())
        self.assertEqual(len(report['items']),1)
        self.assertEqual(report['mode'],'preview')
        self.assertFalse(self.store.connection.execute('SELECT 1 FROM delivery_queue').fetchone())
        self.assertEqual(report['items'],again['items'])
        self.assertEqual(report['preview_key'],again['preview_key'])

    def test_auto_uses_queue_kind_and_retains_original(self):
        from job_agent.service.followup import run
        from job_agent.service.deliver import confirm
        jid = self.job()
        cfg = self.config(auto=True)
        with patch('job_agent.service.followup.time.time',return_value=self.at),patch('job_agent.store.delivery.time.time',return_value=self.at),patch('job_agent.engine.queue.time.time',return_value=self.at):
            # Inject clock into real engine, keeping offline adapters and real queue/receipts.
            from job_agent.engine.queue import DeliveryEngine
            original = DeliveryEngine.__init__
            def init(obj,*a,**kw): return original(obj,*a,clock=lambda:self.at,sleep=lambda _:None,**kw)
            with patch.object(DeliveryEngine,'__init__',init), patch('job_agent.engine.adapters.FakeAdapter.check_applied',return_value=True):
                report = run(self.store,cfg,confirm_fn=confirm)
        self.assertEqual(report['items'][0]['status'],'sent')
        rows = self.store.list_deliveries()
        self.assertEqual(rows[0]['kind'],'attempt')
        self.assertTrue(any(r['kind']=='followup' and r['status']=='sent' for r in rows))
        self.assertEqual(self.candidates(cfg),[])
        self.assertEqual(len(self.store.attempt_times('boss')),1)

    def test_generation_failure_falls_back_and_uses_greeting_budget(self):
        from job_agent.service.followup import message
        jid = self.job(); row = self.candidates()[0]; cfg = self.config()
        cfg.followup.generate.enabled = True
        with patch('job_agent.service.scoring.resolve_key',return_value='mock'),patch('job_agent.service.scoring.request_completion',side_effect=TimeoutError) as api:
            text,trace = message(row,cfg)
        self.assertIn(text,cfg.followup.messages)
        self.assertEqual(trace['source'],'fallback')
        api.assert_called_once()
        from job_agent.service.llm_usage import ledger
        with ledger() as db:
            self.assertEqual(db.execute('SELECT purpose,status FROM calls').fetchone()[:],('greetings','provider_error'))

class FollowupQuotaTests(FollowupFixture):
    def test_platform_and_followup_daily_caps_and_pauses(self):
        for i in range(4): self.job()
        cfg = self.config(daily_limit=2)
        self.assertEqual(len(self.candidates(cfg)),2)
        cfg.engine.platforms['boss'].daily_limit=1
        self.assertEqual(len(self.candidates(cfg)),1)
        self.store.pause_platform('boss')
        self.assertEqual(self.candidates(cfg),[])

    def test_attempts_and_reservations_consume_platform_quota(self):
        jid = self.job(); cfg = self.config()
        cfg.engine.platforms['boss'].daily_limit=1
        self.store.connection.execute('INSERT INTO delivery_attempts VALUES(?,?)',(jid,self.at-3600))
        self.assertEqual(self.candidates(cfg),[])
        self.store.connection.execute('DELETE FROM delivery_attempts')
        from job_agent.store.automation import AutomationRepository
        AutomationRepository(self.store).record('boss','delivery',self.at-3600,{'due':self.at,'count':1,'target':10})
        self.assertEqual(self.candidates(cfg),[])

    def test_only_first_send_sets_age_even_if_new_send_exists(self):
        jid=self.job(age=8)
        self.store.record_delivery(jid,status='sent',created_at=datetime.fromtimestamp(self.at-86400,timezone.utc).isoformat())
        self.assertEqual(self.candidates(),[])

    def test_deleted_blank_url_disabled_wrong_mode_and_reply_direction(self):
        jid=self.job(); cfg=self.config()
        cfg.engine.platforms['boss'].enabled=False
        self.assertEqual(self.candidates(cfg),[])
        cfg.engine.platforms['boss'].enabled=True
        cfg.engine.platforms['boss'].greeting_mode='platform_default'
        self.assertEqual(self.candidates(cfg),[])
        cfg.engine.platforms['boss'].greeting_mode='send_message'
        self.store.add_reply(jid,message_id='out',direction='outbound',body='已存在消息')
        self.assertEqual(self.candidates(cfg),[])
        self.store.connection.execute('DELETE FROM replies')
        self.store.connection.execute("UPDATE jobs SET url='' WHERE id=?",(jid,))
        self.assertEqual(self.candidates(cfg),[])
        self.store.connection.execute("UPDATE jobs SET url='https://example.invalid/job',deleted_at='now' WHERE id=?",(jid,))
        self.assertEqual(self.candidates(cfg),[])

    def test_settings_defaults_and_invalid_ranges(self):
        cfg=Settings()
        self.assertFalse(cfg.followup.enabled);self.assertFalse(cfg.followup.auto)
        self.assertEqual(cfg.followup.after_days,(1,3));self.assertEqual(cfg.followup.daily_limit,10)
        for values in ({'after_days':[3,1]},{'after_days':[0,1]},{'after_days':[True,3]},
                       {'after_days':['1',3]},{'after_days':[1,8]},{'daily_limit':-1},{'messages':[]}):
            with self.subTest(values=values),self.assertRaises(ValueError):Settings(followup=values)

class FollowupExecutionTests(FollowupFixture):
    def batch(self,cfg,ids):
        from job_agent.engine.models import Job
        jobs=[self.store.get_job(i) for i in ids]
        texts={'boss':cfg.engine.platforms['boss'].greeting,'_kind':'followup','_followup':cfg.followup.model_dump(mode='json')}
        for row in jobs: texts['boss:'+row['platform_job_id']]=cfg.followup.messages[0]
        return self.store.create_batch([Job(r['platform'],r['platform_job_id'],r['url']) for r in jobs],texts,'',cfg.engine.model_dump())

    def engine(self,cfg,adapter=None):
        from job_agent.engine.queue import DeliveryEngine
        from job_agent.engine.adapters import FakeAdapter
        from job_agent.engine.preflight import FakePreflight
        from job_agent.engine.spaces import FakeTaskSpaces
        return DeliveryEngine(self.store,cfg.engine,{'boss':adapter or FakeAdapter(applied=True)},FakePreflight(),FakeTaskSpaces(),clock=lambda:self.at,sleep=lambda _:None)

    def test_reply_between_preview_confirm_and_send_and_duplicate_batch(self):
        cfg=self.config(auto=True);jid=self.job();token=self.batch(cfg,[jid]);other=self.batch(cfg,[jid])
        with patch('job_agent.store.delivery.time.time',return_value=self.at):
            self.store.confirm_batch(token,config=cfg.engine.model_dump())
            with self.assertRaises(ValueError): self.store.confirm_batch(other,config=cfg.engine.model_dump())
        self.store.add_reply(jid,message_id='late',direction='inbound',body='提问')
        engine=self.engine(cfg);engine.run(batch_token=token)
        self.assertEqual(engine.adapters['boss'].greetings,[])
        self.assertEqual(self.store.records()[0].reason.value,'followup_ineligible')

    def test_global_followup_cap_at_effect_boundary(self):
        cfg=self.config(auto=True,daily_limit=1)
        ids=[self.job(),self.job()];token=self.batch(cfg,ids)
        with patch('job_agent.store.delivery.time.time',return_value=self.at):self.store.confirm_batch(token,config=cfg.engine.model_dump())
        engine=self.engine(cfg);engine.run(batch_token=token)
        self.assertEqual(len(engine.adapters['boss'].greetings),1)
        from job_agent.store.followup import used_today
        self.assertEqual(used_today(self.store,cfg.engine,self.at),1)
        self.assertEqual(used_today(self.store,cfg.engine,self.at+86400),0)

    def test_preview_is_once_per_day_and_telegram_only(self):
        from job_agent.service.followup import run,notify_report
        cfg=self.config();cfg.notify.enabled=True
        from job_agent.config import TelegramSettings,WebhookSettings
        cfg.notify.channels={'tg':TelegramSettings(enabled=True,chat_id='mock'),'hook':WebhookSettings(enabled=True)}
        self.job()
        with patch('job_agent.service.followup.time.time',return_value=self.at),patch('job_agent.notify.send_one',return_value={'success':True}) as sender:
            report=run(self.store,cfg)
            notify_report(self.store,cfg,report);notify_report(self.store,cfg,report)
        sender.assert_called_once()
        self.assertEqual(sender.call_args.args[0].type,'telegram')
        self.assertIn('测试公司｜工程师｜',sender.call_args.args[2])

    def test_pending_followup_batch_resumes_without_replanning(self):
        from job_agent.service.followup import run
        from job_agent.service.deliver import confirm
        from job_agent.engine.queue import DeliveryEngine
        cfg=self.config(auto=True);jid=self.job();token=self.batch(cfg,[jid])
        with patch('job_agent.store.delivery.time.time',return_value=self.at):self.store.confirm_batch(token,config=cfg.engine.model_dump())
        original=DeliveryEngine.__init__
        def init(obj,*a,**kw):return original(obj,*a,clock=lambda:self.at,sleep=lambda _:None,**kw)
        with patch('job_agent.service.followup.time.time',return_value=self.at),patch.object(DeliveryEngine,'__init__',init),patch('job_agent.engine.adapters.FakeAdapter.check_applied',return_value=True):
            report=run(self.store,cfg,confirm_fn=confirm)
        self.assertEqual([r['job_id'] for r in report['items']],[jid])
        self.assertEqual(report['items'][0]['status'],'sent')
        self.assertEqual(self.store.records()[0].attempted_at,self.at)

class FollowupCopyTests(FollowupFixture):
    def test_generated_copy_and_lint_fallback(self):
        import json
        from job_agent.service.followup import message
        cfg=self.config();cfg.followup.generate.enabled=True
        row={'company':'公司','title':'工程师','jd':'岗位描述','original_greeting':'你好，我是候选人。'}
        good='你好，看到这个岗位还在招聘，我仍然很感兴趣。需要了解什么细节都可以聊。'
        response=lambda text:{'choices':[{'finish_reason':'stop','message':{'content':json.dumps({'text':text})}}]}
        with patch('job_agent.service.scoring.resolve_key',return_value='mock'),patch('job_agent.service.scoring.request_completion',return_value=(response(good),{})):
            text,trace=message(row,cfg)
        self.assertEqual(text,good);self.assertEqual(trace['source'],'generated')
        for bad in ('很短',good+'贵司',good+'13812345678',good+'我做过99个项目。',good+'尽快回复我。',good+'候选人'):
            with self.subTest(bad_length=len(bad)),patch('job_agent.service.scoring.resolve_key',return_value='mock'),patch('job_agent.service.scoring.request_completion',return_value=(response(bad),{})):
                text,trace=message(row,cfg)
            self.assertEqual(trace['source'],'fallback');self.assertIn(text,cfg.followup.messages)
        cfg.followup.messages=['不合格']
        self.assertEqual(message(row,cfg),('',{'source':'blocked'}))

    def test_followup_budget_and_message_audit_redaction(self):
        from job_agent.service.followup import message
        from job_agent.service.settings import update_settings,settings_history
        cfg=self.config();cfg.followup.generate.enabled=True;cfg.llm.greetings.daily_calls=0
        with patch('job_agent.service.scoring.resolve_key',return_value='mock'),patch('job_agent.service.scoring.request_completion') as api:
            text,trace=message({'original_greeting':''},cfg)
        api.assert_not_called();self.assertEqual(trace['source'],'fallback')
        value=['你好，这个岗位我仍然很感兴趣。需要补充材料的话随时告诉我就好。']
        update_settings({'followup.messages':value},confirm=True)
        self.assertNotIn(value[0],str(settings_history()))
        self.assertNotIn(value[0],Path(self.tmp.name,'config-history/changes.jsonl').read_text())

class FollowupHistoricalTests(FollowupFixture):
    def test_naive_history_uses_existing_store_utc_convention(self):
        jid=self.job(age=2)
        # Append a second job with the legacy timestamp representation.
        second=self.store.upsert_job(platform='boss',platform_job_id='legacy',url='https://example.invalid/legacy')
        self.store.record_delivery(second,status='sent',created_at=datetime.fromtimestamp(self.at-2*86400,timezone.utc).replace(tzinfo=None).isoformat())
        self.assertEqual([r['id'] for r in self.candidates()],[jid,second])

    def test_legacy_followup_is_never_followed_again(self):
        jid=self.job(age=2)
        self.store.connection.execute("UPDATE jobs SET legacy_status='follow_up_sent' WHERE id=?",(jid,))
        self.assertEqual(self.candidates(),[])
        self.store.connection.execute("UPDATE jobs SET legacy_status='sent' WHERE id=?",(jid,))
        self.store.connection.execute("INSERT INTO source_history VALUES('legacy-fup',?,'follow_up_sent','',?)",(jid,datetime.now(timezone.utc).isoformat()))
        self.assertEqual(self.candidates(),[])

    def test_failed_old_attempt_does_not_redefine_first_confirmed_send(self):
        jid=self.job(age=2)
        self.store.connection.execute('INSERT INTO delivery_attempts VALUES(?,?)',(jid,self.at-8*86400))
        self.assertEqual([r['id'] for r in self.candidates()],[jid])

class FollowupReportingTests(FollowupFixture):
    def test_followup_does_not_inflate_new_application_success_totals(self):
        from job_agent.store.reporting import success_counts
        jid=self.job(age=2)
        self.store.record_delivery(jid,status='sent',kind='followup',created_at=datetime.fromtimestamp(self.at,timezone.utc).isoformat())
        self.assertEqual(success_counts(self.store.connection,datetime.fromtimestamp(self.at,timezone.utc)),{})

    def test_result_notice_survives_exception_after_committed_receipt(self):
        from job_agent.service.followup import execute
        from job_agent.engine.models import Job,Status,Reason
        cfg=self.config(auto=True)
        cfg.notify.enabled=True
        from job_agent.config import TelegramSettings
        cfg.notify.channels={'tg':TelegramSettings(enabled=True,chat_id='mock')}
        jid=self.job();row=self.store.get_job(jid);job=Job('boss',row['platform_job_id'],row['url'])
        greetings={'boss':cfg.engine.platforms['boss'].greeting,'boss:'+job.job_id:cfg.followup.messages[0],
            '_kind':'followup','_followup':cfg.followup.model_dump(mode='json')}
        token=self.store.create_batch([job],greetings,'',cfg.engine.model_dump())
        def interrupted(db,config,token):
            with patch('job_agent.store.delivery.time.time',return_value=self.at):db.confirm_batch(token,config=config.engine.model_dump())
            db.prepare_run(config.engine.model_dump(),batch_token=token)
            db.update(job,Status.UNVERIFIED,Reason.INTERRUPTED,self.at)
            db.update(job,Status.SENT,Reason.NONE)
            raise RuntimeError('synthetic interruption')
        with self.assertRaises(RuntimeError):execute(self.store,cfg,token,interrupted)
        queued=[r for r in self.store.notifications() if r['dedupe_key'].startswith('followup-result:')]
        self.assertEqual(len(queued),1);self.assertIn('成功',queued[0]['body'])
        self.assertEqual(queued[0]['kind'],'delivery')
        self.assertTrue(self.store.delivery_notification_ready(queued[0]['delivery_id']))

    def test_large_preview_is_chunked_without_losing_characters(self):
        from job_agent.service.followup import notification_parts
        text='示例😀'*3000
        parts=notification_parts(text)
        self.assertEqual(''.join(parts),text)
        self.assertTrue(all(len(p.encode('utf-16-le'))//2<=3000 for p in parts))
