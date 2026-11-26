import unittest
import tempfile
import time
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
from browser.base import FakePage,FakeSpace,FakeBrowser
from platforms.zhilian import pages as zl
from platforms.boss import pages as boss
from service.receipt_audit import audit_zhilian,audit_boss,verify_uncertain


class ReceiptAuditTests(unittest.TestCase):
    def setUp(self):
        self.at=datetime(2026,11,25,12,23,39,tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
        self.job={'platform_job_id':'fixture-id','company':'甲公司','title':'AI 工程师','attempted_at':self.at-2}
        self.row={'url':'http://jobs.zhaopin.com/fixture-id.htm?tracking=fixture','company':'甲公司',
                  'title':'AI 工程师','status':'成功投递','time':'2026-11-25 12:23:39'}

    def projection(self, rows):
        return {'origin':'https://i.zhaopin.com','path':'/schedule','rows':rows}

    def test_application_requires_identity_fresh_time_and_unique_positive_record(self):
        expected='2026-11-25T12:23:39+08:00'
        self.assertEqual(zl.fresh_application_record(self.projection([self.row]),self.job,self.at-2,self.at+2),expected)
        for change in ({'company':'乙'},{'title':'其他'},{'url':'https://other.example/fixture-id.htm'},
                       {'url':'https://jobs.zhaopin.com/other.htm'},{'status':'待投递'},
                       {'time':'2026-11-24 12:23:39'},{'time':'2026-11-25 12:25:00'},{'time':'12:23'}):
            self.assertIsNone(zl.fresh_application_record(self.projection([dict(self.row,**change)]),
                self.job,self.at-2,self.at+2))
        self.assertIsNone(zl.fresh_application_record(self.projection([self.row,self.row]),self.job,self.at-2,self.at+2))
        bad=dict(self.projection([self.row]),origin='https://other.example')
        self.assertIsNone(zl.fresh_application_record(bad,self.job,self.at-2,self.at+2))

    def test_zhilian_audit_is_read_only_and_does_not_infer_greeting(self):
        page=FakePage('owned',{'zhilian.state':{},'zhilian.application_records':self.projection([self.row])})
        result=audit_zhilian(FakeSpace('audit',pages=[page]),self.job,clock=lambda:self.at+2)
        self.assertEqual(result['status'],'application_confirmed')
        self.assertTrue(result['application_confirmed'])
        self.assertIsNone(result['greeting_confirmed'])
        self.assertIsNone(result['attachment_confirmed'])
        self.assertTrue(page.closed)
        self.assertFalse(any(a[0] in ('click','fill','press') for a in page.actions))
        self.assertEqual(len([a for a in page.actions if a[0]=='goto']),1)

    def test_risk_is_not_interpreted_as_an_application(self):
        page=FakePage('owned',{'zhilian.state':{'risk':True},'zhilian.application_records':self.projection([self.row])})
        with self.assertRaises(zl.RiskControl):
            audit_zhilian(FakeSpace('audit',pages=[page]),self.job,clock=lambda:self.at+2)
        self.assertTrue(page.closed)
        self.assertFalse(any(a[0] in ('click','fill','press') for a in page.actions))

    def test_boss_requires_fresh_exact_outgoing_message(self):
        row={'id':'message-1','job_id':'fixture-id','conversation_id':'conversation','direction':'outgoing',
             'body':'全文','receipt':'送达','time':self.at}
        def verified(candidate):
            return boss.fresh_greeting_receipt({'rows':[candidate]},'fixture-id','conversation','全文',self.at-2,self.at+2)
        self.assertTrue(verified(row))
        for change in ({'job_id':'wrong'},{'conversation_id':'wrong'},{'body':'全'},
                       {'receipt':'发送中'},{'direction':'incoming'},{'card':True},{'time':self.at-100}):
            self.assertFalse(verified(dict(row,**change)))

    def test_zhilian_fresh_exact_greeting_is_independent_of_application(self):
        from platforms.zhilian.receipt_reader import read_greeting
        job=dict(self.job,greeting='全文')
        row={'msg':{'idServer':'m','fromMe':True,'flow':'out','time':self.at,
                    'type':'text','body':'全文','sendState':'success'},
             'session':{'sessionId':'session','jobNumber':'fixture-id'}}
        self.assertTrue(zl.fresh_greeting_receipt({'messages':[row]},job,'session',self.at+2))
        for change in ({'time':self.at-100},{'body':'其他'},{'sendState':'pending'},{'fromMe':False}):
            bad=dict(row,msg=dict(row['msg'],**change))
            self.assertFalse(zl.fresh_greeting_receipt({'messages':[bad]},job,'session',self.at+2))
        self.assertFalse(zl.fresh_greeting_receipt({'messages':[row]},job,'different',self.at+2))
        page=FakePage('owned',{'zhilian.state':{},'zhilian.list':{'rows':[{'sessionId':'session','jobNumber':'fixture-id'}]},
            'zhilian.select_existing_session':True,'zhilian.messages':{'messages':[row]}})
        self.assertTrue(read_greeting(FakeSpace('audit',pages=[page]),job,clock=lambda:self.at+2))
        self.assertTrue(page.closed)
        self.assertFalse(any(a[0] in ('click','fill','press') for a in page.actions))

    def test_boss_first_message_requires_visible_and_projected_state(self):
        d={'current_job_id':'fixture-id','rows':[]}
        readiness={'root':True,'editor':True,'input_empty':True,'loading':False,'visible_messages':0}
        self.assertTrue(boss.first_message_ready(readiness,d,'fixture-id','session'))
        for change in ({'loading':True},{'input_empty':False},{'visible_messages':1},{'editor':False}):
            self.assertFalse(boss.first_message_ready(dict(readiness,**change),d,'fixture-id','session'))
        for url in ('http://www.zhipin.com/web/geek/chat','https://www.zhipin.com.evil.test/web/geek/chat',
                    'https://www.zhipin.com/web/geek/chat/other','https://www.zhipin.com:444/web/geek/chat'):
            self.assertFalse(boss.is_chat_url(url))
        self.assertTrue(boss.is_chat_url('https://www.zhipin.com/web/geek/chat?fixture=1'))

    def test_boss_empty_chat_does_not_trigger_submission_or_claim_send(self):
        page=FakePage('owned',{'boss.state':{},'boss.list':{'rows':[
            {'encryptJobId':'fixture-id','uniqueId':'session','lastMsgId':''}]}})
        result=audit_boss(FakeSpace('audit',pages=[page]),dict(self.job,greeting='全文'),clock=lambda:self.at+2)
        self.assertEqual(result,{'status':'not_verified','detail':'chat_without_stable_message'})
        self.assertTrue(page.closed)
        self.assertFalse(any(a[0] in ('click','fill','press') for a in page.actions))

    def test_paused_queue_not_opened_and_uncertain_history_unchanged(self):
        from store import SQLiteDeliveryStore
        from config import Settings
        with tempfile.TemporaryDirectory() as tmp:
            home=Path(tmp)
            with SQLiteDeliveryStore(home/'jobs.sqlite3',dry_run=False) as store:
                jid=store.upsert_job(platform='zhilian',platform_job_id='fixture-id',url='https://www.zhaopin.com/fixture-id.htm')
                store.record_delivery(jid,status='unverified',reason_code='effect_error')
                store.connection.execute("INSERT INTO delivery_queue(job_id,status,attempted_at) VALUES(?,'unverified',?)",(jid,time.time()))
                store.pause_platform('zhilian')
                before=store.list_deliveries(jid)
            browser=FakeBrowser()
            result=verify_uncertain(jid,config=Settings(),home=home,browser=browser)
            self.assertEqual(result['status'],'paused')
            self.assertEqual(browser.spaces,{})
            with SQLiteDeliveryStore(home/'jobs.sqlite3',dry_run=False) as store:
                self.assertEqual(store.list_deliveries(jid),before)
                self.assertEqual(store.connection.execute('SELECT status FROM delivery_queue WHERE job_id=?',(jid,)).fetchone()[0],'unverified')
