import json,os,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
from config import Settings
from store import SQLiteDeliveryStore
from engine.models import Job,Status,Reason
from platforms.availability import wait_job_ready,JobUnavailable

class Round4Tests(unittest.TestCase):
    def test_four_closed_fixtures_do_not_wait_for_button(self):
        from platforms.registry import ADAPTERS
        for platform in ('boss','zhilian','liepin','51job'):
            data=json.loads((Path(__file__).parent/'fixtures/platforms'/('job51' if platform=='51job' else platform)/'closed.json').read_text())
            class Page:
                def evaluate(self,js):
                    import subprocess
                    script="globalThis.document={body:{innerText:"+json.dumps(data['text'])+"}}; console.log(JSON.stringify(await ("+js+")()));"
                    return json.loads(subprocess.check_output(['node','--input-type=module','-e',script],text=True))
                def wait_for(self,*a): raise AssertionError('Must not wait for Apply')
            with self.subTest(platform=platform),self.assertRaises(JobUnavailable):wait_job_ready(Page(),'button')

    def test_retry_preserves_history_and_blocks_second_uncertainty(self):
        with tempfile.TemporaryDirectory() as tmp,SQLiteDeliveryStore(Path(tmp)/'db',dry_run=False) as s:
            cfg=Settings(engine={'platforms':{'zhilian':{'enabled':True,'greeting':'hi','greeting_max_length':10}}}).engine
            j=Job('zhilian','abc','https://www.zhaopin.com/jobdetail/abc.htm')
            jid=s.upsert_job(platform=j.platform,platform_job_id=j.job_id,url=j.url,company='Company',title='Role')
            token=s.create_batch([j],{'zhilian':'hi'},'',cfg.model_dump());s.confirm_batch(token,config=cfg.model_dump())
            s.update(j,Status.UNVERIFIED,Reason.EFFECT_ERROR,123)
            old=[dict(r) for r in s.connection.execute('SELECT * FROM deliveries')]
            evidence=dict(status='not_applied',job_id=jid,platform_job_id='abc',attempted_at=123,latest_delivery_id=old[-1]['id'],checked_epoch=time.time())
            for bad in ({**evidence,'status':'not_verified'},{**evidence,'latest_delivery_id':0},{**evidence,'checked_epoch':0}):
                with self.assertRaises(ValueError):s.requeue_verified_not_applied(jid,bad)
            s.requeue_verified_not_applied(jid,evidence)
            self.assertEqual(old,[dict(r) for r in s.connection.execute('SELECT * FROM deliveries WHERE id<=?',(old[-1]['id'],))])
            self.assertFalse(s.history_blocked(jid))
            self.assertFalse(s.duplicate_for(j))
            self.assertEqual(s.uncertain_events(),[])
            token=s.create_batch([j],{'zhilian':'hi'},'',cfg.model_dump());s.confirm_batch(token,config=cfg.model_dump())
            s.update(j,Status.SKIPPED,Reason.COOLDOWN)
            skip=s.connection.execute('SELECT MAX(id) FROM deliveries').fetchone()[0]
            s.requeue_verified_not_applied(jid,{**evidence,'latest_delivery_id':skip,'checked_epoch':time.time()})
            self.assertFalse(s.history_blocked(jid))
            token=s.create_batch([j],{'zhilian':'hi'},'',cfg.model_dump());s.confirm_batch(token,config=cfg.model_dump())
            s.update(j,Status.UNVERIFIED,Reason.EFFECT_ERROR,124)
            self.assertTrue(s.history_blocked(jid))
            with self.assertRaises(ValueError):s.requeue_verified_not_applied(jid,evidence)

    def test_notification_secret_shared_fallback_and_environment_priority(self):
        from notify import secret,SendError
        with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{'JOB_AGENT_HOME':tmp},clear=True):
            p=Path(tmp)/'daily.env.json';p.write_text('{"TG_TEST":"from_file"}');p.chmod(0o600)
            self.assertEqual(secret('TG_TEST'),'from_file')
            os.environ['TG_TEST']='from_env';self.assertEqual(secret('TG_TEST'),'from_env')
            del os.environ['TG_TEST'];p.chmod(0o644)
            with self.assertRaises(SendError):secret('TG_TEST')

    def test_click_failure_exposes_safe_step(self):
        import subprocess
        from browser.ego import EgoBrowser
        from browser.base import BrowserError
        for step in ('target_recheck','mouse.down','mouse.up','post_click_observation'):
            response='__JOB_AGENT_RESULT__'+json.dumps({'ok':False,'code':'browser_error','step':step})
            with patch('browser.ego.subprocess.run',return_value=subprocess.CompletedProcess([],0,response,'')):
                with self.assertRaisesRegex(BrowserError,'click.'+step):
                    EgoBrowser(keepalive_seconds=0)._execute({'op':'page','method':'click'})

    def test_zhilian_result_popup_is_observed_without_another_submission(self):
        from unittest.mock import Mock
        from platforms.zhilian.apply import ZhilianAdapter
        from platforms.zhilian import pages
        a=ZhilianAdapter(Mock(),{})
        a.page=Mock();a.space=Mock();a.job=Job('zhilian','abc','https://www.zhaopin.com/jobdetail/abc.htm')
        a.identity={'platform_job_id':'abc','company':'Company','title':'Role'}
        a.check_applied=Mock(return_value=False)
        old=Mock(label='p1');new=Mock(label='p2',url='https://i.zhaopin.com/schedule')
        a.space.tabs.side_effect=[[old],[old,new]]
        a.space.inspect_page.return_value.evaluate.return_value={}
        a.send_greeting('hello')
        a.page.click.assert_called_once_with(pages.START)
        a.space.claim_page.assert_called_once()
        self.assertEqual(len(a.result_pages),1)
        a.close()
        a.space.inspect_page.return_value.close.assert_called_once()

    def test_daily_preview_keeps_expired_cooldown_policy(self):
        from service.deliver import preview
        with tempfile.TemporaryDirectory() as tmp,SQLiteDeliveryStore(Path(tmp)/'db',dry_run=False) as s:
            cfg=Settings(daily={'enabled':True,'deliver_enabled':True},engine={'platforms':{'boss':{'enabled':True,'greeting':'hi','greeting_max_length':10}}})
            j=Job('boss','abc','https://www.zhipin.com/job_detail/abc.html')
            jid=s.upsert_job(platform=j.platform,platform_job_id=j.job_id,url=j.url,company='Company',title='Role')
            token=s.create_batch([j],{'boss':'hi'},'',cfg.engine.model_dump());s.confirm_batch(token,config=cfg.engine.model_dump())
            s.update(j,Status.SENT,Reason.NONE,time.time()-40*86400)
            with self.assertRaises(ValueError):preview(s,cfg,job_ids=[jid])
            self.assertEqual(len(json.loads(preview(s,cfg,job_ids=[jid],daily=True))['jobs']),1)
            s.record_delivery(jid,status='unverified')
            with self.assertRaises(ValueError):preview(s,cfg,job_ids=[jid],daily=True)
