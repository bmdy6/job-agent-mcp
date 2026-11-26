import tempfile
import unittest
from pathlib import Path
from engine.cooldown import CooldownSettings, duplicate
from store import Store


class CooldownTest(unittest.TestCase):
    def setUp(self):
        self.cfg = CooldownSettings()
        self.now = 1800000000
        self.job = dict(platform='boss', company='测试科技有限公司（苏州）', title='急招 AI工程师 15-25K', hr_name='张经理', hr_id='1')
        self.sent = dict(self.job, attempted_at=self.now-86400)

    def test_rules(self):
        cases = [({}, True), ({'hr_name':'李经理','hr_id':'2'},False),
                 ({'title':'Java工程师'},False), ({'attempted_at':self.now-31*86400},False),
                 ({'platform':'zhilian','hr_id':'999'},True), ({'hr_name':'','hr_id':''},True),
                 ({'company':'测试科技','title':'AI工程师'},True),
                 ({'attempted_at':self.now-30*86400},False)]
        for changes, expected in cases:
            with self.subTest(changes=changes):
                self.assertEqual(duplicate(self.job, dict(self.sent, **changes), self.cfg, self.now), expected)

    def test_missing_hr_and_no_shared_identity_is_conservative(self):
        self.assertTrue(duplicate(dict(self.job,hr_name='',hr_id=''),self.sent,self.cfg,self.now))
        self.assertTrue(duplicate(self.job,dict(self.sent,platform='liepin',hr_name='',hr_id='2'),self.cfg,self.now))
        self.assertFalse(duplicate(self.job,dict(self.sent,hr_id='2'),self.cfg,self.now))

    def test_normalization_configurable(self):
        cfg=CooldownSettings(company_patterns=['公司$'],title_patterns=['招聘'])
        self.assertTrue(duplicate(dict(self.job,company='甲公司',title='招聘AI'),dict(self.sent,company='甲',title='AI'),cfg,self.now))

    def test_additive_hr_migration(self):
        with tempfile.TemporaryDirectory() as tmp, Store(Path(tmp)/'jobs.db') as s:
            jid=s.upsert_job(platform='boss',platform_job_id='1',hr_name='张经理',hr_title='招聘经理',hr_id='123')
            self.assertEqual(s.get_job(jid)['hr_title'],'招聘经理')


