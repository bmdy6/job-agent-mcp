import os
import sys
import time
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from browser.base import FakeBrowser,FakeSpace,FakePage
from config import Settings
from store import Store
from store.collection import CollectionRepository
from platforms.session import check_session,SessionResult
from service.safety import PlatformSafety
from collect.runner import Collector
from collect.config import CollectSettings


class SafetyTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=Store(Path(self.tmp.name)/'jobs.sqlite3');self.addCleanup(self.store.close)
        self.config=Settings()
    def test_engine_checks_login_in_its_own_space(self):
        seen=[]
        def check(browser,platform,space,**kw):
            seen.append(space)
            return SessionResult(platform=platform,ok=True,logged_in=True,account_type='unknown',reason='')
        safety=PlatformSafety(FakeBrowser(),self.store,self.config,check=check,alert=lambda *a:None,space_name='job-agent 监听')
        safety.inspect('boss')
        self.assertEqual(seen,['job-agent 监听'])
        PlatformSafety(FakeBrowser(),self.store,self.config,check=check,alert=lambda *a:None).inspect('boss')
        self.assertEqual(seen[-1],self.config.collect.task_space_name)

    def test_positive_login_and_liepin_account_type(self):
        for platform in ['boss','zhilian','liepin','51job']:
            for logged,account,expected in [(True,'professional',True),(False,'unknown',False),(True,'student',True)]:
                page=FakePage('p2',{'session.preflight':dict(logged_in=logged,account_type=account,expected_host=True)})
                browser=FakeBrowser({'test':FakeSpace('test',pages=[page])})
                result=check_session(browser,platform,'test')
                self.assertEqual(result.ok,expected)
                self.assertTrue(page.closed)
                self.assertFalse(any(a[0] in {'click','fill','press'} for a in page.actions))
    def test_unknown_no_false_login_and_risk_precedence(self):
        for data,reason in [(None,'session_unknown'),({},'login_required'),({'risk':True,'login':True},'risk_control'),({'logged_in':True,'expected_host':False},'unexpected_redirect')]:
            browser=FakeBrowser({'test':FakeSpace('test',pages=[FakePage('p2',{'session.preflight':data})])})
            self.assertEqual(check_session(browser,'boss','test').reason,reason)
    def test_resume_requires_fresh_login_and_never_auto_clears(self):
        alerts=[]
        safety=PlatformSafety(FakeBrowser(),self.store,self.config,alert=lambda *a:alerts.append(a),
           check=lambda b,p,s:SessionResult(p,False,'login_required'))
        safety.repo.pause('liepin')
        self.assertFalse(safety.resume('liepin')['resumed'])
        self.assertIn('liepin',safety.repo.pauses())
        safety.check=lambda b,p,s:SessionResult(p,True,'',True,'professional')
        self.assertTrue(safety.inspect('liepin').ok)
        self.assertIn('liepin',safety.repo.pauses())
        self.assertTrue(safety.resume('liepin')['resumed'])
        self.assertNotIn('liepin',safety.repo.pauses())
    def test_collection_login_skip_other_platform_runs_and_notifies(self):
        from collect.fake import FakeSource
        browser=FakeBrowser();alerts=[]
        safety=PlatformSafety(browser,self.store,self.config,alert=lambda *a:alerts.append(a),
          check=lambda b,p,s:SessionResult(p,p=='boss','' if p=='boss' else 'login_required',p=='boss'))
        cfg=CollectSettings(keywords=['AI'],cities=['苏州'],platforms={'boss':{'enabled':True},'zhilian':{'enabled':True}})
        result=Collector(browser,[FakeSource('zhilian'),FakeSource('boss')],CollectionRepository(self.store),cfg,
            safety=safety,sleep=lambda _:None).run()
        self.assertEqual(result['platforms']['zhilian']['error'],'login_required')
        self.assertGreater(result['platforms']['boss']['jobs'],0)
        self.assertEqual(alerts,[('zhilian','login_required')])
    def test_hung_platform_times_out_and_next_runs(self):
        class Source:
            def __init__(self,p):self.platform=p
            def search(self,space,query):
                if self.platform=='boss':time.sleep(10)
                return []
        cfg=CollectSettings(keywords=['AI'],cities=['苏州'],platform_timeout_seconds=.05,
              platforms={'boss':{'enabled':True},'zhilian':{'enabled':True}})
        started=time.monotonic()
        result=Collector(FakeBrowser(),[Source('boss'),Source('zhilian')],CollectionRepository(self.store),cfg).run()
        self.assertLess(time.monotonic()-started,1)
        self.assertEqual(result['platforms']['boss']['error'],'collection_timeout')
        self.assertNotIn('error',result['platforms']['zhilian'])

