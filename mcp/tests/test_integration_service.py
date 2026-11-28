import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from job_agent.config import Settings
from job_agent.engine.models import Job, Status, Reason
from job_agent.store import SQLiteDeliveryStore
from job_agent.service.deliver import preview, confirm
from job_agent.platforms.registry import create_adapters
from job_agent.monitor.runtime import create_sources
from job_agent.browser.ego import EgoBrowser, _SCRIPT
from test_ego_browser import result

class IntegrationServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env=patch.dict(os.environ, JOB_AGENT_HOME=self.tmp.name)
        env.start(); self.addCleanup(env.stop)
        self.db = SQLiteDeliveryStore(Path(self.tmp.name)/'dry.sqlite')
        self.addCleanup(self.db.close)
        self.config = Settings.model_validate({'engine': {'monitor': {'interval_minutes': 3}, 'platforms': {
            p: {'enabled': True, 'greeting': '完整招呼语', 'greeting_max_length': 500,
                'interval_seconds': 0, 'greeting_mode': 'platform_default' if p == 'zhilian' else 'send_message'}
            for p in ('boss','zhilian','liepin','51job')}}})
        self.ids = [self.db.upsert_job(platform=p, platform_job_id=p+'-1',url='https://example.com/'+p)
                    for p,options in self.config.engine.platforms.items() if options.enabled]

    def test_full_batch_and_replay_and_unrelated_queue(self):
        self.db.add(Job('boss','outside','https://example.com/outside'))
        token = json.loads(preview(self.db,self.config,job_ids=self.ids))['token']
        with patch('job_agent.notify.secret',return_value='TEST-TOKEN'),patch('job_agent.notify.TelegramAdapter.send') as send:
            text = confirm(self.db,self.config,token)
            send.assert_not_called()
        self.assertEqual(text.count('✅'),4)
        self.assertIn('未投递',text)
        self.assertEqual(self.db.get_batch(token)['status'],'executed')
        self.assertEqual(next(r for r in self.db.records() if r.job.job_id=='outside').status,Status.PENDING)
        with self.assertRaises(ValueError): confirm(self.db,self.config,token)

    def test_content_change_refuses_before_confirmation(self):
        token = json.loads(preview(self.db,self.config,job_ids=self.ids))['token']
        self.config.engine.platforms['boss'].greeting = '已改动'
        with self.assertRaises(ValueError): confirm(self.db,self.config,token)
        self.assertEqual(self.db.get_batch(token)['status'],'pending')

    def test_dry_notify_and_live_switch_fail_closed(self):
        token = json.loads(preview(self.db,self.config,job_ids=self.ids))['token']
        with self.assertRaises(ValueError): confirm(self.db,self.config,token,notify=True)
        self.config.engine.dry_run = False
        with self.assertRaises(ValueError): confirm(self.db,self.config,token)
        self.assertEqual(self.db.get_batch(token)['status'],'pending')

    def test_registry_constructs_all_concrete_adapters_and_sources(self):
        adapters = create_adapters(object(),self.config.engine)
        self.assertEqual(set(adapters),set(self.config.engine.platforms))
        self.assertEqual({s.platform for s in create_sources(object(),self.config.engine.monitor)},set(self.config.engine.monitor.platforms))
        self.assertTrue(all(type(s).__name__ != 'UnavailableSource' for s in create_sources(object(),self.config.engine.monitor)))

    def test_platform_message_uniqueness_across_jobs_and_directions(self):
        first=self.db.add_reply(self.ids[0],message_id='same',direction='inbound',body='one')
        other=self.db.upsert_job(platform='boss',platform_job_id='other')
        self.assertEqual(self.db.add_reply(other,message_id='same',direction='outbound',body='two'),first)
        self.assertNotEqual(self.db.add_reply(self.ids[1],message_id='same',direction='inbound',body='three'),first)

    def test_engine_error_persists_human_reason_and_audit(self):
        from job_agent.engine.queue import DeliveryEngine
        from job_agent.engine.adapters import FakeAdapter
        from job_agent.engine.preflight import FakePreflight
        from job_agent.engine.spaces import FakeTaskSpaces
        job=Job('51job','broken','https://example.com/broken')
        self.db.add(job)
        engine=DeliveryEngine(self.db,self.config.engine,{'51job':FakeAdapter()},FakePreflight(),FakeTaskSpaces())
        with patch.object(FakeAdapter,'send_greeting',side_effect=RuntimeError('已申请、附件未确认')):
            records=engine.run()
        self.assertEqual(records[0].status,Status.UNVERIFIED)
        self.assertEqual(records[0].error_detail,'已申请、附件未确认')
        audit=json.loads(self.db.list_deliveries()[-1]['error_detail'])
        self.assertEqual(audit['detail'],'已申请、附件未确认')
        self.assertEqual(len(audit['run_id']),32)

    def test_shutdown_closes_only_own_temporaries_retains_anchor(self):
        with patch('job_agent.browser.ego.subprocess.run') as call:
            call.return_value=result({'space_id':99,'anchor':'p1'})
            b=EgoBrowser(); space=b.space('test')
            call.return_value=result({'label':'p2'})
            page=space.new_page()
            call.return_value=result({'space_id':99,'anchor':'p1'})
            self.assertIs(b.space('test'),space)
            call.return_value=result()
            b.shutdown('test')
            self.assertTrue(page._closed)
            requests=[json.loads(c.kwargs['input'].split('const request = ',1)[1].split(';\n',1)[0]) for c in call.call_args_list]
            closes=[r['label'] for r in requests if r.get('method')=='close']
            self.assertEqual(closes,['p2'])
            self.assertFalse(next(r for r in requests if r.get('op')=='shutdown')['finish'])

    def test_historical_unverified_does_not_block_new_batch(self):
        old=Job('boss','old-unknown','https://example.com/old')
        self.db.add(old)
        self.db.update(old,Status.UNVERIFIED,Reason.EFFECT_ERROR)
        token=json.loads(preview(self.db,self.config,job_ids=self.ids))['token']
        self.assertEqual(confirm(self.db,self.config,token).count('✅'),4)

    def test_telegram_summary_transport_is_explicit_and_mocked(self):
        from job_agent.service.deliver import send_summary
        cfg=Settings.model_validate({'notify':{'enabled':True,'channels':{'telegram':{'type':'telegram','enabled':True,'chat_id':'test'}}}})
        with patch('job_agent.notify.secret',return_value='TEST-TOKEN'),patch('job_agent.notify.TelegramAdapter.send') as send:
            send_summary(cfg,'平台四条汇总')
            send.assert_called_once_with('job-agent 投递汇总','平台四条汇总',None)

    def test_opted_in_live_service_uses_concrete_factory_with_mock_effect(self):
        from job_agent.engine.queue import DeliveryEngine
        cfg=self.config.model_copy(deep=True)
        cfg.engine.dry_run=False
        cfg.engine.live_send_enabled=True
        with SQLiteDeliveryStore(Path(self.tmp.name)/'live.sqlite',dry_run=False) as db:
            ids=[db.upsert_job(platform='boss',platform_job_id='live-mock',url='https://example.com/mock')]
            token=json.loads(preview(db,cfg,job_ids=ids))['token']
            def simulate(engine,job,settings,space):
                db.update(job,Status.SENT,Reason.NONE)
            with patch('job_agent.service.safety.PlatformSafety.require'), patch('job_agent.engine.rate.RateLimiter.acquire'), patch.object(DeliveryEngine,'_deliver',simulate), patch('job_agent.engine.spaces.TaskSpaces.shutdown') as shutdown, patch('job_agent.service.deliver.drain_notifications') as notify:
                text=confirm(db,cfg,token,notify=True)
                self.assertIn('成功 1',text)
                notify.assert_called_once()
                shutdown.assert_called_once_with(cfg.engine.task_space_name)
            self.assertEqual(db.get_batch(token)['status'],'executed')

    def test_v2_reply_collision_is_archived_without_losing_originals(self):
        import sqlite3
        from job_agent.store.database import MIGRATIONS, Store
        path=Path(self.tmp.name)/'v2.sqlite'
        with sqlite3.connect(path) as raw:
            for version,statements in MIGRATIONS[:2]:
                for statement in statements: raw.execute(statement)
            raw.execute('PRAGMA user_version=2')
            for i in (1,2):
                raw.execute("INSERT INTO jobs(id,platform,platform_job_id,collected_at) VALUES(?,'boss',?,'now')",(i,str(i)))
                raw.execute("INSERT INTO replies(job_id,message_id,direction,body,occurred_at) VALUES(?,'dup','inbound',?,'now')",(i,str(i)))
        with Store(path, migrate=True) as db:
            self.assertEqual(len(db.list_replies()),1)
            archive=db.connection.execute('SELECT body FROM replies_legacy_duplicates').fetchall()
            self.assertEqual([r[0] for r in archive],['2'])
            self.assertEqual(db.connection.execute('PRAGMA integrity_check').fetchone()[0],'ok')
