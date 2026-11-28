"""Sanitized regression coverage for opt-in historical receipt recovery."""
import copy
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from job_agent.store import Store
from job_agent.store.reconciliation import candidates, apply_results
from job_agent.service.reconcile import artifact
from job_agent.platforms.reconciliation import evidence


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)/'jobs.sqlite3'
        self.store = Store(self.path)
        self.addCleanup(self.store.close)
        self.jid = self.store.upsert_job(platform='51job', platform_job_id='900001', company='测试公司', title='工程师')
        self.store.record_delivery(self.jid, status='unverified', created_at='2026-11-27T02:00:00+00:00')

    def test_explicit_unknown_run_uses_latest_fingerprint_without_repeating_confirmed(self):
        jobs = candidates(self.path, statuses=['unverified'])
        report = artifact(jobs, [evidence('still_unknown','unsupported_platform')])
        apply_results(self.path, report, apply=True)
        self.assertEqual(candidates(self.path,statuses=['unverified']), [])
        jobs2 = candidates(self.path,statuses=['unverified'],retry_run=report['run_id'])
        self.assertEqual(len(jobs2),1)
        self.assertNotEqual(jobs2[0]['fingerprint'],jobs[0]['fingerprint'])
        self.assertGreater(jobs2[0]['latest_delivery_id'],jobs[0]['latest_delivery_id'])
        self.assertEqual(candidates(self.path,statuses=['unverified'],retry_run='0'*32),[])
        positive=artifact(jobs2,[evidence('sent_confirmed','application_record_day',receipt_at=1795708800)])
        apply_results(self.path,positive,apply=True)
        self.assertEqual(candidates(self.path,statuses=['unverified'],retry_run=positive['run_id']),[])
        self.assertEqual(candidates(self.path,statuses=['unverified'],retry_run=report['run_id']),[])

    def test_date_precision_receipt_apply_rejects_wrong_day_and_has_no_false_exact_time(self):
        from job_agent.platforms.reconciliation import job51_outcome
        job=candidates(self.path,statuses=['unverified'])[0]
        rows=[dict(id='900001',company='测试公司',title='工程师',appliedAt='2026-11-27',resumeName='private')]
        result=job51_outcome(job,rows)
        self.assertEqual(result['status'],'sent_confirmed')
        self.assertEqual(result['evidence']['code'],'application_record_day')
        stamp=datetime.fromtimestamp(result['evidence']['receipt_at'],ZoneInfo('Asia/Shanghai'))
        self.assertEqual(stamp.isoformat(),'2026-11-27T00:00:00+08:00')
        self.assertNotIn('private',json.dumps(result))
        report=artifact([job],[result])
        self.assertEqual(apply_results(self.path,report)['added'],1)
        bad=copy.deepcopy(report);bad['rows'][0]['evidence']['receipt_at']-=86400
        with self.assertRaisesRegex(ValueError,'date'):
            apply_results(self.path,bad)
        bad=copy.deepcopy(report);bad['rows'][0]['platform']='zhilian'
        with self.assertRaises(ValueError):
            apply_results(self.path,bad)
        for changed in ([],rows+rows,[dict(rows[0],title='其他岗位')],[dict(rows[0],company='其他公司')],
                        [dict(rows[0],id='900002')],[dict(rows[0],appliedAt='2026-11-26')]):
            self.assertEqual(job51_outcome(job,changed)['status'],'still_unknown')

    def test_job51_reader_reuses_official_parser_once_and_never_submits(self):
        from job_agent.platforms.reconciliation import OriginalSiteReader
        job=candidates(self.path,statuses=['unverified'])[0]
        reader=OriginalSiteReader(Mock())
        rows=[dict(id='900001',company='测试公司',title='工程师',appliedAt='2026-11-27')]
        with patch('job_agent.platforms.job51.status.read_applications',return_value=rows) as read:
            self.assertEqual(reader.read(job)['status'],'sent_confirmed')
            self.assertEqual(reader.read(job)['status'],'sent_confirmed')
            read.assert_called_once_with(reader.space)
        reader.space.new_page.assert_not_called()

    def test_retry_run_validation_and_cli_forwarding(self):
        from job_agent.cli import main
        for run in ('', 'x'*32, '../history'):
            with self.assertRaises(ValueError):
                candidates(self.path,statuses=['unverified'],retry_run=run)
        with patch('job_agent.service.reconcile.reconcile',return_value={'read_only':True}) as run:
            main(['reconcile','--database',str(self.path),'--output',str(self.path.parent/'out.json'),
                  '--retry-run','a'*32,'--platform','51job'])
            self.assertEqual(run.call_args.kwargs['retry_run'],'a'*32)
            self.assertEqual(run.call_args.kwargs['platforms'],['51job'])

    def test_job51_risk_stops_remaining_reads_and_keeps_history_readonly(self):
        from contextlib import nullcontext
        from job_agent.service.reconcile import reconcile
        from job_agent.platforms.job51.pages import RiskControl
        other=self.store.upsert_job(platform='51job',platform_job_id='900002')
        self.store.record_delivery(other,status='unverified')
        before=self.store.list_deliveries()
        reader=Mock();reader.read.side_effect=RiskControl('challenge')
        with patch('job_agent.service.reconcile.wait_platform',return_value=nullcontext()):
            result=reconcile(self.path,self.path.parent/'risk.json',browser=Mock(),reader_factory=lambda space:reader)
        reader.read.assert_called_once()
        self.assertEqual(result['counts'],{'51job':{'still_unknown':2}})
        report=json.loads((self.path.parent/'risk.json').read_text())
        self.assertEqual({r['evidence']['code'] for r in report['rows']},{'risk_control'})
        self.assertEqual(self.store.list_deliveries(),before)

    def test_cleanup_failure_preserves_review_artifact_without_retrying_actions(self):
        from contextlib import nullcontext
        from job_agent.service.reconcile import reconcile
        from job_agent.browser.base import SpaceUnavailable
        reader=Mock();reader.read.return_value=evidence('still_unknown','no_matching_record')
        reader.close.side_effect=SpaceUnavailable('page gone')
        browser=Mock();browser.shutdown.side_effect=SpaceUnavailable('page gone')
        with patch('job_agent.service.reconcile.wait_platform',return_value=nullcontext()):
            result=reconcile(self.path,self.path.parent/'cleanup.json',browser=browser,reader_factory=lambda space:reader)
        self.assertEqual(result['total'],1)
        self.assertEqual(json.loads((self.path.parent/'cleanup.json').read_text())['counts'],{'51job':{'still_unknown':1}})
        reader.close.assert_called_once()
        browser.shutdown.assert_called_once()

    def test_unavailable_application_list_is_not_reopened_for_every_job(self):
        from job_agent.platforms.reconciliation import OriginalSiteReader
        from job_agent.browser.base import PageTimeout
        reader=OriginalSiteReader(Mock())
        job=candidates(self.path,statuses=['unverified'])[0]
        with patch('job_agent.platforms.job51.status.read_applications',side_effect=PageTimeout('unavailable')) as read:
            for _ in range(2):
                with self.assertRaises(PageTimeout):reader.read(job)
            read.assert_called_once()


if __name__=='__main__':
    unittest.main()
