import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from datetime import datetime, timedelta, timezone
from job_agent.config import Settings
from job_agent.store import SQLiteDeliveryStore
from job_agent.engine.models import Status,Reason


class DailyFollowupTests(unittest.TestCase):
    def test_followup_after_new_delivery_preview_only_and_separate_summary(self):
        from job_agent.service.daily import daily_once,delivery_message
        from job_agent.service.followup import run
        with tempfile.TemporaryDirectory() as home,patch.dict('os.environ',JOB_AGENT_HOME=home):
            cfg=Settings(daily={'enabled':True,'collect_enabled':True,'deliver_enabled':True},
                followup={'enabled':True,'after_days':[1,1]},engine={'dry_run':False,'live_send_enabled':True,
                'platforms':{'boss':{'enabled':True,'greeting':'你好','greeting_max_length':1000,'interval_seconds':0,'daily_limit':20}}})
            with SQLiteDeliveryStore(Path(home)/'jobs.sqlite3',dry_run=False) as db:
                old=db.upsert_job(platform='boss',platform_job_id='old',url='https://example.invalid/old',company='旧公司',title='旧岗位')
                db.record_delivery(old,status='sent',created_at=(datetime.now(timezone.utc)-timedelta(days=2)).isoformat())
            calls=[]
            def collect(**kw):
                calls.append('collect')
                with SQLiteDeliveryStore(Path(home)/'jobs.sqlite3',dry_run=False) as db:
                    db.upsert_job(platform='boss',platform_job_id='new',url='https://example.invalid/new',company='新公司',title='新岗位')
                return {'platforms':{'boss':{'new':1}}}
            def confirm(db,cfg,token,**kw):
                calls.append('new_delivery')
                db.confirm_batch(token,config=cfg.engine.model_dump(),daily=True)
                db.prepare_run(cfg.engine.model_dump(),batch_token=token)
                for job in db.execution_jobs(token):
                    db.update(job,Status.UNVERIFIED,Reason.INTERRUPTED,datetime.now().timestamp())
                    db.update(job,Status.SENT,Reason.NONE)
                db.complete_batch(token)
                return ''
            def followups(*a,**kw):calls.append('followup');return run(*a,**kw)
            with patch('job_agent.service.followup.run',side_effect=followups),patch('job_agent.service.daily.send',return_value=[]),patch('job_agent.notify.send_one') as tg:
                report=daily_once(config=cfg,collect_fn=collect,confirm_fn=confirm)
            self.assertEqual(calls,['collect','new_delivery','followup'])
            self.assertEqual(report['followup']['items'][0]['company'],'旧公司')
            self.assertNotIn('message',report['followup']['items'][0])
            self.assertIn('跟进：仅预览，未发送',delivery_message(report)[1])
            tg.assert_not_called()
