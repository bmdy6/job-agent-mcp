import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from job_agent.browser.base import FakePage, FakeSpace
from job_agent.platforms.job51 import status
from job_agent.store import Store
from test_job51_status import snapshot


class ReconcileTests(unittest.TestCase):
    def test_summary_distinguishes_attachment_from_greeting(self):
        from job_agent.service.deliver import summary
        from job_agent.engine.models import Job, Record, Status
        text = summary([Record(Job('51job', '900001', 'https://example.com'), Status.SENT)], dry_run=False)
        self.assertIn('指定附件回执已确认', text)
        self.assertIn('未发送招呼语', text)

    def test_reader_follows_only_validated_pages_and_closes(self):
        class Page(FakePage):
            def evaluate(self, js):
                data = snapshot()
                if 'pageno=2' in self._url:
                    data.update(page=2, pages=2)
                    data['rows'][0].update(id='900002', url='https://jobs.51job.com/all/900002.html')
                else:
                    data.update(pages=2, next=status.APPLICATION_URL + '&pageno=2')
                return data
        page = Page('p2')
        rows = status.read_applications(FakeSpace('test', pages=[page]))
        self.assertEqual([r['id'] for r in rows], ['900001', '900002'])
        self.assertTrue(page.closed)
        self.assertFalse(any(a[0] in ('click', 'fill', 'press') for a in page.actions))

    def test_reader_fails_closed_on_risk_and_repeated_pagination(self):
        for data in (dict(snapshot(), risk=True), dict(snapshot(), page=2)):
            page = FakePage('p2', {'job51.applications': data})
            with self.assertRaises(status.Unverified):
                status.read_applications(FakeSpace('test', pages=[page]))
            self.assertTrue(page.closed)

    def test_reader_rejects_list_shrinking_during_pagination(self):
        class ShrinkingPage(FakePage):
            def evaluate(self, js):
                data = snapshot()
                if 'pageno=2' in self._url:
                    data.update(rows=[], empty=True, page=0, pages=0)
                else:
                    data.update(pages=2, next=status.APPLICATION_URL+'&pageno=2')
                return data
        page = ShrinkingPage('p2')
        with self.assertRaises(status.Unverified):
            status.read_applications(FakeSpace('test', pages=[page]))
        self.assertTrue(page.closed)

    def test_reconciliation_does_not_treat_a_collected_job_as_application(self):
        from job_agent.service.job51_reconcile import reconcile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'jobs.sqlite3'
            with Store(path) as store:
                jid = store.upsert_job(platform='51job', platform_job_id='900001')
                # A failed precheck is not evidence that an application was submitted.
                store.record_delivery(jid, status='failed')
                store.record_delivery(jid, status='unverified', reason_code='receipt_unknown')
            before = path.read_bytes()
            report = reconcile(snapshot()['rows'], database=path)
            self.assertEqual(report['items'][0]['category'], 'site_only')
            self.assertEqual(report['items'][0]['local_job_id'], jid)
            self.assertEqual(report['site_only'], 1)
            self.assertEqual(path.read_bytes(), before)
            self.assertNotIn('url', report['items'][0])
            self.assertNotIn('company', report['items'][0])

    def test_distinct_attachment_attempt_legacy_and_unknown_evidence(self):
        from job_agent.service.job51_reconcile import reconcile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'jobs.sqlite3'
            with Store(path) as store:
                jid = store.upsert_job(platform='51job', platform_job_id='900001')
                store.record_delivery(jid, status='sent', attachment_name='synthetic', kind='legacy_snapshot')
                store.record_delivery(jid, status='unverified', kind='attempt')
                with store.transaction():
                    store.connection.execute('INSERT INTO delivery_attempts VALUES(?,?)', (jid,time.time()))
            row = snapshot()['rows'][0]
            row['appliedAt'] = datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
            item = reconcile([row], database=path)['items'][0]
            self.assertEqual(item['category'], 'local_attempt_unverified')
            self.assertFalse(item['attachment_confirmed'])
            self.assertIsNone(item['greeting_confirmed'])
            with Store(path) as store:
                store.record_delivery(jid, status='sent', attachment_name='synthetic', kind='attempt')
            self.assertEqual(reconcile([row], database=path)['items'][0]['category'], 'local_attachment_confirmed')
            row['appliedAt'] = '2020-01-01'
            self.assertEqual(reconcile([row], database=path)['items'][0]['category'], 'legacy_record_only')

    def test_missing_job_and_simulated_or_missing_db(self):
        from job_agent.service.job51_reconcile import reconcile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'jobs.sqlite3'
            with self.assertRaises(sqlite3.OperationalError):
                reconcile([], database=path)
            self.assertFalse(path.exists())
            with Store(path) as store:
                pass
            item = reconcile(snapshot()['rows'], database=path)['items'][0]
            self.assertEqual(item['category'], 'site_only')
            self.assertIsNone(item['local_job_id'])
            with closing(sqlite3.connect(path)) as db:
                db.execute('UPDATE store_mode SET dry_run=1')
                db.commit()
            with self.assertRaises(ValueError):
                reconcile([], database=path)

    def test_cli_is_readonly_and_reports_only_safe_fields(self):
        import job_agent.cli as cli
        with patch('job_agent.service.job51_reconcile.run', return_value={'read_only': True}) as run:
            self.assertEqual(cli.main(['reconcile-51job', '--database', '/fake/jobs.sqlite3']), 0)
            run.assert_called_once_with(database='/fake/jobs.sqlite3')


if __name__ == '__main__':
    unittest.main()
