import unittest
from job_agent.browser.base import FakeBrowser, FakePage, FakeSpace
from job_agent.platforms.boss import pages
from job_agent.platforms.boss.apply import BossAdapter
from job_agent.engine.models import Job

TEXT='你好，我还在关注这个岗位。有需要补充的材料或项目细节，随时找我就好。'


class BossFollowupTests(unittest.TestCase):
    def make(self):
        job=Job('boss','one','https://www.zhipin.com/job_detail/one.html')
        p=FakePage('owned',{'boss.state':{'buttons':[{'text':'继续沟通'}]},
            'boss.identity':{'company':'示例','title':'工程师'},
            'boss.list':{'rows':[{'encryptJobId':'one','uniqueId':'session','lastMsgId':'first'}]},
            'boss.select_session':True,
            'boss.chat_identity':{'job_id':'one','company':'示例','title':'工程师','header_title':'工程师','conversation_id':'session'},
            'boss.chat_ready':{'root':True,'editor':True,'loading':False,'input_empty':True,'visible_messages':1},
            'boss.messages':{'current_job_id':'one','current_conversation_id':'session','rows':[
                {'id':'first','job_id':'one','conversation_id':'session','direction':'outgoing','receipt':'送达','body':'首投'}]},
            '#chat-input':TEXT})
        a=BossAdapter(FakeBrowser({'test':FakeSpace('test',pages=[p])}),{'greeting_max_length':1000})
        a.open_job(job,'test');a.bind_delivery_kind('followup')
        return a,p

    def test_existing_chat_send_once_and_same_receipt_path(self):
        a,p=self.make()
        a.send_greeting(TEXT)
        self.assertNotIn(('click',pages.START),p.actions)
        self.assertEqual([x for x in p.actions if x[0]=='press'],[('press',pages.SEND)])
        self.assertFalse(a.confirm_receipt())  # Old first-contact receipt is insufficient.
        p.responses['boss.messages']['rows'].append({'id':'followup','job_id':'one','conversation_id':'session','direction':'outgoing','body':TEXT,'receipt':'送达'})
        self.assertTrue(a.confirm_receipt())
        with self.assertRaises(pages.PlatformStateError):a.send_greeting(TEXT)
        a.close();self.assertTrue(p.closed)

    def test_inbound_mismatch_loading_and_missing_chat_block(self):
        for failure in ('reply','identity','loading','missing','not_readback','unknown_receipt','new_message'):
            with self.subTest(failure=failure):
                a,p=self.make()
                if failure=='reply':p.responses['boss.messages']['rows'][0]['direction']='incoming'
                if failure=='identity':p.responses['boss.chat_identity']['job_id']='other'
                if failure=='loading':p.responses['boss.chat_ready']['loading']=True
                if failure=='missing':p.responses['boss.list']['rows']=[]
                if failure=='not_readback':p.responses['#chat-input']='other'
                if failure=='unknown_receipt':p.responses['boss.messages']['rows'][0]['receipt']='发送中'
                if failure=='new_message':
                    fill=p.fill
                    def change(*args,**kw):
                        fill(*args,**kw)
                        p.responses['boss.messages']['rows'].append({'id':'late','job_id':'one','conversation_id':'session','direction':'incoming'})
                        p.responses['boss.chat_ready']['visible_messages']=2
                    p.fill=change
                with self.assertRaises(pages.PlatformStateError):a.send_greeting(TEXT)
                self.assertFalse(any(x[0]=='press' for x in p.actions))
                self.assertNotIn(('click',pages.START),p.actions)