class DailyQueueTest(unittest.TestCase):
    def setUp(self):
        from config import Settings
        from store import SQLiteDeliveryStore
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.db=SQLiteDeliveryStore(Path(self.tmp.name)/'jobs.sqlite3',dry_run=False); self.addCleanup(self.db.close)
        self.cfg=Settings(daily={'enabled':True,'deliver_enabled':True},engine={'backend':'own','dry_run':False,'live_send_enabled':True,
            'platforms':{p:dict(enabled=True,greeting='您好',greeting_max_length=10,daily_limit=2,interval_seconds=0) for p in ('boss','zhilian')}})
        self.now=1800000000

    def job(self, key, **kw):
        return self.db.upsert_job(platform=kw.pop('platform','boss'),platform_job_id=key,url='https://example.test/'+key,
                                  company='甲',title=key,**kw)

    def selected(self,cutoff,**kw):
        return [r['id'] for r in self.db.daily_candidates(self.cfg.engine,cutoff=cutoff,clock=self.now,**kw)]

    def test_old_first_all_jobs_including_filtered_unscored(self):
        a=self.job('a',legacy_status='filtered',legacy_score=0,collected_at='2026-01-01')
        b=self.job('b',legacy_score=90,collected_at='2026-02-01')
        c=self.job('c',legacy_score=0,collected_at='2026-03-01')
        n=self.job('new',platform='zhilian',legacy_score=100)
        self.assertEqual(self.selected(c),[b,a])
        self.cfg.engine.platforms['boss'].daily_limit=4
        self.assertEqual(self.selected(c),[b,a,c,n])

    def test_unverified_not_retried_cooldown_expires_and_identity_is_frozen(self):
        from engine.models import Job,Status,Reason
        jid=self.job('AI',hr_name='张',hr_id='1')
        job=Job('boss','AI','https://example.test/AI')
        token=self.db.create_batch([job],{'boss':'您好'},'',self.cfg.engine.model_dump())
        self.db.confirm_batch(token,config=self.cfg.engine.model_dump(),daily=True)
        self.db.prepare_run(self.cfg.engine.model_dump(),batch_token=token)
        self.db.update(job,Status.UNVERIFIED,Reason.INTERRUPTED,self.now)
        self.db.update(job,Status.SENT,Reason.NONE)
        self.assertTrue(self.db.duplicate_for(job,self.cfg.engine.cooldown,self.now+86400))
        self.db.upsert_job(platform='boss',platform_job_id='AI',hr_name='李',hr_id='2')
        self.assertFalse(self.db.duplicate_for(job,self.cfg.engine.cooldown,self.now+86400))
        self.db.upsert_job(platform='boss',platform_job_id='AI',hr_name='张',hr_id='1')
        self.assertIn(jid,[j['id'] for j in self.db.daily_candidates(self.cfg.engine,cutoff=jid,clock=self.now+31*86400)])
        from unittest.mock import patch
        next_token=self.db.create_batch([job],{'boss':'您好'},'',self.cfg.engine.model_dump())
        with patch('store.daily.time.time',return_value=self.now+31*86400):
            self.db.confirm_batch(next_token,config=self.cfg.engine.model_dump(),daily=True)
        self.assertEqual(self.db.records()[0].status,Status.PENDING)
        self.db.connection.execute("UPDATE delivery_queue SET status='unverified'")
        self.assertEqual(self.selected(jid),[])

    def test_actual_remaining_budget_and_acceptance_cap(self):
        self.job('a'); self.job('b'); c=self.job('c')
        self.assertEqual(len(self.selected(c,max_per_platform=1)),1)
        self.db.connection.execute("INSERT INTO delivery_queue(job_id,status,attempted_at) VALUES(1,'sent',?)",(self.now,))
        self.db.connection.execute('INSERT INTO delivery_attempts VALUES(1,?)',(self.now,))
        self.db.record_delivery(1,status='sent')
        self.assertEqual(self.selected(c),[2])

    def record_sent(self, jid, days_ago=1):
        from datetime import datetime, timezone
        stamp=datetime.fromtimestamp(self.now-days_ago*86400,timezone.utc).isoformat()
        self.db.record_delivery(jid,status='sent',kind='legacy_event',created_at=stamp)

    def test_recent_sent_history_in_pending_snapshot_does_not_take_slot(self):
        old=self.job('already-sent', legacy_score=99)
        self.record_sent(old)
        self.db.connection.execute("INSERT INTO delivery_queue(job_id,status) VALUES(?,'pending')",(old,))
        nxt=self.job('genuinely-new', legacy_score=10)
        excluded={}
        self.assertEqual(self.selected(nxt,max_per_platform=1,planning_exclusions=excluded),[nxt])
        self.assertEqual(excluded['boss']['known_cooldown'],1)

    def test_known_identity_duplicate_but_unknown_hr_on_other_job_is_not_preempted(self):
        self.cfg.engine.platforms['boss'].daily_limit=20
        self.cfg.engine.platforms['zhilian'].daily_limit=20
        def row(platform, key, name='', hr_id=''):
            return self.db.upsert_job(platform=platform,platform_job_id=key,
                url='https://example.test/'+key,company='甲有限公司',title='AI工程师',
                hr_name=name,hr_id=hr_id)
        original=row('boss','old','张经理','1')
        self.record_sent(original)
        same=row('boss','same','张经理','1')
        other=row('boss','other','李经理','2')
        unknown=row('boss','unknown')
        cross_same=row('zhilian','cross-same','张经理','8')
        cross_unknown=row('zhilian','cross-unknown')
        for jid in (same,other,unknown,cross_same,cross_unknown):
            self.db.connection.execute("INSERT INTO delivery_queue(job_id,status) VALUES(?,'pending')",(jid,))
        selected=set(self.selected(cross_unknown))
        self.assertEqual(selected,{other,unknown,cross_unknown})

    def test_expired_pending_history_can_be_planned_again(self):
        jid=self.job('expired')
        self.record_sent(jid,days_ago=31)
        self.db.connection.execute("INSERT INTO delivery_queue(job_id,status) VALUES(?,'pending')",(jid,))
        self.assertEqual(self.selected(jid),[jid])

    def test_pending_with_attempt_timestamp_is_not_rebindable(self):
        jid=self.job('attempted')
        self.db.connection.execute("INSERT INTO delivery_queue(job_id,status,attempted_at) VALUES(?,'pending',?)",(jid,self.now-86400))
        nxt=self.job('next')
        excluded={}
        self.assertEqual(self.selected(nxt,max_per_platform=1,planning_exclusions=excluded),[nxt])
        self.assertEqual(excluded['boss']['attempted_pending'],1)

    def test_preview_preserves_explicit_planner_order(self):
        import json
        from service.deliver import preview
        a=self.job('lower')
        b=self.job('higher')
        batch=json.loads(preview(self.db,self.cfg,job_ids=[b,a,b]))
        self.assertEqual([j['job_id'] for j in batch['jobs']],['higher','lower'])

    def test_metadata_is_persisted_without_erasing_list_hr_id(self):
        from platforms.base import JobListing
        from platforms.hr import JobDetail
        from store.collection import CollectionRepository
        listing=JobListing('boss','x','https://example.test/x','甲','AI',hr_name='张',hr_title='HR',hr_id='123')
        repo=CollectionRepository(self.db)
        j=repo.ingest(listing,[])
        repo.set_jd(listing,JobDetail('完整JD',{'hr_name':'张','hr_title':'经理','hr_id':''}))
        self.assertEqual(self.db.get_job(j['id'])['hr_id'],'123')
        self.assertEqual(self.db.get_job(j['id'])['jd'],'完整JD')

    def test_unverified_event_cannot_be_hidden_by_failed_snapshot(self):
        from engine.models import Job
        jid=self.job('unknown')
        self.db.record_delivery(jid,status='unverified',reason_code='receipt_unknown')
        self.db.connection.execute("INSERT INTO delivery_queue(job_id,status) VALUES(?,'failed')",(jid,))
        self.assertEqual(self.selected(jid),[])
        job=Job('boss','unknown','https://example.test/unknown')
        token=self.db.create_batch([job],{'boss':'您好'},'',self.cfg.engine.model_dump())
        with self.assertRaises(ValueError):
            self.db.confirm_batch(token,config=self.cfg.engine.model_dump(),daily=True)


