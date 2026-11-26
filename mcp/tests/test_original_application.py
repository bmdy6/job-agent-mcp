import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from service.original_application import evaluate_application_record, review_resume_record
from store import Store

FIXTURES=Path(__file__).parent/'fixtures/platforms'


def fixture(platform,name='application-list'):
    return json.loads((FIXTURES/platform/(name+'.json')).read_text())


class OriginalApplicationTests(unittest.TestCase):
    def test_liepin_saved_official_record_is_only_historical_application_evidence(self):
        data=fixture('liepin')
        for row in data['items']:
            r=evaluate_application_record('liepin',row['href'],data)
            self.assertEqual(r['status'],'application_record_matched')
            self.assertTrue(r['application_record_matched'])
            self.assertFalse(r['site_origin_verified'])
            self.assertIsNone(r['current_attempt_confirmed'])
            self.assertIsNone(r['attachment_confirmed'])
            self.assertEqual(r['time_precision'],'minute')

    def test_liepin_contact_controls_and_text_cannot_prove_application(self):
        for name in ('applied-job','applied-lptjob','chat-only-job','employer-messages'):
            r=evaluate_application_record('liepin','https://www.liepin.com/lptjob/90000001',fixture('liepin',name))
            self.assertEqual(r['status'],'not_verified')
            self.assertFalse(r['application_record_matched'])

    def test_job51_online_resume_label_is_not_attachment_receipt(self):
        data=fixture('job51');row=data['observedRow']
        r=evaluate_application_record('51job',row['jobHref'].split('?')[0],data)
        self.assertEqual(r['status'],'application_record_matched')
        self.assertEqual(r['time_precision'],'day')
        self.assertEqual(r['resume_label'],'申请简历：我的简历1')
        self.assertIsNone(r['attachment_confirmed'])
        self.assertIsNone(r['current_attempt_confirmed'])
        # A supplied arbitrary assertion about a file is never promoted.
        row['attachment_confirmed']=True
        row['resumeLabel']='申请简历：version.pdf'
        r=evaluate_application_record('51job',row['jobHref'],data)
        self.assertTrue(r['application_record_matched'])
        self.assertIsNone(r['attachment_confirmed'])

    def test_ambiguous_mismatched_or_incomplete_records_are_not_verified(self):
        lp=fixture('liepin')['items'][0]
        for evidence, url in (
                ({'items':[lp,lp]},lp['href']),
                ({'items':[lp]},'https://www.liepin.com/job/1990000001.shtml'),
                ({'items':[lp],'status':'已投递'},'https://other.invalid/lptjob/90000001'),
                (dict(lp,time='昨天'), lp['href']),
                (dict(lp,status='继续聊'), lp['href']),
                (dict(lp,href='https://www.liepin.com.evil.invalid/lptjob/90000001'),lp['href']),
                (dict(lp,href='https://www.liepin.com/job?id=one'),'https://www.liepin.com/job?id=two')):
            r=evaluate_application_record('liepin',url,evidence)
            self.assertEqual(r['status'],'not_verified')
            self.assertFalse(r['application_record_matched'])
            self.assertIsNone(r['attachment_confirmed'])
        j=fixture('job51')['observedRow']
        for change in ({'resumeLabel':'申请简历：'}, {'appliedAt':'昨日'}, {'resumeLabel':'我的简历1'}):
            r=evaluate_application_record('51job',j['jobHref'],dict(j,**change))
            self.assertFalse(r['application_record_matched'])

    def test_owned_record_review_preserves_all_state_and_pauses(self):
        data=fixture('job51')
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'jobs.sqlite3'
            with Store(path) as db:
                jid=db.upsert_job(platform='51job',platform_job_id='fixture',url=data['observedRow']['jobHref'])
                db.pause_platform('51job')
                db.record_delivery(jid,status='unverified',attachment_name='resume',reason_code='receipt_unknown',kind='attempt')
                before=db.list_deliveries()
                pause=db.paused_platforms()
                with patch('browser.ego.EgoBrowser',side_effect=AssertionError('Offline review cannot access sites')) as access:
                    r=review_resume_record(jid,data,database=path)
                access.assert_not_called()
                self.assertTrue(r['application_record_matched'])
                self.assertEqual(r['platform_paused'],'risk_control')
                self.assertEqual(db.list_deliveries(),before)
                self.assertEqual(db.paused_platforms(),pause)
                self.assertEqual(db.connection.execute('SELECT count(*) FROM delivery_queue').fetchone()[0],0)

    def test_cli_supplied_record_is_read_only(self):
        from cli import main
        from contextlib import redirect_stdout
        import io
        data=fixture('job51')
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'jobs.sqlite3'
            evidence=Path(tmp)/'record.json'
            evidence.write_text(json.dumps(data))
            with Store(path) as db:
                jid=db.upsert_job(platform='51job',platform_job_id='fixture',url=data['observedRow']['jobHref'])
                db.pause_platform('51job')
                out=io.StringIO()
                with redirect_stdout(out):
                    self.assertEqual(main(['review-resume-record','--job-id',str(jid),'--record-file',str(evidence),'--database',str(path)]),0)
                result=json.loads(out.getvalue())
                self.assertTrue(result['application_record_matched'])
                self.assertFalse(result['site_origin_verified'])
                self.assertEqual(db.paused_platforms()['51job'],'risk_control')
                self.assertEqual(db.list_deliveries(),[])

    def test_simulation_database_cannot_be_reviewed_as_real_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'jobs.sqlite3'
            with Store(path,dry_run=True) as db:
                jid=db.upsert_job(platform='liepin',platform_job_id='fixture',url='https://www.liepin.com/lptjob/1')
            with self.assertRaisesRegex(ValueError,'simulation'):
                review_resume_record(jid,{},database=path)


if __name__=='__main__':
    unittest.main()
