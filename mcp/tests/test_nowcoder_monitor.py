import unittest
from unittest.mock import Mock
from job_agent.browser.base import FakePage,FakeSpace,RiskControl
from job_agent.platforms.nowcoder import messages,pages
from job_agent.platforms.nowcoder.replies import ReplySource
from job_agent.monitor.delta import ConversationCursor

ROW={'id':1,'jobId':2,'hrId':3,'jobName':'AI','latestMsg':{'id':4,'createTime':1000000},'unreadCount':0}
PAYLOAD={'status':200,'code':0,'data':{'datas':[ROW],'totalCount':1,'totalPage':1}}
MSG={'status':200,'code':0,'owner_id':5,'conversation_id':'1','data':{'messageList':[{'id':4,'sender':3,'createTime':1000000,'content':'岗位沟通'}]}}

class MonitorTests(unittest.TestCase):
    def page(self,payload=PAYLOAD,msg=MSG):
        return FakePage('p1',{'nowcoder.guard':{'logged_in':True},'nowcoder.roster':payload,'nowcoder.messages':msg})

    def test_first_baseline_and_unchanged_never_reads_messages(self):
        store=Mock();store.monitor_baseline.return_value=None;store.monitor_conversations.return_value={}
        cursor=ConversationCursor(store,'nowcoder',24)
        p=self.page();self.assertEqual(ReplySource().fetch(FakeSpace('t',pages=[p]),cursor=cursor),[])
        self.assertFalse(any('nowcoder.messages' in str(x) for x in p.actions))
        self.assertTrue(p.closed)
        store.monitor_baseline.return_value=0
        store.monitor_conversations.return_value={'1':{'last_message_id':'4'}}
        p=self.page();cursor=ConversationCursor(store,'nowcoder',24)
        self.assertEqual(ReplySource().fetch(FakeSpace('t',pages=[p]),cursor=cursor),[])
        self.assertFalse(any('nowcoder.messages' in str(x) for x in p.actions))

    def test_changed_reads_once_and_acks_after_validation(self):
        cursor=Mock();cursor.select.return_value=True
        p=self.page();events=ReplySource().fetch(FakeSpace('t',pages=[p]),cursor=cursor)
        self.assertEqual([(e.message_id,e.platform_job_id,e.direction) for e in events],[('1:4','2','incoming')])
        cursor.acknowledge.assert_called_once()
        self.assertFalse(any(x[0] in ('click','fill','press') for x in p.actions))
        self.assertFalse(any('jobAssistant' in str(x) for x in p.actions))

    def test_wrong_identity_missing_latest_and_unknown_sender_never_ack(self):
        for patch in [{'conversation_id':'9'},{'data':{'messageList':[]}},{'data':{'messageList':[{'id':4,'sender':8,'createTime':1000000,'content':'x'}]}}]:
            cursor=Mock();cursor.select.return_value=True
            p=self.page(msg={**MSG,**patch})
            with self.assertRaises(pages.Unverified):ReplySource().fetch(FakeSpace('t',pages=[p]),cursor=cursor)
            cursor.acknowledge.assert_not_called();self.assertTrue(p.closed)

    def test_incomplete_or_duplicate_roster_fails_before_cursor(self):
        for data in [{'datas':[],'totalCount':1,'totalPage':1}, {'datas':[ROW,ROW],'totalCount':2,'totalPage':1}]:
            p=self.page(payload={**PAYLOAD,'data':data});cursor=Mock()
            with self.assertRaises(pages.Unverified):ReplySource().fetch(FakeSpace('t',pages=[p]),cursor=cursor)
            cursor.select.assert_not_called()

    def test_direction_and_since(self):
        data={**MSG,'data':{'messageList':[{**MSG['data']['messageList'][0],'sender':5}]}}
        self.assertEqual(messages.events(data,ROW)[0].direction,'outgoing')
        self.assertEqual(messages.events(data,ROW,1001),[])

    def test_api_risk(self):
        with self.assertRaises(RiskControl):messages.roster({'status':200,'code':1125})
        with self.assertRaises(pages.Unverified):messages.summary({**ROW,'unreadCount':None})
