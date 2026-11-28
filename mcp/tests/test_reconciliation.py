import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from job_agent.store import Store


class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'jobs.sqlite3'
        self.store = Store(self.path)
        self.addCleanup(self.store.close)
        self.jid = self.store.upsert_job(platform='zhilian', platform_job_id='J1',
            company='测试公司', title='工程师', url='https://www.zhaopin.com/jobdetail/J1.htm')
        self.store.record_delivery(self.jid, status='unverified', created_at='2026-11-27T02:00:00+00:00')

    def candidate(self):
        from job_agent.store.reconciliation import candidates
        return candidates(self.path, statuses=['unverified', 'error'])[0]

    def test_three_fixture_outcomes_and_ambiguity(self):
        from job_agent.platforms.reconciliation import application_outcome
        job = self.candidate()
        for name in ('sent_confirmed', 'not_sent', 'still_unknown'):
            fixture = json.loads((Path(__file__).parent/'fixtures'/'reconciliation'/f'{name}.json').read_text())
            result = application_outcome(job, fixture, checked_epoch=1795834800)
            self.assertEqual(result['status'], name)
            self.assertNotIn('url', json.dumps(result))
            self.assertNotIn('body', json.dumps(result))
        fixture['rows'] = []
        self.assertEqual(application_outcome(job, fixture, checked_epoch=1795834800)['status'], 'still_unknown')

    def artifact(self, status='sent_confirmed'):
        from job_agent.service.reconcile import artifact
        from job_agent.platforms.reconciliation import evidence
        return artifact([self.candidate()], [evidence(status, 'application_record' if status=='sent_confirmed' else 'no_matching_record', receipt_at=1795744800 if status=='sent_confirmed' else None)])

    def test_preview_append_idempotency_funnel_and_no_queue_rewrite(self):
        from job_agent.store.reconciliation import apply_results
        from job_agent.service.read_tools import jobs_from
        from job_agent.analytics import records_for, counts
        before = self.store.list_deliveries()
        report = self.artifact()
        self.assertFalse(apply_results(self.path, report)['applied'])
        self.assertEqual(self.store.list_deliveries(), before)
        self.assertTrue(apply_results(self.path, report, apply=True)['applied'])
        self.assertEqual(self.store.schema_version, 7)
        self.assertEqual(self.store.list_deliveries()[:len(before)], before)
        self.assertEqual(self.store.list_deliveries()[-1]['reason_code'], 'sent_confirmed')
        self.assertEqual(json.loads(self.store.list_deliveries()[-1]['error_detail'])['run_id'], report['run_id'])
        self.assertEqual(self.store.events()[-1]['run_id'], report['run_id'])
        self.assertEqual(jobs_from(self.store)[0]['status'], 'sent_confirmed')
        self.assertEqual(counts(records_for(jobs_from(self.store), []).values())['applied'], 1)
        self.assertEqual(apply_results(self.path, report, apply=True)['added'], 0)
        self.assertEqual(len(self.store.list_deliveries()), len(before)+1)

    def test_changed_history_rejects_and_invalid_artifact_cannot_partially_apply(self):
        from job_agent.store.reconciliation import apply_results
        report = self.artifact()
        self.store.record_delivery(self.jid, status='sent')
        before = self.store.list_deliveries()
        with self.assertRaises(ValueError):
            apply_results(self.path, report, apply=True)
        self.assertEqual(self.store.list_deliveries(), before)
        report['rows'][0]['evidence']['body'] = 'private'
        with self.assertRaises(ValueError):
            apply_results(self.path, report, apply=True)

    def test_missing_database_never_created(self):
        from job_agent.store.reconciliation import candidates
        missing = self.path.parent/'missing.sqlite3'
        with self.assertRaises(Exception):
            candidates(missing, statuses=['unverified'])
        self.assertFalse(missing.exists())

    def test_busy_lock_is_waited_without_browser_access(self):
        from job_agent.service.reconcile import wait_platform
        from job_agent.browser.platform_lock import PlatformBusy
        lock = Mock(side_effect=[PlatformBusy('busy'), Mock(__enter__=Mock(), __exit__=Mock())])
        sleep = Mock()
        with patch('job_agent.service.reconcile.platform_lock', lock):
            with wait_platform('boss', Path(self.tmp.name), sleep=sleep):
                self.assertEqual(sleep.call_count, 1)

    def test_boss_selects_existing_conversation_through_projected_click(self):
        from job_agent.platforms.reconciliation import OriginalSiteReader, boss_outcome
        job = {**self.candidate(), 'platform':'boss'}
        data = {'current_job_id':'J1','current_conversation_id':'C1','rows':[{
            'id':'M1','job_id':'J1','conversation_id':'C1','company':'测试公司','job_title':'工程师',
            'direction':'outgoing','receipt':'已读','time':job['attempted_at']}]}
        page = Mock()
        page.evaluate.side_effect = lambda script: data if 'reconcile.boss_messages' in script else {}
        reader = OriginalSiteReader(Mock())
        reader.page, reader.platform = page, 'boss'
        reader.cached = {('J1','C1'):({'encryptJobId':'J1','uniqueId':'C1','lastMsgId':'M1'},0)}
        self.assertEqual(reader.read(job)['status'],'sent_confirmed')
        page.click_projected.assert_called_once()
        page.click.assert_not_called()
        page.fill.assert_not_called()
        page.press.assert_not_called()
        for key,value in [('company','别的公司'),('job_title','其他岗位'),('time',job['attempted_at']-1000),('receipt','发送失败')]:
            changed={**data,'rows':[{**data['rows'][0],key:value}]}
            self.assertEqual(boss_outcome(job,changed,checked_epoch=1795834800)['status'],'still_unknown')

    def test_legacy_error_alias_and_negative_audit_never_requeues(self):
        from job_agent.store.reconciliation import candidates, apply_results
        from job_agent.store import SQLiteDeliveryStore
        self.store.upsert_job(platform='zhilian',platform_job_id='J1',legacy_status='error')
        self.store.record_delivery(self.jid,status='unverified',reason_code='legacy_error',kind='legacy_event')
        self.assertEqual(len(candidates(self.path,statuses=['error'])),1)
        from job_agent.service.reconcile import artifact
        from job_agent.platforms.reconciliation import evidence
        report=artifact([self.candidate()],[evidence('not_sent','explicit_not_applied')])
        apply_results(self.path,report,apply=True)
        with SQLiteDeliveryStore(self.path,dry_run=False) as store:
            self.assertTrue(store.history_blocked(self.jid))
            from job_agent.engine.models import Job
            job=Job('zhilian','J1','https://www.zhaopin.com/jobdetail/J1.htm')
            self.assertFalse(store.daily_rebindable(None,job,{}))

    def test_test_process_cannot_open_production_path(self):
        from job_agent.store.reconciliation import connect
        with patch('pathlib.Path.home',return_value=Path(self.tmp.name)):
            for writable in (False,True):
                with self.assertRaisesRegex(RuntimeError,'production'):
                    connect(Path(self.tmp.name)/'.job-agent'/'jobs.sqlite3',writable=writable)

    def test_service_artifact_is_private_readonly_and_stops_on_user_control(self):
        from job_agent.service.reconcile import reconcile
        from job_agent.browser.base import SpaceUnavailable
        from contextlib import nullcontext
        output=Path(self.tmp.name)/'result.json'
        before=self.store.list_deliveries()
        browser=Mock()
        reader=Mock()
        reader.read.side_effect=SpaceUnavailable('user control')
        with patch('job_agent.service.reconcile.wait_platform',return_value=nullcontext()):
            result=reconcile(self.path,output,browser=browser,reader_factory=lambda space:reader)
        self.assertEqual(result['total'],1)
        self.assertTrue(result['read_only'])
        self.assertEqual(json.loads(output.read_text())['rows'][0]['evidence']['code'],'task_space')
        self.assertEqual(output.stat().st_mode & 0o777,0o600)
        self.assertEqual(self.store.list_deliveries(),before)
        reader.close.assert_not_called()
        browser.shutdown.assert_called_once_with('job-agent 对账',finish=False)

    def test_whole_batch_conflict_leaves_first_item_unapplied(self):
        from job_agent.store.reconciliation import candidates,apply_results
        from job_agent.service.reconcile import artifact
        from job_agent.platforms.reconciliation import evidence
        other=self.store.upsert_job(platform='zhilian',platform_job_id='J2')
        self.store.record_delivery(other,status='unverified')
        jobs=candidates(self.path,statuses=['unverified'])
        report=artifact(jobs,[evidence('still_unknown','no_matching_record') for _ in jobs])
        self.store.record_delivery(other,status='sent')
        before=self.store.list_deliveries()
        with self.assertRaises(ValueError):
            apply_results(self.path,report,apply=True)
        self.assertEqual(self.store.list_deliveries(),before)
        self.assertEqual(self.store.events(),[])

    def test_paused_platform_never_opens_original_site(self):
        from job_agent.service.reconcile import reconcile
        from contextlib import nullcontext
        self.store.pause_platform('zhilian')
        browser=Mock()
        with patch('job_agent.service.reconcile.wait_platform',return_value=nullcontext()):
            reconcile(self.path,Path(self.tmp.name)/'paused.json',browser=browser)
        browser.space.assert_not_called()


if __name__ == '__main__':
    unittest.main()
