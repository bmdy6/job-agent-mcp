from state_helpers import business_snapshot
"""#50 collection, reply and cohort contracts."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from contextlib import closing
from job_agent.config import Settings
from job_agent.platforms.base import JobListing
from job_agent.store import Store, SQLiteDeliveryStore

class CollectionSignalsTests(unittest.TestCase):
    def test_activity_vocabulary_and_unknown(self):
        from job_agent.platforms.signals import activity
        for raw, expected in [('刚刚活跃','now'),('在线','now'),('今日活跃','today'),('3日内活跃','three_days'),('本周活跃','week'),('近一个月活跃','inactive'),('2周前活跃','inactive'),('', 'unknown'),('职位更新今天','unknown')]:
            self.assertEqual(activity(raw), expected, raw)

    def test_four_platform_parsers_and_storage(self):
        from importlib import import_module
        from job_agent.platforms.base import SearchQuery
        from job_agent.store.collection import CollectionRepository
        for name in ('boss','zhilian','liepin','job51'):
            module=import_module('job_agent.platforms.'+name+'.pages')
            data=json.loads((Path(__file__).parent/'fixtures/platforms'/name/'search-list.json').read_text())
            data['rows' if name in ('boss','zhilian') else 'items'][0]['hr_activity_raw']='今日活跃'
            rows=module.search_rows(data) if name in ('boss','zhilian') else module.parse_search(data,SearchQuery('AI','上海'))
            first=rows[0]
            self.assertEqual(first.hr_activity,'today')
            self.assertTrue(first.experience)
            self.assertTrue(first.education)
            with tempfile.TemporaryDirectory() as tmp, Store(Path(tmp)/'db') as store:
                saved=CollectionRepository(store).ingest(first,[])
                self.assertEqual(saved['hr_activity_raw'],'今日活跃')
                self.assertEqual(saved['experience'],first.extra['experience'])
                self.assertEqual(saved['education'],first.extra['education'])
                self.assertEqual(store.schema_version,7)

    def test_ranking_and_opt_in_filter(self):
        from job_agent.config.engine import EngineSettings
        from job_agent.engine.job_ranking import rank_jobs
        rows=[dict(id=i,hr_activity=a,experience=e,collected_at='2026-11-26',legacy_score=s) for i,a,e,s in [(1,'inactive','1-3年',100),(2,'unknown','应届',99),(3,'today','3-5年',80),(4,'now','3-5年',10),(5,'today','1-3年',70)]]
        settings=EngineSettings()
        self.assertEqual([j['id'] for j in rank_jobs(rows,settings)], [4,3,5,2,1])
        settings.experience_priority=['应届','经验不限','1-3年']
        self.assertEqual([j['id'] for j in rank_jobs(rows,settings)], [4,5,3,2,1])
        settings.minimum_hr_activity='today'
        self.assertEqual([j['id'] for j in rank_jobs(rows,settings)], [4,5,3])

class ReplySignalsTests(unittest.TestCase):
    def test_rule_categories_and_no_false_invitation(self):
        from job_agent.service.reply_classification import classify
        settings=Settings().scoring
        cases=[('方便明天下午三点线上面试吗？','interview'),('请发一份简历','resume_request'),
               ('暂不考虑，祝你早日找到合适工作','rejection'),('您有几年开发经验？','question'),
               ('谢谢您的关注','other'),('暂不安排面试','rejection'),
               ('岗位职责：负责面试流程优化','other'),('目前流程需要面试，先发简历','resume_request')]
        for body,category in cases:
            result=classify(body,settings,use_model=False)
            self.assertEqual(result['category'],category,body)
        result=classify(cases[0][0],settings,use_model=False)
        self.assertEqual(result['form'],'online')
        self.assertIn('明天',result['time_text'])

    def test_model_contract_and_failure_no_retry(self):
        from job_agent.service.reply_classification import classify
        settings=Settings().scoring
        response={'choices':[{'finish_reason':'stop','message':{'content':json.dumps(dict(category='interview',confidence=.9,time_text='周五',form='phone'))}}]}
        with patch('job_agent.service.reply_classification.resolve_key',return_value='secret'), patch('job_agent.service.reply_classification.request_completion',return_value=(response,{})) as request:
            result=classify('周五打个电话聊聊合适吗',settings,use_model=True)
            self.assertEqual(result['category'],'interview')
            self.assertEqual(result['source'],'model')
            payload=request.call_args.args[0]
            self.assertEqual(payload['model'],settings.model)
            self.assertIn('max_completion_tokens',payload)
            self.assertNotIn('max_tokens',payload)
            self.assertEqual(request.call_count,1)
        with patch('job_agent.service.reply_classification.resolve_key',side_effect=ValueError('secret')):
            result=classify('这周聊聊',settings,use_model=True)
            self.assertEqual(result['category'],'other')
            self.assertEqual(result['confidence'],0)
            self.assertEqual(result['error'],'provider_unavailable')
            self.assertNotIn('secret',str(result))

    def test_pending_schedule_idempotency_and_status_exclusion(self):
        from job_agent.service.reply_classification import classify_pending
        from job_agent.store.signals import invitations
        with tempfile.TemporaryDirectory() as tmp,Store(Path(tmp)/'db') as store:
            job=store.upsert_job(platform='boss',platform_job_id='a',url='https://example.com/job')
            store.record_delivery(job,status='sent',created_at='2026-11-25T00:00:00+00:00')
            store.add_reply(job,message_id='invite',direction='inbound',body='方便明天线上面试吗？',chat_url='https://example.com/chat')
            store.add_reply(job,message_id='out',direction='outbound',body='明天面试')
            classify_pending(store,Settings())
            classify_pending(store,Settings())
            rows=invitations(store)
            self.assertEqual(len(rows),1)
            self.assertEqual(rows[0]['status'],'pending_confirmation')
            self.assertEqual(rows[0]['chat_url'],'https://example.com/chat')
            self.assertIsNone(store.list_replies()[1]['classification'])

class GreetingStatsTests(unittest.TestCase):
    def test_unique_temporal_cohorts_intervals_and_missing_version(self):
        from job_agent.store.signals import greeting_statistics, save_classification
        from job_agent.platforms.greeting import greeting_version
        with tempfile.TemporaryDirectory() as tmp,Store(Path(tmp)/'db') as store:
            for i in range(3):
                j=store.upsert_job(platform='boss',platform_job_id=str(i))
                store.record_delivery(j,status='sent',greeting='你好' if i<2 else '',created_at='2026-11-24T00:00:00+00:00')
                store.record_delivery(j,status='sent',greeting='后续',created_at='2026-11-25T00:00:00+00:00')
                store.add_reply(j,message_id='old'+str(i),direction='inbound',body='历史',occurred_at='2026-11-23T00:00:00+00:00')
                if i==0:
                    r=store.add_reply(j,message_id='new',direction='inbound',body='明天面试',occurred_at='2026-11-26T00:00:00+00:00')
                    save_classification(store,r,dict(category='interview',confidence=.95,source='rule',time_text='明天',form='unknown'))
                    store.add_reply(j,message_id='new2',direction='inbound',body='后续回复',occurred_at='2026-11-26T01:00:00+00:00')
            stats=greeting_statistics(store,min_samples=20)
            rows={g['greeting_version']:g for g in stats['groups']}
            group=rows[greeting_version('你好')]
            self.assertEqual((group['applied'],group['replied'],group['interview_invited']),(2,1,1))
            self.assertEqual(group['reply_rate'],.5)
            self.assertEqual(group['assessment'],'样本不足')
            self.assertLess(group['reply_ci95'][0],.5)
            self.assertGreater(group['reply_ci95'][1],.5)
            self.assertEqual(rows['unknown']['applied'],1)
            self.assertEqual(len(rows),2)

class MigrationSixTests(unittest.TestCase):
    def v5(self,path):
        import sqlite3
        from job_agent.store.database import MIGRATIONS
        with closing(sqlite3.connect(path)) as db, db:
            db.execute('CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY,applied_at TEXT)')
            for version,statements in MIGRATIONS:
                if version>5: break
                for sql in statements: db.execute(sql)
                db.execute('INSERT INTO schema_migrations VALUES(?,?)',(version,'before'))
            db.execute('PRAGMA user_version=5')
            db.execute("INSERT INTO jobs(platform,platform_job_id,collected_at,legacy_score_reason) VALUES('boss','before','2026-11-25','keep')")
            db.execute("INSERT INTO deliveries(job_id,status,greeting,created_at,updated_at) VALUES(1,'sent','original','2026-11-25','2026-11-25')")
            db.execute("INSERT INTO replies(job_id,message_id,direction,body,occurred_at,platform) VALUES(1,'r','inbound','original reply','2026-11-26','boss')")

    def test_v5_upgrade_idempotent_preserves_all_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'v5.db';self.v5(path)
            for _ in range(2):
                with Store(path, migrate=True) as store:
                    self.assertEqual(store.schema_version,7)
                    self.assertEqual(store.get_job(1)['legacy_score_reason'],'keep')
                    self.assertEqual(store.get_job(1)['hr_activity'],'unknown')
                    self.assertEqual(store.list_deliveries()[0]['greeting'],'original')
                    self.assertEqual(store.list_replies()[0]['body'],'original reply')
                    self.assertIsNone(store.list_replies()[0]['classification'])
                    self.assertEqual(store.connection.execute('SELECT count(*) FROM schema_migrations WHERE version=6').fetchone()[0],1)

    def test_failed_migration_restores_schema_and_ledger(self):
        import sqlite3
        from job_agent.store import database
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'v5.db';self.v5(path)
            bad=[(v,sql+['INVALID SQL'] if v==6 else sql) for v,sql in database.MIGRATIONS]
            with patch.object(database,'MIGRATIONS',bad),self.assertRaises(sqlite3.OperationalError): Store(path, migrate=True)
            with closing(sqlite3.connect(path)) as db, db:
                self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],5)
                self.assertNotIn('hr_activity',[r[1] for r in db.execute('PRAGMA table_info(jobs)')])
                self.assertEqual(db.execute('SELECT count(*) FROM schema_migrations WHERE version=6').fetchone()[0],0)

class ModelValidationTests(unittest.TestCase):
    def test_invalid_refused_or_invented_evidence_is_fallback(self):
        from job_agent.service.reply_classification import classify
        valid=dict(category='interview',confidence=.9,time_text='周五',form='phone')
        variations=[dict(valid,confidence=True),dict(valid,confidence=-1),dict(valid,time_text='明年'),
                    dict(valid,form='onsite'),dict(valid,category='offer'),dict(valid,confidence=.2)]
        for value in variations:
            response={'choices':[{'finish_reason':'stop','message':{'content':json.dumps(value)}}]}
            with patch('job_agent.service.reply_classification.resolve_key',return_value='hidden'),patch('job_agent.service.reply_classification.request_completion',return_value=(response,{})):
                outcome=classify('周五打个电话聊聊合适吗',Settings().scoring,use_model=True)
                self.assertEqual(outcome['category'],'other')
                self.assertEqual(outcome['error'],'invalid_model_result')

    def test_model_cap_and_excluded_synthetic_replies(self):
        from job_agent.service.reply_classification import classify_pending
        cfg=Settings(engine={'monitor':{'classification_model_enabled':True,'classification_model_limit':1}})
        with tempfile.TemporaryDirectory() as tmp,Store(Path(tmp)/'db') as store:
            j=store.upsert_job(platform='boss',platform_job_id='j')
            ids=[]
            for i in range(4): ids.append(store.add_reply(j,message_id=str(i),direction='inbound',body='这周聊聊'))
            store.add_reply(j,message_id='status',direction='inbound',body='明天面试',id_origin='application_status')
            store.add_reply(j,message_id='excerpt',direction='inbound',body='明天面试',is_excerpt=True)
            with patch('job_agent.service.reply_classification.resolve_key',side_effect=ValueError('hidden')) as resolve:
                stats=classify_pending(store,cfg)
                self.assertEqual(stats['classified'],6)
                self.assertEqual(stats['model_calls'],1)
                self.assertEqual(resolve.call_count,1)
                classify_pending(store,cfg)
                self.assertEqual(resolve.call_count,1)
            self.assertEqual(store.list_replies()[-1]['classification_source'],'excluded')

class SignalMCPTests(unittest.IsolatedAsyncioTestCase):
    async def test_tools_funnel_briefing_and_readonly(self):
        import job_agent.server as server
        from job_agent.store.signals import save_classification
        with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,JOB_AGENT_HOME=tmp):
            Path(tmp,'config.yaml').write_text('engine: {backend: own}\n')
            with Store() as store:
                j=store.upsert_job(platform='boss',platform_job_id='j',company='测试',title='AI',experience='1-3年',education='本科')
                store.record_delivery(j,status='sent',greeting='v1',created_at='2026-11-24T00:00:00+00:00')
                r=store.add_reply(j,message_id='r',direction='inbound',body='明天线上面试',occurred_at='2026-11-26T00:00:00+00:00')
                save_classification(store,r,dict(category='interview',confidence=.95,source='rule',time_text='明天',form='online'))
            before=business_snapshot(Path(tmp,'jobs.sqlite3'))
            with patch('job_agent.service.monitor.status',return_value={}),patch('job_agent.service.reply_classification.request_completion',side_effect=AssertionError('No model')):
                stats=(await server.mcp.call_tool('greeting_stats',{}))[1]
                self.assertEqual(stats['total']['interview_invited'],1)
                schedule=(await server.mcp.call_tool('interview_schedule',{}))[1]
                self.assertEqual(schedule['count'],1)
                for group in ('platform','greeting_version'):
                    funnel=(await server.mcp.call_tool('funnel_stats',{'group_by':group}))[1]
                    self.assertEqual(funnel['total']['interview_invited'],1)
                    self.assertEqual(sum(g['interview_invited'] for g in funnel['groups']),1)
                    self.assertEqual(funnel['total']['invitation_rate'],1)
                briefing=(await server.mcp.call_tool('briefing',{}))[1]
                self.assertIn('greeting_stats',briefing)
                self.assertEqual(briefing['todos']['interview_invitations']['count'],1)
            self.assertEqual(business_snapshot(Path(tmp,'jobs.sqlite3')),before)

class DailySignalTests(unittest.TestCase):
    def test_daily_old_before_new_and_activity_within_cohort(self):
        cfg=Settings(engine={'platforms':{'boss':dict(enabled=True,greeting_max_length=20,daily_limit=5)}})
        with tempfile.TemporaryDirectory() as tmp,SQLiteDeliveryStore(Path(tmp)/'db') as store:
            for i,a,score in [('a','unknown',99),('b','today',20),('c','now',80)]:
                store.upsert_job(platform='boss',platform_job_id=i,url='https://example.test/'+i,hr_activity=a,legacy_score=score)
            rows=store.daily_candidates(cfg.engine,cutoff=2,clock=1800000000)
            self.assertEqual([r['platform_job_id'] for r in rows],['b','a','c'])
            cfg.engine.minimum_hr_activity='today'
            rows=store.daily_candidates(cfg.engine,cutoff=2,clock=1800000000)
            self.assertEqual([r['platform_job_id'] for r in rows],['b','c'])
            from job_agent.service.deliver import preview
            with self.assertRaisesRegex(ValueError,'minimum HR activity'):
                preview(store,cfg,job_ids=[1])

class MonitorSignalTests(unittest.TestCase):
    def test_invitation_alert_is_separate_label_and_deduplicated(self):
        import time
        from job_agent.monitor import Monitor
        from job_agent.browser.base import FakeBrowser
        from job_agent.platforms.base import ReplyEvent
        class Source:
            platform='boss'
            def fetch(self,*args,**kwargs):
                return [ReplyEvent('boss','invite','j','公司','岗位','incoming','方便明天下午三点线上面试吗？',time.time(),'https://example.com/chat')]
        config=Settings(notify={'enabled':True,'channels':{'tg':{'type':'telegram','enabled':True,'chat_id':'fake'}}})
        with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,JOB_AGENT_HOME=tmp),Store() as store:
            j=store.upsert_job(platform='boss',platform_job_id='j')
            store.record_delivery(j,status='sent')
            with patch('job_agent.notify.send_one',return_value={'success':True}) as sender:
                monitor=Monitor(FakeBrowser(),[Source()],store,config)
                self.assertEqual(monitor.poll_once()['added'],1)
                self.assertIn('面试邀约（待确认）',sender.call_args.args[1])
                self.assertIn('需本人决定',sender.call_args.args[2])
                self.assertEqual(monitor.poll_once()['sent'],0)
                self.assertEqual(sender.call_count,1)

    def test_config_rejects_invalid_activity_and_model_limits(self):
        from pydantic import ValidationError
        for options in ({'minimum_hr_activity':'inactive'}, {'monitor':{'classification_model_limit':31}}, {'monitor':{'classification_model_enabled':'true'}}):
            with self.assertRaises(ValidationError): Settings(engine=options)

class ClassificationLifecycleTests(unittest.TestCase):
    def test_monitor_loads_same_scoring_settings(self):
        from job_agent.monitor.config import load_settings
        with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,JOB_AGENT_HOME=tmp):
            Path(tmp,'config.yaml').write_text('scoring: {model: custom-scoring, service_tier: flex, key_source: env, api_key_env: TEST_KEY}\n')
            cfg,_=load_settings()
            self.assertEqual(cfg.scoring.model,'custom-scoring')
            self.assertEqual(cfg.scoring.service_tier,'flex')
            self.assertEqual(cfg.scoring.api_key_env,'TEST_KEY')

    def test_backlog_survives_batch_cap_and_dispatched_crash_never_retries(self):
        from job_agent.service.reply_classification import classify_pending
        from job_agent.store.signals import claim_classification
        cfg=Settings(engine={'monitor':{'classification_batch_limit':1,'classification_model_enabled':True}})
        with tempfile.TemporaryDirectory() as tmp,Store(Path(tmp)/'db') as store:
            j=store.upsert_job(platform='boss',platform_job_id='j')
            a=store.add_reply(j,message_id='a',direction='inbound',body='请发简历')
            b=store.add_reply(j,message_id='b',direction='inbound',body='方便明天线上面试吗')
            c=store.add_reply(j,message_id='c',direction='inbound',body='周五聊聊')
            self.assertTrue(claim_classification(store,c))
            with patch('job_agent.service.reply_classification.request_completion',side_effect=AssertionError('No retry')):
                self.assertEqual(classify_pending(store,cfg)['classified'],1)
                self.assertEqual(classify_pending(store,cfg)['classified'],1)
            rows={r['id']:r for r in store.list_replies()}
            self.assertEqual(rows[c]['classification_error'],'worker_interrupted')
            self.assertEqual(rows[b]['classification'],'interview')

    def test_migration_does_not_queue_historical_paid_classification(self):
        from job_agent.service.reply_classification import classify_pending
        cfg=Settings(engine={'monitor':{'classification_model_enabled':True}})
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'db';MigrationSixTests().v5(path)
            with Store(path, migrate=True) as store,patch('job_agent.service.reply_classification.request_completion',side_effect=AssertionError('No historical model')):
                self.assertEqual(classify_pending(store,cfg)['classified'],0)


class NotificationBacklogTests(unittest.TestCase):
    def test_backlog_waits_for_classification_without_losing_invitation_alert(self):
        import time
        from job_agent.monitor import Monitor
        from job_agent.browser.base import FakeBrowser
        from job_agent.platforms.base import ReplyEvent
        class Source:
            platform='boss'
            def fetch(self,*args,**kwargs):
                return [ReplyEvent('boss',str(i),'j','公司','岗位','incoming','方便明天线上面试吗？',time.time()) for i in range(2)]
        cfg=Settings(engine={'monitor':{'classification_batch_limit':1}},notify={'enabled':True,'channels':{'tg':{'type':'telegram','enabled':True,'chat_id':'fake'}}})
        with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,JOB_AGENT_HOME=tmp),Store() as store:
            j=store.upsert_job(platform='boss',platform_job_id='j')
            store.record_delivery(j,status='sent')
            with patch('job_agent.notify.send_one',return_value={'success':True}) as sender:
                monitor=Monitor(FakeBrowser(),[Source()],store,cfg)
                self.assertEqual(monitor.poll_once()['sent'],1)
                self.assertEqual(monitor.poll_once()['sent'],1)
                self.assertEqual(monitor.poll_once()['sent'],0)
                self.assertEqual(sender.call_count,2)
                self.assertTrue(all('面试邀约' in call.args[1] for call in sender.call_args_list))
