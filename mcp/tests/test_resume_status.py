import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from browser.base import FakeBrowser
from config import Settings
from engine.models import Job, Reason, Status
from engine.queue import DeliveryEngine
from engine.resume_contract import resume_block_reason
from engine.spaces import TaskSpaces
from platforms.preflight import AdapterPreflight
from platforms.registry import create_adapters
from service.daily import daily_once, delivery_message
from service.deliver import preview
from service.resume_status import resume_status
from store import SQLiteDeliveryStore, Store


class ResumeStatusTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'jobs.sqlite3'
        self.store = Store(self.path)
        self.addCleanup(self.store.close)

    def job(self, p, key, **fields):
        return self.store.upsert_job(platform=p, platform_job_id=key, url='https://example.invalid/'+key,
            company='示例', title=key, **{'collected_at':'2026-11-25T03:00:00+00:00', **fields})

    def test_distinct_evidence_and_old_jobs_read_only(self):
        lp = self.job('liepin', 'contact')
        self.store.record_delivery(lp,status='sent',greeting='hello')
        legacy = self.job('liepin','legacy')
        self.store.record_delivery(legacy,status='sent',kind='legacy_event')
        pending = self.job('liepin','pending')
        uncertain = self.job('51job','unknown')
        self.store.record_delivery(uncertain,status='unverified',attachment_name='resume')
        sent = self.job('51job','confirmed')
        self.store.record_delivery(sent,status='sent',attachment_name='resume')
        old = self.job('51job','legacy-attachment')
        self.store.record_delivery(old,status='sent',kind='legacy_event',attachment_name='resume')
        failed = self.job('51job','preflight')
        self.store.connection.execute("INSERT INTO delivery_queue(job_id,status,reason_code) VALUES(?,'failed','precheck_error')",(failed,))
        later = self.job('51job','today',collected_at='2026-11-25T16:00:00+00:00')
        deleted = self.job('liepin','deleted',deleted_at='2026-11-25')
        self.store.pause_platform('liepin')
        self.store.pause_platform('51job')
        history = self.store.list_deliveries()
        pauses = self.store.paused_platforms()
        before_count = self.store.connection.execute('SELECT count(*) FROM delivery_queue').fetchone()[0]
        r = resume_status(database=self.path,before='2026-11-26',include_jobs=True)
        categories = {item['id']:item['receipt_status'] for item in r['items']}
        self.assertEqual(categories, {lp:'contact_only',legacy:'historical_sent_unverified',
            pending:'not_submitted',uncertain:'receipt_unverified',sent:'attachment_receipt_confirmed',
            old:'historical_sent_unverified',failed:'precheck_failed'})
        self.assertNotIn(later,categories); self.assertNotIn(deleted,categories)
        self.assertEqual(r['platforms']['liepin']['counts']['attachment_receipt_confirmed'],0)
        self.assertEqual(r['platforms']['51job']['counts']['attachment_receipt_confirmed'],1)
        self.assertEqual(r['platforms']['liepin']['paused'],'risk_control')
        self.assertEqual(history,self.store.list_deliveries())
        self.assertEqual(pauses,self.store.paused_platforms())
        self.assertEqual(before_count,self.store.connection.execute('SELECT count(*) FROM delivery_queue').fetchone()[0])
        limited=resume_status(database=self.path,before='2026-11-26',include_jobs=True,limit=1)
        self.assertEqual(len(limited['items']),1)
        self.assertTrue(limited['items_truncated'])
        self.assertEqual(limited['platforms'],r['platforms'])

    def test_never_use_dry_run_as_live_evidence(self):
        path=Path(self.tmp.name)/'dry.sqlite3'
        with Store(path,dry_run=True):
            pass
        with self.assertRaisesRegex(ValueError,'never a simulation'):
            resume_status(database=path)


class ResumeHoldTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'jobs.sqlite3'
        self.cfg=Settings(daily={'enabled':True,'collect_enabled':False,'deliver_enabled':True},
            engine={'backend':'own','dry_run':False,'live_send_enabled':True,
                'platforms':{p:dict(enabled=True,greeting='hello',greeting_max_length=20,daily_limit=2,interval_seconds=0)
                             for p in ('liepin','51job')}})
        self.store=SQLiteDeliveryStore(self.path,dry_run=False)
        self.addCleanup(self.store.close)
        self.ids=[self.store.upsert_job(platform=p,platform_job_id=p,url='https://example.invalid/'+p,
                    title=p,company='example') for p in ('liepin','51job')]

    def test_live_pending_before_browser_or_attempt(self):
        batch=json.loads(preview(self.store,self.cfg,job_ids=self.ids))
        self.assertEqual(batch['delivery_holds']['liepin']['resume_block'],'resume_flow_unverified')
        self.assertEqual(batch['delivery_holds']['51job']['resume_block'],'resume_attachment_required')
        token=batch['token']
        self.store.confirm_batch(token,config=self.cfg.engine.model_dump())
        browser=FakeBrowser()
        adapters=create_adapters(browser,self.cfg.engine)
        engine=DeliveryEngine(self.store,self.cfg.engine,adapters,AdapterPreflight(browser,adapters),TaskSpaces(browser))
        with patch.object(browser,'space',side_effect=AssertionError('Must not access site')) as access:
            records=engine.run(batch_token=token)
        access.assert_not_called()
        self.assertEqual({r.job.platform:r.reason for r in records},
            {'liepin':Reason.RESUME_FLOW_UNVERIFIED,'51job':Reason.RESUME_ATTACHMENT_REQUIRED})
        self.assertTrue(all(r.status==Status.PENDING and r.attempted_at is None for r in records))
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM delivery_attempts').fetchone()[0],0)

    def test_daily_reports_pause_and_missing_resume_without_confirm(self):
        self.store.pause_platform('liepin')
        confirm=Mock(side_effect=AssertionError('Must not confirm'))
        with patch.dict(os.environ,{'JOB_AGENT_HOME':self.tmp.name}):
            r=daily_once(config=self.cfg,confirm_fn=confirm)
        confirm.assert_not_called()
        self.assertEqual(r['selected'],[])
        self.assertEqual(r['delivery']['liepin']['stop_reason'],'risk_control_paused')
        self.assertEqual(r['delivery']['liepin']['resume_block_reason'],'resume_flow_unverified')
        self.assertEqual(r['delivery']['51job']['stop_reason'],'resume_attachment_required')
        message=delivery_message(r)[1]
        self.assertIn('正式简历流程尚未验收',message)
        self.assertIn('缺少明确配置附件',message)
        self.assertIn('不等于简历送达',message)
        self.assertIn('liepin',self.store.paused_platforms())

    def test_mcp_preview_reports_holds_without_consuming_confirmation(self):
        from service.mcp_backend import preview as mcp_preview, send
        with patch.dict(os.environ,{'JOB_AGENT_HOME':self.tmp.name}), \
             patch('service.mcp_backend.load_config',return_value=self.cfg), \
             patch('service.tasks.start') as start:
            result=mcp_preview([str(x) for x in self.ids])
            self.assertTrue(all(not x['delivery_enabled'] and x['delivery_hold'] for x in result['items']))
            with self.assertRaisesRegex(ValueError,'待核验'):
                send(result['confirm_token'])
            start.assert_not_called()
            from delivery import token_db
            with token_db() as db:
                self.assertEqual(db.execute('SELECT used FROM approvals').fetchone()[0],0)

    def test_exact_51job_attachment_does_not_clear_site_pause(self):
        self.cfg.engine.platforms['51job'].attachment_name='resume-v8.2'
        self.assertIsNone(resume_block_reason('51job',self.cfg.engine.platforms['51job']))
        self.store.pause_platform('51job')
        self.store.pause_platform('liepin')
        confirm=Mock()
        with patch.dict(os.environ,{'JOB_AGENT_HOME':self.tmp.name}):
            r=daily_once(config=self.cfg,confirm_fn=confirm)
        confirm.assert_not_called()
        self.assertEqual(r['delivery']['51job']['stop_reason'],'risk_control_paused')
        self.assertIn('51job',self.store.paused_platforms())


if __name__ == '__main__':
    unittest.main()
