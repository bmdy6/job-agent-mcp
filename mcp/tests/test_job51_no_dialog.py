"""No-dialog receipt fallback never submits or mistakes an online resume for a file."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from job_agent.browser.base import FakeBrowser, FakePage, FakeSpace, PageTimeout, RiskControl
from job_agent.engine.models import Job
from job_agent.platforms.job51 import pages, status
from job_agent.platforms.job51.apply import Job51Adapter
from test_adapters_b import job51
from test_job51_status import snapshot


class NoDialogTests(unittest.TestCase):
    def adapter(self):
        class NoDialogPage(FakePage):
            def wait_for(self, selector, **kwargs):
                if selector == pages.ROWS:
                    raise PageTimeout('missing attachment dialog')
                return super().wait_for(selector, **kwargs)
        p = NoDialogPage('p2', {'job51.job': job51(), 'job51.observe': True,
                                'job51.prepare_apply': True})
        space = FakeSpace('test', pages=[p])
        a = Job51Adapter(FakeBrowser({'test': space}), SimpleNamespace(greeting_max_length=800))
        a.open_job(Job('51job', '172037288', 'https://jobs.51job.com/all/172037288.html'), 'test')
        a.send_greeting('')
        return a, p, space

    def test_timeout_reads_application_and_keeps_online_resume_unverified(self):
        a, p, space = self.adapter()
        evidence = dict(application_confirmed=True, attachment_confirmed=False,
                        applied_at='2026-11-27', resume_name='我的简历1')
        with patch.object(status, 'read_application_receipt', return_value=evidence) as read:
            with self.assertRaisesRegex(pages.AttachmentUnverified, '我的申请已确认.*附件未确认'):
                a.choose_attachment('requested-file')
        read.assert_called_once_with(space, '172037288', a._attempted_at)
        self.assertEqual(a.application_receipt, evidence)
        self.assertFalse(a.confirm_receipt())
        self.assertEqual([x for x in p.actions if x[0] == 'click'], [('click', pages.APPLY)])
        with self.assertRaises(pages.Unverified):
            a.send_greeting('')

    def test_fallback_missing_or_failed_read_never_replays(self):
        for result in (None, PageTimeout('private contents'), RiskControl('challenge')):
            a, p, _ = self.adapter()
            with patch.object(status, 'read_application_receipt', **(
                    {'side_effect': result} if isinstance(result, Exception) else {'return_value': result})):
                expected = RiskControl if isinstance(result, RiskControl) else pages.AttachmentUnverified
                with self.assertRaises(expected) as raised:
                    a.choose_attachment('requested-file')
            self.assertNotIn('private contents', str(raised.exception))
            self.assertFalse(a.confirm_receipt())
            self.assertEqual([x for x in p.actions if x[0] == 'click'], [('click', pages.APPLY)])

    def test_no_fallback_for_non_timeout_error(self):
        a, p, _ = self.adapter()
        with patch.object(p, 'wait_for', side_effect=RuntimeError('broken transport')):
            with patch.object(status, 'read_application_receipt') as read:
                with self.assertRaises(pages.AttachmentUnverified):
                    a.choose_attachment('requested-file')
                read.assert_not_called()

    def test_no_fallback_after_attachment_send(self):
        from test_adapters_b import attachment, SequencePage
        class NoReceiptPage(SequencePage):
            def wait_for(self, selector, **kwargs):
                if selector == pages.ATTACHMENT_RECEIPT:
                    raise PageTimeout('receipt missing')
                return super().wait_for(selector, **kwargs)
        p = NoReceiptPage('p2', {'job51.job': job51(), 'job51.observe': True,
                                'job51.prepare_apply': True,
                                'job51.attachment': [attachment(), attachment(True)]})
        a = Job51Adapter(FakeBrowser({'test': FakeSpace('test', pages=[p])}),
                         SimpleNamespace(greeting_max_length=800))
        a.open_job(Job('51job', '172037288', 'https://jobs.51job.com/all/172037288.html'), 'test')
        a.send_greeting('')
        with patch.object(status, 'read_application_receipt') as read:
            with self.assertRaisesRegex(pages.AttachmentUnverified, 'wait_receipt'):
                a.choose_attachment('示例候选人_简历')
            read.assert_not_called()
        self.assertFalse(a.confirm_receipt())

    def test_original_list_receipt_matches_job_and_china_attempt_date(self):
        # 16:00 UTC is the next application date in China.
        attempted = 1795795200  # 2026-11-27T16:00:00Z
        from datetime import datetime
        from zoneinfo import ZoneInfo
        day = datetime.fromtimestamp(attempted, ZoneInfo('Asia/Shanghai')).date().isoformat()
        row = dict(snapshot()['rows'][0], appliedAt=day, resumeName='requested-file')
        with patch.object(status, 'read_applications', return_value=[row]) as read:
            receipt = status.read_application_receipt('space', '900001', attempted)
            self.assertTrue(receipt['application_confirmed'])
            # Even an identical online resume NAME is not a file delivery receipt.
            self.assertFalse(receipt['attachment_confirmed'])
            self.assertEqual(receipt['resume_name'], 'requested-file')
            read.assert_called_once_with('space')
        for rows in ([], [dict(row, id='900002')], [dict(row, appliedAt='2000-01-01')], [row, row]):
            with patch.object(status, 'read_applications', return_value=rows):
                self.assertIsNone(status.read_application_receipt('space', '900001', attempted))

    def test_fallback_reader_closes_history_page_on_success_and_risk(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        attempted = 1795795200
        day = datetime.fromtimestamp(attempted, ZoneInfo('Asia/Shanghai')).date().isoformat()
        data = snapshot()
        data['rows'][0].update(appliedAt=day, resumeName='我的简历1')
        p = FakePage('p3', {'job51.applications': data})
        receipt = status.read_application_receipt(FakeSpace('test', pages=[p]), '900001', attempted)
        self.assertTrue(receipt['application_confirmed'])
        self.assertTrue(p.closed)
        self.assertFalse(any(a[0] in ('click', 'fill', 'press') for a in p.actions))
        data['risk'] = True
        p = FakePage('p3', {'job51.applications': data})
        with self.assertRaises(pages.RiskControl):
            status.read_application_receipt(FakeSpace('test', pages=[p]), '900001', attempted)
        self.assertTrue(p.closed)

    def test_all_alias_pages_are_validated_without_relaxing_other_filters(self):
        data = snapshot()
        data.update(pages=2, next=status.APPLICATION_URL.replace('tagType=', 'tagType=all') + '&page=2')
        self.assertEqual(status.parse_application_page(data, '')[1], data['next'])
        data.update(page=2, next=None, tag='all')
        self.assertEqual(status.parse_application_page(data, '')[0], data['rows'])
        with self.assertRaises(pages.Unverified):
            status.parse_application_page(data, 'cy')

    def test_reconcile_reports_resume_name_but_never_promotes_it(self):
        import tempfile
        from pathlib import Path
        from job_agent.store import Store
        from job_agent.service.job51_reconcile import reconcile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'jobs.sqlite3'
            with Store(path):
                pass
            row = dict(snapshot()['rows'][0], resumeName='requested-file')
            before = path.read_bytes()
            item = reconcile([row], database=path)['items'][0]
            self.assertTrue(item['application_confirmed'])
            self.assertEqual(item['resume_name'], 'requested-file')
            self.assertFalse(item['attachment_confirmed'])
            self.assertEqual(path.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
