"""Status rows below are synthetic; the live account currently has no HR status."""
import os
import tempfile
import unittest
from unittest.mock import patch
from browser.base import FakePage, FakeSpace
from config import Settings
from monitor import Monitor
from platforms.job51 import status as pages
from platforms.job51.replies import Job51ReplySource
from store import Store
from browser.base import FakeBrowser


def snapshot(tag='', rows=None):
    return dict(loggedIn=True, login=False, risk=False, ready=True, tag=tag,
                page=1, pages=1, empty=False, rows=rows if rows is not None else [
                    dict(id='900001', title='示例岗位', company='示例公司', appliedAt='2026-11-24',
                         statuses=[], url='https://jobs.51job.com/all/900001.html')])


class StatusTests(unittest.TestCase):
    def test_explicit_empty_only_and_fail_closed(self):
        for tag in ('', 'cy', 'xq', 'ms'):
            data=snapshot(tag, []); data['empty']=True; data['page']=data['pages']=0
            self.assertEqual(pages.parse_application_page(data, tag), ([], None))
            data['empty']=False
            with self.assertRaises(pages.Unverified): pages.parse_application_page(data, tag)
        for field,value,kind in [('loggedIn',False,pages.LoginRequired), ('risk',True,pages.RiskControl),
                                  ('ready',False,pages.Unverified), ('tag','ms',pages.Unverified)]:
            data=snapshot(); data[field]=value
            with self.assertRaises(kind): pages.parse_application_page(data, '')

    def test_validate_identity_dates_pagination(self):
        for field,value in [('id',''),('company',''),('title',''),('appliedAt','yesterday'),
                            ('url','https://example.com/900001.html'),('statuses',['未知状态'])]:
            data=snapshot();data['rows'][0][field]=value
            with self.assertRaises(pages.Unverified):pages.parse_application_page(data,'')
        data=snapshot();data['pages']=2
        with self.assertRaises(pages.Unverified):pages.parse_application_page(data,'')
        data['next']='https://i.51job.com/userset/my_apply.php?type=sh&tagType=&pageno=2'
        self.assertEqual(pages.parse_application_page(data,'')[1],data['next'])
        data['next']='https://example.com/userset/my_apply.php'
        with self.assertRaises(pages.Unverified):pages.parse_application_page(data,'')

    def test_status_events_are_observations_and_stable(self):
        row=snapshot()['rows'][0]
        data={'': [row], 'cy':[row], 'xq':[], 'ms':[]}
        first=pages.application_events(data,observed_at=1795560000)
        second=pages.application_events(data,observed_at=1795560300)
        self.assertEqual(len(first),1)
        self.assertEqual(first[0].message_id,second[0].message_id)
        self.assertEqual(first[0].extra['id_origin'],'application_status')
        self.assertIn('51job 状态变化，没有正文',first[0].body)
        self.assertIn('已查阅',first[0].body)
        self.assertIn('原站未提供状态发生时间',first[0].body)
        self.assertEqual(first[0].occurred_at,1795560000)
        self.assertEqual(pages.application_events({'':[row],'cy':[],'xq':[],'ms':[]},observed_at=1795560000),[])
        self.assertEqual(pages.application_events(data, observed_at=1795560000, since=1795560000), [])
        data['cy'][0]=dict(row,company='错误公司')
        with self.assertRaises(pages.Unverified):pages.application_events(data,observed_at=1795560000)

    def test_source_reads_filters_only_and_closes_on_failure(self):
        class FilterPage(FakePage):
            def evaluate(self,js):
                from urllib.parse import urlsplit,parse_qs
                tag=parse_qs(urlsplit(self._url).query).get('tagType',[''])[0]
                data=snapshot(tag, []);data.update(empty=True,page=0,pages=0)
                return data
        page=FilterPage('p2')
        self.assertEqual(Job51ReplySource().fetch(FakeSpace('test',pages=[page])),[])
        self.assertEqual(len([a for a in page.actions if a[0]=='goto']),4)
        self.assertFalse(any(a[0] in ('click','fill','press') for a in page.actions))
        self.assertTrue(page.closed)
        page=FakePage('p3',{'job51.applications':dict(loggedIn=True,ready=False)})
        with self.assertRaises(pages.Unverified):Job51ReplySource().fetch(FakeSpace('test',pages=[page]))
        self.assertTrue(page.closed)

    def test_pagination_progress_and_structure_failures(self):
        class RepeatedPage(FakePage):
            def evaluate(self,js):
                data=snapshot();data.update(pages=2,next='https://i.51job.com/userset/my_apply.php?tagType=&pageno=2')
                return data
        page=RepeatedPage('p2')
        with self.assertRaises(pages.Unverified):Job51ReplySource().fetch(FakeSpace('test',pages=[page]))
        self.assertTrue(page.closed)
        data=snapshot();data['rows'][0]['statuses']=['不匹配']
        rows,_=pages.parse_application_page(data,'')
        events=pages.application_events({'':rows,'cy':[],'xq':[],'ms':[]},observed_at=1795560000)
        self.assertEqual(len(events),1)
        self.assertIn('不匹配',events[0].body)

    def test_notification_dedup_restart_and_unknown_job(self):
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ,JOB_AGENT_HOME=home), \
                patch('notify.send_one',return_value={'success':True}) as sender:
            store=Store();self.addCleanup(store.close)
            job=store.upsert_job(platform='51job',platform_job_id='900001',company='示例公司',title='示例岗位')
            store.record_delivery(job,status='sent')
            config=Settings.model_validate({'notify':{'enabled':True,'channels':{'tg':{'type':'telegram','enabled':True,'chat_id':'fake'}}}})
            row=snapshot()['rows'][0]
            import time
            events=pages.application_events({'':[row],'cy':[row],'xq':[],'ms':[]},observed_at=time.time())
            class Source:
                platform='51job'
                def fetch(self,space,**kwargs):return events
            self.assertEqual(Monitor(FakeBrowser(),[Source()],store,config).poll_once()['sent'],1)
            self.assertEqual(Monitor(FakeBrowser(),[Source()],store,config).poll_once()['sent'],0)
            self.assertEqual(sender.call_count,1)
            title,text=sender.call_args.args[1:3]
            self.assertIn('51job 状态变化，没有正文',title)
            self.assertNotIn('HR 回复',text)
            self.assertEqual(store.list_replies()[0]['id_origin'],'application_status')
            # A different status creates an independent record; unknown jobs never notify.
            events[:] = pages.application_events({'':[row],'cy':[],'xq':[row],'ms':[]},observed_at=time.time())
            sender.return_value={'success':False}
            self.assertEqual(Monitor(FakeBrowser(),[Source()],store,config).poll_once()['failed'],1)
            sender.return_value={'success':True}
            self.assertEqual(Monitor(FakeBrowser(),[Source()],store,config).poll_once()['sent'],1)
            self.assertEqual(len(store.list_replies()),2)
            unknown=dict(row,id='900002',url='https://jobs.51job.com/all/900002.html')
            events[:] = pages.application_events({'':[unknown],'cy':[unknown],'xq':[],'ms':[]},observed_at=time.time())
            self.assertEqual(Monitor(FakeBrowser(),[Source()],store,config).poll_once()['added'],0)
            self.assertEqual(len(store.list_replies()),2)


if __name__=='__main__':unittest.main()