class NotificationTest(unittest.TestCase):
    def test_exact_telegram_login_instruction_no_body_or_url(self):
        from service.safety import telegram_alert
        config=Settings(notify={'enabled':True,'channels':{'tg':{'type':'telegram','enabled':True,'chat_id':'test'}}})
        with patch('notify.send_one',return_value={'success':True}) as send:
            telegram_alert(config,'zhilian','login_required')
        self.assertEqual(send.call_args.args[2],'请在 Ego 空间「job-agent 采集」里登录 智联')

    def test_ego_subprocess_timeout_is_bounded_by_collection_deadline(self):
        import subprocess
        from browser.ego import EgoBrowser
        from collect.deadline import platform_deadline,CollectionTimeout
        with patch('browser.ego.subprocess.run',side_effect=subprocess.TimeoutExpired('ego',.1)) as run:
            with self.assertRaises(CollectionTimeout):
                with platform_deadline(.1):EgoBrowser(keepalive_seconds=0)._call({'op':'space','name':'test'})
        self.assertLessEqual(run.call_args.kwargs['timeout'],.1)

class MigrationTest(unittest.TestCase):
    def test_backup_first_legacy_values_and_non_target_config_preserved(self):
        import importlib.util
        import yaml
        path=Path(__file__).resolve().parents[2]/'scripts/migrate-rate-config.py'
        spec=importlib.util.spec_from_file_location('migrate_rate',path)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as tmp:
            config=Path(tmp)/'config.yaml'
            data={'engine':{'platforms':{'boss':{'daily_limit':200,'interval_seconds':30}}},
                  'collect':{'interval_seconds':[8,15],'platforms':{'boss':{'enabled':True}}},
                  'notify':{'enabled':False},'browser':{'keepalive_seconds':300}}
            original=yaml.safe_dump(data)
            config.write_text(original)
            with patch('sys.argv',['migrate','--config',str(config),'--apply']):module.main()
            self.assertEqual(next(Path(tmp).glob('config.yaml.backup-*')).read_text(),original)
            updated=yaml.safe_load(config.read_text())
            self.assertEqual(updated['engine']['platforms']['boss']['interval_seconds'],30)
            self.assertEqual(updated['engine']['platforms']['boss']['daily_limit'],200)
            self.assertEqual(updated['engine']['platforms']['boss']['rate']['interval_seconds'],[45,120])
            self.assertEqual(updated['notify'],data['notify'])
            self.assertEqual(updated['browser'],data['browser'])
            self.assertEqual(updated['collect']['interval_seconds'],[8,15])