class DailyRoundTest(DailyQueueTest):
    def test_collect_then_old_delivery_then_new_and_two_notifications(self):
        import os
        from unittest.mock import patch
        from service.daily import daily_once
        from engine.models import Status,Reason
        self.cfg.notify.enabled=True
        self.cfg.notify.channels={}
        self.cfg.engine.platforms['boss'].daily_limit=20
        self.job('old',legacy_score=20)
        self.job('higher',platform='zhilian',legacy_score=90)
        calls=[]
        def collect():
            calls.append('collect')
            self.job('new',legacy_score=100)
            return {'platforms':{'boss':{'new':1}},'failed':0}
        def confirm(db,cfg,token,**kw):
            db.confirm_batch(token,config=cfg.engine.model_dump(),daily=True)
            db.prepare_run(cfg.engine.model_dump(),batch_token=token)
            for j in db.execution_jobs(token):
                calls.append(j.job_id)
                db.update(j,Status.UNVERIFIED,Reason.INTERRUPTED,self.now)
                db.update(j,Status.SENT,Reason.NONE)
            db.complete_batch(token)
            return "—— 明细 ——\n成功回执\n   https://example.test/private-job"
        # SQLite cannot open one live path in dry-run; this verifies the real owned store.
        with patch.dict(os.environ,{'JOB_AGENT_HOME':self.tmp.name}),patch('service.daily.send',return_value=['tg']) as send:
            result=daily_once(config=self.cfg,collect_fn=collect,confirm_fn=confirm)
            self.assertEqual(result['status'],'completed')
            self.assertEqual(calls[0],'collect')
            self.assertCountEqual(calls[1:3],['higher','old'])
            self.assertEqual(calls[3],'new')
            self.assertEqual(send.call_count,2)
            self.assertIn('成功回执',result['delivery_details'])
            self.assertFalse(any('https://' in line for line in result['delivery_details']))

    def test_report_separates_preexcluded_from_actual_batch_results(self):
        import os
        from unittest.mock import patch
        from service.daily import daily_once, delivery_message
        from engine.models import Status,Reason
        self.cfg.daily.collect_enabled=False
        old=self.job('old-sent',legacy_score=99)
        self.record_sent(old)
        self.db.connection.execute("INSERT INTO delivery_queue(job_id,status) VALUES(?,'pending')",(old,))
        nxt=self.job('candidate')
        def confirm(db,cfg,token,**kw):
            db.confirm_batch(token,config=cfg.engine.model_dump(),daily=True)
            db.prepare_run(cfg.engine.model_dump(),batch_token=token)
            for j in db.execution_jobs(token):
                db.update(j,Status.SKIPPED,Reason.JOB_UNAVAILABLE)
            db.complete_batch(token)
        with patch.dict(os.environ,{'JOB_AGENT_HOME':self.tmp.name}),patch('store.daily.time.time',return_value=self.now):
            result=daily_once(config=self.cfg,confirm_fn=confirm,max_per_platform=1)
        self.assertEqual(result['selected'],[nxt])
        self.assertEqual(result['planning_exclusions']['boss'],{'known_cooldown':1})
        self.assertEqual(result['delivery']['boss']['skipped'],1)
        self.assertEqual(result['delivery']['boss']['sent'],0)
        self.assertIn('规划排除（不占本轮选择）',delivery_message(result)[1])

    def test_independent_switches_never_send_or_collect_when_disabled(self):
        import os
        from unittest.mock import patch,Mock
        from service.daily import daily_once
        self.cfg.daily.collect_enabled=False
        self.cfg.daily.deliver_enabled=False
        collect,confirm=Mock(),Mock()
        with patch.dict(os.environ,{'JOB_AGENT_HOME':self.tmp.name}):
            r=daily_once(config=self.cfg,collect_fn=collect,confirm_fn=confirm)
        collect.assert_not_called();confirm.assert_not_called()
        self.assertEqual(r['status'],'completed')

    def test_collection_risk_blocks_only_affected_platform(self):
        import os
        from unittest.mock import patch,Mock
        from service.daily import daily_once
        collect=lambda:{'platforms':{'boss':{'error':'risk_control'}},'failed':1}
        confirm=Mock()
        self.job('old')
        with patch.dict(os.environ,{'JOB_AGENT_HOME':self.tmp.name}):
            r=daily_once(config=self.cfg,collect_fn=collect,confirm_fn=confirm)
        confirm.assert_not_called()
        self.assertEqual(r['delivery']['boss']['stop_reason'],'collection_error_or_previous_batch_stop')


    def test_collection_timeout_does_not_block_delivery(self):
        import os
        from unittest.mock import patch
        from service.daily import daily_once
        collect=lambda:{'platforms':{'boss':{'error':'collection_timeout','pages':1}},'failed':1}
        calls=[]
        def confirm(store,config,token,daily=False):
            calls.append(token)
            return ''
        self.job('old')
        with patch.dict(os.environ,{'JOB_AGENT_HOME':self.tmp.name}):
            r=daily_once(config=self.cfg,collect_fn=collect,confirm_fn=confirm)
        self.assertNotEqual(r['delivery']['boss'].get('stop_reason'),'collection_error_or_previous_batch_stop')
        self.assertTrue(calls)