class RiskAndCleanupTest(unittest.TestCase):
    def test_new_pause_during_login_check_cannot_be_cleared(self):
        with tempfile.TemporaryDirectory() as tmp,Store(Path(tmp)/'jobs.sqlite3') as store:
            safety=PlatformSafety(FakeBrowser(),store,Settings(),alert=lambda *a:None)
            safety.repo.pause('boss')
            def inspect(b,p,s):
                safety.repo.pause(p)
                return SessionResult(p,True,'',True)
            safety.check=inspect
            result=safety.resume('boss')
            self.assertFalse(result['resumed'])
            self.assertEqual(result['reason'],'pause_changed')
            self.assertIn('boss',safety.repo.pauses())
    def test_login_notification_failure_still_skips_platform_with_visible_failure(self):
        from engine.rate import RateDeferred
        with tempfile.TemporaryDirectory() as tmp,Store(Path(tmp)/'jobs.sqlite3') as store:
            safety=PlatformSafety(FakeBrowser(),store,Settings(),check=lambda b,p,s:SessionResult(p,False,'login_required'),
               alert=lambda *a:(_ for _ in ()).throw(RuntimeError('private provider error')))
            with self.assertRaises(RateDeferred) as error:safety.require('zhilian')
            self.assertEqual(error.exception.code,'login_required')
            self.assertTrue(error.exception.alert_failed)
    def test_live_queue_login_skip_has_no_attempt_and_other_platform_continues(self):
        from store import SQLiteDeliveryStore
        from service.deliver import preview,confirm
        from engine.queue import DeliveryEngine
        from engine.models import Status,Reason
        import json
        config=Settings(engine={'backend':'own','dry_run':False,'live_send_enabled':True,'platforms':{
            p:{'enabled':True,'greeting':'hi','greeting_max_length':500,'rate':{'interval_seconds':[0,0],'allowed_hours':['00:00','23:59']}}
            for p in ('boss','zhilian')}})
        with tempfile.TemporaryDirectory() as tmp,SQLiteDeliveryStore(Path(tmp)/'live.sqlite3',dry_run=False) as store:
            ids=[store.upsert_job(platform=p,platform_job_id='test-'+p,url='https://example.invalid/'+p) for p in ('zhilian','boss')]
            token=json.loads(preview(store,config,job_ids=ids))['token']
            from engine.rate import RateDeferred
            def require(safety,platform):
                if platform=='zhilian':raise RateDeferred('login_required')
            def deliver(engine,job,settings,space):store.update(job,Status.SENT,Reason.NONE,1795654800)
            with patch.object(PlatformSafety,'require',require),patch.object(DeliveryEngine,'_deliver',deliver),patch('browser.ego.EgoBrowser.shutdown'):
                text=confirm(store,config,token)
            records={r.job.platform:r for r in store.records()}
            self.assertEqual(records['zhilian'].status,Status.PENDING)
            self.assertIsNone(records['zhilian'].attempted_at)
            self.assertEqual(records['boss'].status,Status.SENT)
            self.assertIn('待续：未登录',text)

class EntryPointTest(unittest.IsolatedAsyncioTestCase):
    async def test_setup_check_mcp_exposes_login_results_without_modifying_database(self):
        import yaml
        import server
        with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{'JOB_AGENT_HOME':tmp}):
            home=Path(tmp)
            home.joinpath('config.yaml').write_text(yaml.safe_dump({'engine':{'backend':'own','platforms':{'zhilian':{'enabled':True,'greeting_max_length':500}}}}))
            with Store() as store:pass
            before=home.joinpath('jobs.sqlite3').read_bytes()
            browser=FakeBrowser()
            browser.executable='/nonexistent/ego-browser'
            with patch('browser.ego.EgoBrowser',return_value=browser),patch('platforms.session.check_session',return_value=SessionResult('zhilian',False,'login_required')):
                data=(await server.mcp.call_tool('setup_check',{}))[1]
            self.assertEqual(data['checks']['platform_sessions']['platforms']['zhilian']['reason'],'login_required')
            self.assertEqual(before,home.joinpath('jobs.sqlite3').read_bytes())

    async def test_manual_resume_cli_exit_and_result(self):
        from cli import main
        with patch('service.safety.resume_platform',return_value={'resumed':False,'reason':'login_required'}) as resume:
            self.assertEqual(main(['resume-platform','zhilian']),1)
        resume.assert_called_once_with('zhilian')