class CompatibilityTest(unittest.TestCase):
    def test_daily_extension_survives_scoring_migration(self):
        from store import Store
        with tempfile.TemporaryDirectory() as tmp:
            db=Path(tmp)/'jobs.sqlite3'
            with Store(db) as store:
                store.upsert_job(platform='boss',platform_job_id='1',hr_name='张')
            import sqlite3
            from contextlib import closing
            with closing(sqlite3.connect(db)) as old:
                self.assertEqual(old.execute('PRAGMA user_version').fetchone()[0],6)
                self.assertEqual(old.execute('SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 1').fetchone()[0],6)
                self.assertEqual(old.execute('SELECT platform,platform_job_id FROM jobs').fetchone(),('boss','1'))

    def test_installer_defaults_are_calendar_only_and_acceptance_cap_is_explicit(self):
        import importlib.util
        path=Path(__file__).resolve().parents[2]/'scripts/install-daily.py'
        spec=importlib.util.spec_from_file_location('daily_installer',path)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        data=module.plist('/stable',Path('/python'),Path('/private'),8,0)
        self.assertEqual(data['StartCalendarInterval'],{'Hour':8,'Minute':0})
        self.assertNotIn('RunAtLoad',data)
        self.assertNotIn('--max-per-platform',data['ProgramArguments'])
        cap=module.plist('/stable',Path('/python'),Path('/private'),11,20,3,1)
        self.assertEqual(cap['ProgramArguments'][-4:],['--max-per-platform','3','--collection-page-limit','1'])

    def test_legacy_filter_is_not_an_automatic_delivery_filter(self):
        from store import SQLiteDeliveryStore
        from config import Settings
        with tempfile.TemporaryDirectory() as tmp,SQLiteDeliveryStore(Path(tmp)/'jobs.sqlite3',dry_run=False) as store:
            jid=store.upsert_job(platform='boss',platform_job_id='x',url='https://example.test/x',legacy_status='filtered')
            store.connection.execute("INSERT INTO delivery_queue(job_id,status,reason_code) VALUES(?,'skipped','legacy_filtered')",(jid,))
            cfg=Settings(engine={'platforms':{'boss':{'enabled':True,'greeting':'您好','greeting_max_length':10}}})
            self.assertEqual([j['id'] for j in store.daily_candidates(cfg.engine,cutoff=jid)],[jid])

    def test_refresh_daily_restarts_each_query_first_page(self):
        from store.collection import CollectionRepository
        from platforms.base import SearchQuery
        with tempfile.TemporaryDirectory() as tmp,Store(Path(tmp)/'jobs.sqlite3') as s:
            clock=[1800000000.0]
            repo=CollectionRepository(s,clock=lambda:clock[0])
            repo.begin_cycle('boss',['AI','Agent'],['苏州'],refresh_daily=True)
            repo.complete('boss',SearchQuery('AI','苏州'),done=True)
            self.assertIsNone(repo.next_page('boss','AI','苏州'))
            clock[0]+=86400
            repo.begin_cycle('boss',['AI','Agent'],['苏州'],refresh_daily=True)
            self.assertEqual(repo.next_page('boss','AI','苏州'),1)

    def test_confirm_requires_daily_opt_in(self):
        from config import Settings
        from service.deliver import confirm
        with self.assertRaisesRegex(ValueError,'daily delivery opt-in'):
            confirm(None,Settings(),'',daily=True)

    def test_sidecar_settings_preserve_old_config_and_main_wins(self):
        import os
        from unittest.mock import patch
        from config import load_config
        with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{'JOB_AGENT_HOME':tmp}):
            Path(tmp,'config.yaml').write_text('engine:\n  backend: own\n')
            original=Path(tmp,'config.yaml').read_bytes()
            Path(tmp,'daily.yaml').write_text('daily:\n  enabled: true\n  hour: 9\nengine:\n  cooldown:\n    days: 31\ncollect:\n  refresh_daily: true\n')
            cfg=load_config()
            self.assertEqual((cfg.daily.hour,cfg.engine.cooldown.days,cfg.collect.refresh_daily),(9,31,True))
            self.assertEqual(Path(tmp,'config.yaml').read_bytes(),original)
            Path(tmp,'config.yaml').write_text('daily:\n  hour: 8\n')
            self.assertEqual(load_config().daily.hour,8)
            Path(tmp,'daily.yaml').write_text('engine:\n  live_send_enabled: true\n')
            with self.assertRaises(ValueError):load_config()

    def test_daily_lock_rejects_second_round_before_effects(self):
        from service.daily import run_lock
        with tempfile.TemporaryDirectory() as tmp,run_lock(Path(tmp)):
            with self.assertRaisesRegex(RuntimeError,'already running'):
                with run_lock(Path(tmp)):
                    self.fail('second round acquired the lock')

    def test_risk_pause_survives_restart_and_requires_explicit_resume(self):
        from browser.base import FakeBrowser,BrowserError
        from collect.runner import Collector
        from collect.config import CollectSettings
        from store.collection import CollectionRepository
        class Risk(BrowserError):code='risk_control'
        class Source:
            platform='liepin'
            calls=0
            def search(self,*args):
                self.calls+=1
                raise Risk('stop')
        cfg=CollectSettings(platforms={'liepin':{'enabled':True}})
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'jobs.sqlite3'
            source=Source()
            for _ in range(2):
                with Store(path) as s:
                    result=Collector(FakeBrowser(),[source],CollectionRepository(s),cfg,sleep=lambda _:None).run()
            self.assertEqual(source.calls,1)
            self.assertEqual(result['platforms']['liepin']['error'],'risk_control_paused')
            with Store(path) as s:
                s.resume_platform('liepin')
                self.assertNotIn('liepin',s.paused_platforms())

    def test_positive_unavailable_page_is_terminal_skip_without_effect(self):
        from engine.adapters import FakeAdapter
        from engine.config import EngineSettings
        from engine.models import Job,Status,Reason
        from engine.queue import DeliveryEngine
        from engine.preflight import FakePreflight
        from engine.spaces import FakeTaskSpaces
        from store import SQLiteDeliveryStore
        from platforms.boss.pages import guard,JobUnavailable
        with self.assertRaises(JobUnavailable):guard({'unavailable':True})
        with tempfile.TemporaryDirectory() as tmp,SQLiteDeliveryStore(Path(tmp)/'dry.sqlite3') as db:
            adapter=FakeAdapter()
            from unittest.mock import patch
            cfg=EngineSettings(platforms={'boss':{'enabled':True,'greeting':'您好','greeting_max_length':10}})
            engine=DeliveryEngine(db,cfg,{'boss':adapter},FakePreflight(),FakeTaskSpaces())
            engine.enqueue([Job('boss','closed','https://example.test/closed')])
            with patch.object(adapter,'open_job',side_effect=JobUnavailable('明确不存在')):
                engine.run()
            self.assertEqual((db.records()[0].status,db.records()[0].reason),(Status.SKIPPED,Reason.JOB_UNAVAILABLE))
            self.assertNotIn('send_greeting',adapter.calls)

    def test_imported_send_keeps_its_timestamp_instead_of_stale_queue_time(self):
        from datetime import datetime
        with tempfile.TemporaryDirectory() as tmp,Store(Path(tmp)/'jobs.sqlite3') as s:
            jid=s.upsert_job(platform='boss',platform_job_id='x')
            s.connection.execute("INSERT INTO delivery_queue(job_id,status,attempted_at) VALUES(?,'sent',1)",(jid,))
            event=s.record_delivery(jid,status='sent',kind='legacy_event',created_at='2026-11-25T00:00:00Z')
            at=s.connection.execute('SELECT attempted_at FROM delivery_identities WHERE delivery_id=?',(event,)).fetchone()[0]
            self.assertEqual(at,datetime.fromisoformat('2026-11-25T00:00:00+00:00').timestamp())
