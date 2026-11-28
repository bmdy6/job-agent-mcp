import json
from pathlib import Path
import unittest
from job_agent.browser.base import FakeBrowser, FakePage, FakeSpace
from job_agent.engine.models import Job
from job_agent.platforms.boss import pages as boss
from job_agent.platforms.boss.apply import BossAdapter
from job_agent.platforms.boss.replies import BossReplySource
from job_agent.platforms.zhilian import pages as zhilian
from job_agent.platforms.zhilian.apply import ZhilianAdapter
from job_agent.platforms.zhilian.replies import ZhilianReplySource


def fixture(p,n):
    return json.loads((Path(__file__).parent/'fixtures/platforms'/p/(n+'.json')).read_text())


class Adapters(unittest.TestCase):
    cases=[('boss',boss,BossAdapter,'job-fixture-0','https://www.zhipin.com/job_detail/job-fixture-0.html'),
           ('zhilian',zhilian,ZhilianAdapter,'CC000000000J00000000000','https://www.zhaopin.com/jobdetail/CC000000000J00000000000.html')]

    def make(self,c):
        p=FakePage('owned',{c[0]+'.state':{'buttons':[{'text':c[1].NEW_LABEL}]},
            c[0]+'.messages':{'current_job_id':c[3],'rows':[]}})
        if c[0]=='boss':
            p.responses['boss.identity']={'company':'Company','title':'Role'}
            p.responses['boss.chat_identity']={'job_id':c[3],'company':'Company','title':'Role','header_title':'Role','conversation_id':'session'}
            p.responses['boss.contact_state']={'kind':'none'}
            p.responses['boss.chat_ready']={'root':True,'editor':True,'input_empty':True,'loading':False,'visible_messages':0}
            p.responses['#chat-input']='全文'
            def click(selector):
                FakePage.click(p,selector)
                if selector==boss.START:
                    p._url=boss.CHAT_URL+'?fixture=1'
            p.click=click
        a=c[2](FakeBrowser({'test':FakeSpace('test',pages=[p])}),{})
        a.open_job(Job(c[0],c[3],c[4]),'test')
        if c[0]=='zhilian':
            a.bind_identity({'platform':'zhilian','platform_job_id':c[3],'company':'Company','title':'Role'})
        return a,p

    def test_boss_observed_navigation_and_input_precede_send(self):
        a,p=self.make(self.cases[0])
        a.send_greeting('全文')
        self.assertLess(p.actions.index(('wait_for',boss.INPUT)),p.actions.index(('fill',boss.INPUT,'全文')))
        self.assertEqual([x for x in p.actions if x[0]=='press'],[('press',boss.SEND)])

    def test_boss_identity_fixture_and_mismatch_block_send(self):
        d=fixture('boss','live-chat-identity')
        self.assertTrue(boss.matching_chat(d['target'],d['chat'],'job-fixture-0'))
        for field in ('company','title','header_title','job_id'):
            a,p=self.make(self.cases[0])
            p.responses['boss.chat_identity'][field]='other'
            with self.assertRaises(boss.PlatformStateError):a.send_greeting('全文')
            self.assertFalse(any(x[0] in ('fill','press') for x in p.actions))

    def test_boss_redirect_timeout_never_types(self):
        from job_agent.browser.base import PageTimeout
        from unittest.mock import patch
        a,p=self.make(self.cases[0])
        p.click=lambda selector:FakePage.click(p,selector)
        with patch('job_agent.platforms.boss.apply.monotonic',side_effect=[0,11]),self.assertRaisesRegex(PageTimeout,'boss.chat_navigation'):
            a.send_greeting('全文')
        self.assertFalse(any(x[0] in ('fill','press') for x in p.actions))

    def test_zhilian_modal_timeout_never_replays_application_or_continues_chat(self):
        from job_agent.browser.base import PageTimeout
        a,p=self.make(self.cases[1])
        wait=p.wait_for
        def timeout(selector,**kwargs):
            if selector==zhilian.SUCCESS_MODAL:raise PageTimeout('unknown')
            return wait(selector,**kwargs)
        p.wait_for=timeout
        a.send_greeting('全文')
        self.assertEqual([x[1] for x in p.actions if x[0]=='click'],[zhilian.START])
        self.assertFalse(any(x[0] in ('fill','press') for x in p.actions))

    def test_first_click_timeout_stops_without_other_action(self):
        from job_agent.browser.base import PageTimeout
        for c in self.cases:
            a,p=self.make(c)
            def fail(selector):
                FakePage.click(p,selector)
                raise PageTimeout('unknown effect')
            p.click=fail
            with self.assertRaises(PageTimeout):a.send_greeting('全文')
            self.assertEqual(len([x for x in p.actions if x[0]=='click']),1)
            self.assertFalse(any(x[0] in ('fill','press') for x in p.actions))

    def test_boss_new_owned_chat_observed_not_preexisting_tab(self):
        a,p=self.make(self.cases[0])
        old=FakePage('preexisting',dict(p.responses));old._url=boss.CHAT_URL
        new=FakePage('new-owned',dict(p.responses));new._url=boss.CHAT_URL+'?fixture=2'
        a.space.spawned.append(old)
        def click(selector):
            FakePage.click(p,selector)
            a.space.spawned.append(new)
        p.click=click
        a.send_greeting('全文')
        self.assertIs(a.page,new)
        self.assertTrue(p.closed)
        self.assertFalse(old.closed)
        self.assertFalse(any(x[0] in ('fill','press','click') for x in old.actions))
        self.assertEqual([x for x in new.actions if x[0]=='press'],[('press',boss.SEND)])
        self.assertEqual([x for x in p.actions if x[0]=='click'],[('click',boss.START)])

    def test_boss_unverified_contact_dialog_is_not_bypassed(self):
        for kind in ('preset_greeting','editable_contact','unknown_dialog'):
            a,p=self.make(self.cases[0])
            p.click=lambda selector:FakePage.click(p,selector)
            p.responses['boss.contact_state']={'kind':kind}
            with self.assertRaises(boss.PlatformStateError):a.send_greeting('全文')
            self.assertEqual(len([x for x in p.actions if x[0]=='click']),1)
            self.assertFalse(any(x[0] in ('fill','press') for x in p.actions))

    def editable(self, *, receipt=True):
        import time
        a,p=self.make(self.cases[0])
        state={'kind':'editable_contact','input_ready':True,'value':'默认招呼',
               'submit_selector':boss.CONTACT_SUBMIT_SELECTORS[0],'submit_label':'发送'}
        def click(selector):
            FakePage.click(p,selector)
            if selector==boss.START:
                p.responses['boss.contact_state']=state
            elif selector==state['submit_selector']:
                p._url=boss.CHAT_URL
                if receipt:
                    p.responses['boss.messages']={'current_job_id':self.cases[0][3],'rows':[
                        {'id':'confirmed-modal','job_id':self.cases[0][3],'conversation_id':'session',
                         'direction':'outgoing','body':'全文','time':time.time(),'receipt':'送达'}]}
        def fill(selector,text):
            FakePage.fill(p,selector,text)
            state['value']=text
        p.click,p.fill=click,fill
        return a,p,state

    def modal_receipt_page(self,a,*,confirmed=True):
        import time
        jid=self.cases[0][3]
        page=FakePage('receipt',{'boss.state':{},'boss.list':{'rows':[
            {'encryptJobId':jid,'uniqueId':'session','lastMsgId':'new-message' if confirmed else ''}]},
            'boss.select_session':True,'boss.messages':{'current_job_id':jid,'rows':[
                {'id':'new-message','job_id':jid,'conversation_id':'session',
                 'direction':'outgoing','body':'全文','time':time.time(),'receipt':'送达'}]}})
        a.space._pages.append(page)
        return page

    def test_boss_editable_contact_submits_form_once_not_chat(self):
        a,p,state=self.editable()
        a.send_greeting('全文')
        self.assertTrue(a.confirm_receipt())
        self.assertEqual([x for x in p.actions if x[0]=='click'],
                         [('click',boss.START),('click',state['submit_selector'])])
        self.assertEqual([x for x in p.actions if x[0]=='fill'],[('fill',boss.CONTACT_INPUT,'全文')])
        self.assertFalse(any(x[0]=='press' for x in p.actions))

    def test_boss_editable_contact_empty_chat_is_not_second_send(self):
        a,p,state=self.editable(receipt=False)
        receipt=self.modal_receipt_page(a,confirmed=False)
        a.send_greeting('全文')
        self.assertFalse(a.confirm_receipt())
        self.assertEqual(len([x for x in p.actions if x[0]=='click']),2)
        self.assertFalse(any(x[0]=='press' for x in p.actions))
        self.assertTrue(receipt.closed)
        self.assertFalse(any(x[0] in ('click','fill','press') for x in receipt.actions))

    def test_boss_editable_wrong_readback_or_button_does_not_submit(self):
        for changes in ({'value':'not complete'},{'submit_selector':'.unknown-button'},
                        {'submit_selector':None},{'submit_label':'取消'},{'input_ready':False}):
            a,p,state=self.editable()
            fill=p.fill
            def changed(selector,text):
                fill(selector,text)
                state.update(changes)
            p.fill=changed
            with self.assertRaises(boss.PlatformStateError):a.send_greeting('全文')
            self.assertEqual([x for x in p.actions if x[0]=='click'],[('click',boss.START)])
            self.assertFalse(any(x[0]=='press' for x in p.actions))

    def test_boss_editable_risk_or_identity_change_blocks_submit(self):
        for field in ('risk','identity'):
            a,p,state=self.editable()
            fill=p.fill
            def changed(selector,text):
                fill(selector,text)
                if field=='risk':p.responses['boss.state']={'risk':True}
                else:p.responses['boss.identity']={'company':'Other','title':'Role'}
            p.fill=changed
            expected=boss.RiskControl if field=='risk' else boss.PlatformStateError
            with self.assertRaises(expected):a.send_greeting('全文')
            self.assertEqual([x for x in p.actions if x[0]=='click'],[('click',boss.START)])

    def test_boss_editable_submit_timeout_does_not_repeat(self):
        from job_agent.browser.base import PageTimeout
        for confirmed in (False,True):
            a,p,state=self.editable()
            receipt=self.modal_receipt_page(a,confirmed=confirmed)
            click=p.click
            def timeout(selector):
                if selector==state['submit_selector']:
                    FakePage.click(p,selector)
                    raise PageTimeout('unknown confirmation effect')
                click(selector)
            p.click=timeout
            a.send_greeting('全文')
            self.assertIs(a.confirm_receipt(),confirmed)
            self.assertEqual([x for x in p.actions if x[0]=='click'],
                             [('click',boss.START),('click',state['submit_selector'])])
            self.assertFalse(any(x[0]=='press' for x in p.actions))
            self.assertTrue(receipt.closed)
            self.assertFalse(any(x[0] in ('click','fill','press') for x in receipt.actions))

    def test_boss_editable_no_navigation_uses_original_site_receipt(self):
        from unittest.mock import patch
        a,p,state=self.editable()
        receipt=self.modal_receipt_page(a)
        click=p.click
        def remains_on_job(selector):
            if selector==state['submit_selector']:
                FakePage.click(p,selector)
                state['kind']='none'
            else:
                click(selector)
        p.click=remains_on_job
        with patch('job_agent.platforms.boss.apply.monotonic',side_effect=[0,0,11]):
            a.send_greeting('全文')
        self.assertTrue(a.confirm_receipt())
        self.assertEqual(p.url(),self.cases[0][4])
        self.assertEqual(len([x for x in p.actions if x[0]=='click']),2)
        self.assertFalse(any(x[0]=='press' for x in p.actions))
        self.assertTrue(receipt.closed)
        self.assertFalse(any(x[0] in ('click','fill','press') for x in receipt.actions))

    def test_boss_editable_no_navigation_risk_is_not_hidden_by_receipt_check(self):
        from job_agent.browser.base import PageTimeout
        a,p,state=self.editable()
        click=p.click
        def blocked(selector):
            if selector==state['submit_selector']:
                FakePage.click(p,selector)
                p.responses['boss.state']={'risk':True}
                raise PageTimeout('risk')
            click(selector)
        p.click=blocked
        with self.assertRaises(boss.RiskControl):a.send_greeting('全文')
        self.assertEqual(len(a.space.opened),1)
        self.assertFalse(any(x[0]=='press' for x in p.actions))

    def test_boss_editable_parallel_chat_prevents_modal_submission(self):
        a,p,state=self.editable()
        click=p.click
        def spawned(selector):
            click(selector)
            if selector==boss.START:
                new=FakePage('new-chat',dict(p.responses));new._url=boss.CHAT_URL
                a.space.spawned.append(new)
        p.click=spawned
        with self.assertRaises(boss.PlatformStateError):a.send_greeting('全文')
        self.assertEqual([x for x in p.actions if x[0]=='click'],[('click',boss.START)])
        self.assertFalse(any(x[0]=='fill' for x in p.actions))

    def test_boss_existing_outgoing_must_not_send_twice(self):
        import time
        for receipt in ('发送中','送达'):
            a,p=self.make(self.cases[0])
            p.responses['boss.messages']={'current_job_id':self.cases[0][3],'rows':[
                {'id':'m','job_id':self.cases[0][3],'conversation_id':'session','direction':'outgoing',
                 'body':'此前的消息','time':time.time()-60,'receipt':receipt}]}
            p.responses['boss.chat_ready']['visible_messages']=1
            with self.assertRaises(boss.PlatformStateError):a.send_greeting('全文')
            self.assertFalse(any(x[0] in ('fill','press') for x in p.actions))

    def test_boss_late_outgoing_after_filling_is_not_replayed(self):
        import time
        a,p=self.make(self.cases[0])
        fill=p.fill
        def changed(selector,text):
            fill(selector,text)
            p.responses['boss.messages']={'current_job_id':self.cases[0][3],'rows':[
                {'id':'late','job_id':self.cases[0][3],'conversation_id':'session','direction':'outgoing',
                 'body':'全文','time':time.time(),'receipt':'发送中'}]}
        p.fill=changed
        with self.assertRaises(boss.PlatformStateError):a.send_greeting('全文')
        self.assertFalse(any(x[0]=='press' for x in p.actions))

    def test_boss_positive_fresh_first_contact_receipt_never_sends_again(self):
        import time
        a,p=self.make(self.cases[0])
        click=p.click
        def contacted(selector):
            click(selector)
            p.responses['boss.messages']={'current_job_id':self.cases[0][3],'rows':[
                {'id':'m','job_id':self.cases[0][3],'conversation_id':'session','direction':'outgoing',
                 'body':'全文','time':time.time(),'receipt':'送达'}]}
        p.click=contacted
        a.send_greeting('全文')
        self.assertTrue(a.confirm_receipt())
        self.assertFalse(any(x[0] in ('fill','press') for x in p.actions))

    def test_applied_fixtures_and_unknown(self):
        for c in self.cases:
            for n,expected in [('job-new',False),('job-existing',True)]:
                a,p=self.make(c);d=fixture(c[0],n)
                buttons=d.get('visible_buttons',d.get('buttons'))
                if c[0]=='zhilian' and n=='job-existing':buttons=buttons[:1]
                p.responses[c[0]+'.state']={'buttons':buttons}
                self.assertIs(a.check_applied(),expected)
                self.assertFalse(any(x[0]=='click' for x in p.actions))
                a.close();self.assertTrue(p.closed)
            for d in (None,{}, {'buttons':[{'text':'职位下线'}]}):
                a,p=self.make(c);p.responses[c[0]+'.state']=d
                with self.assertRaises(c[1].PlatformStateError):a.check_applied()

    def test_login_risk_length_and_attachment_no_click(self):
        for c in self.cases:
            for k,exc in [('login_required',c[1].LoginRequired),('risk',c[1].RiskControl)]:
                a,p=self.make(c);p.responses[c[0]+'.state']={k:True}
                with self.assertRaises(exc):a.send_greeting('全文')
                self.assertFalse(any(x[0]=='click' for x in p.actions))
            for text in ('测'*1001,'😀'*501,''):
                a,p=self.make(c)
                with self.assertRaises(ValueError):a.send_greeting(text)
                self.assertFalse(any(x[0]=='click' for x in p.actions))
            a,p=self.make(c);a.settings={'attachment_name':'exact.pdf'}
            with self.assertRaises(c[1].PlatformStateError):a.send_greeting('全文')
            self.assertFalse(any(x[0]=='click' for x in p.actions))

    def test_boss_receipt_full_fresh_and_no_second_submission(self):
        c=self.cases[0]
        a,p=self.make(c);a.send_greeting('全文');self.assertFalse(a.confirm_receipt())
        d={'current_job_id':c[3],'rows':[{'id':'new','job_id':c[3],'conversation_id':'session','direction':'outgoing','body':'全文','receipt':'送达'}]}
        p.responses['boss.messages']=d
        self.assertTrue(a.confirm_receipt())
        a.before=boss.message_ids(d);self.assertFalse(a.confirm_receipt())
        a.before=set();a.greeting='全';self.assertFalse(a.confirm_receipt())
        with self.assertRaises(boss.PlatformStateError):a.send_greeting('全文')
        self.assertFalse(a.choose_attachment('exact.pdf'));self.assertFalse(a.confirm_receipt())

    def test_zhilian_receipt_requires_application_and_greeting_independently(self):
        from unittest.mock import patch
        a,p=self.make(self.cases[1])
        a.send_greeting('全文')
        with patch('job_agent.platforms.zhilian.receipt_reader.read_application',return_value=None),patch('job_agent.platforms.zhilian.receipt_reader.read_greeting') as greeting:
            self.assertFalse(a.confirm_receipt())
            greeting.assert_not_called()
        stamp='2026-11-25T12:23:39+08:00'
        with patch('job_agent.platforms.zhilian.receipt_reader.read_application',return_value=stamp),patch('job_agent.platforms.zhilian.receipt_reader.read_greeting',return_value=False) as greeting:
            self.assertFalse(a.confirm_receipt())
            self.assertEqual(a.application_receipt_at,stamp)
            greeting.return_value=True
            self.assertTrue(a.confirm_receipt())
        with self.assertRaises(zhilian.PlatformStateError):a.send_greeting('全文')
        self.assertFalse(a.choose_attachment('exact.pdf'));self.assertFalse(a.confirm_receipt())
        self.assertEqual([x[1] for x in p.actions if x[0]=='click'],[zhilian.START])
        self.assertFalse(any(x[0] in ('fill','press') for x in p.actions))

    def test_input_readback_and_identity(self):
        a,p=self.make(self.cases[0]);p.responses['#chat-input']='全'
        with self.assertRaises(boss.PlatformStateError):a.send_greeting('全文')
        self.assertFalse(any(x[0]=='press' for x in p.actions))
        for c in self.cases:
            a,p=self.make(c);a.close()
            with self.assertRaises(ValueError):a.open_job(Job(c[0],'wrong',c[4]),'test')
            a.close()

    def test_replies_fixtures(self):
        e=zhilian.events(fixture('zhilian','chat-incoming'))
        self.assertEqual(len(e),5);self.assertEqual([x.direction for x in e],['system']*4+['incoming'])
        self.assertEqual(len(zhilian.events(fixture('zhilian','message-model-outgoing'))),2)
        self.assertEqual(zhilian.events(fixture('zhilian','chat-incoming'),since=2e9),[])
        d=fixture('zhilian','chat-incoming')
        for r in d['messages']:r['msg'].pop('idServer',None)
        self.assertEqual(zhilian.events(d),[])
        self.assertEqual(boss.events(fixture('boss','conversation-model')),[])
        r=fixture('boss','conversation-model')['rows'][0]
        d={'rows':[{'id':r['lastMsgId'],'job_id':r['encryptJobId'],'conversation_id':r['uniqueId'],'time':r['lastTS'],'body':'原文','direction':'incoming'}]}
        self.assertEqual(boss.events(d)[0].body,'原文')
        d['rows'][0]['card']=True;self.assertEqual(boss.events(d)[0].direction,'system')
        d['rows'][0]['time']=None;self.assertEqual(boss.events(d),[])

    def test_reply_cleanup(self):
        for platform,pages,cls in [('boss',boss,BossReplySource),('zhilian',zhilian,ZhilianReplySource)]:
            p=FakePage('owned',{platform+'.state':{},platform+'.list':{'rows':[]}})
            with self.assertRaises(pages.PlatformStateError):cls().fetch(FakeSpace('t',pages=[p]))
            self.assertTrue(p.closed)
            p=FakePage('owned',{platform+'.state':{},platform+'.list':{'rows':[{}]},platform+'.messages':{}})
            self.assertEqual(cls().fetch(FakeSpace('t',pages=[p])),[])
            self.assertTrue(p.closed)
            self.assertFalse(any(x[0] in ('fill','press') for x in p.actions))

    def test_open_failure_closes_page(self):
        for c in self.cases:
            page=FakePage('own',{c[0]+'.state':{'login_required':True}})
            a=c[2](FakeBrowser({'test':FakeSpace('test',pages=[page])}),{})
            with self.assertRaises(c[1].LoginRequired):a.open_job(Job(c[0],c[3],c[4]),'test')
            self.assertTrue(page.closed)

    def test_no_send_to_wrong_chat(self):
        a,p=self.make(self.cases[0]);p.responses['boss.chat_identity']={'job_id':'other'}
        with self.assertRaises(boss.PlatformStateError):a.send_greeting('全文')
        self.assertFalse(any(x[0] in ('fill','press') for x in p.actions))

    def test_receipt_rejects_failure_and_wrong_job(self):
        d=fixture('zhilian','message-model-outgoing')
        jid=d['messages'][0]['session']['jobNumber']
        self.assertTrue(zhilian.receipt(d,'[脱敏消息]',jid,set()))
        self.assertFalse(zhilian.receipt(d,'[脱敏消息]','wrong',set()))
        for r in d['messages']:r['msg']['sendState']='failed'
        self.assertFalse(zhilian.receipt(d,'[脱敏消息]',jid,set()))

    def test_boss_unavailable_latest_bubble_is_not_fabricated(self):
        from job_agent.browser.base import PageTimeout
        class MissingBubble(FakePage):
            def wait_for(self,selector,**kw):
                if 'data-mid' in selector:raise PageTimeout('missing')
                super().wait_for(selector,**kw)
        p=MissingBubble('owned',{'boss.select_session':True,'boss.state':{},'boss.list':{'rows':[{'encryptJobId':'j','uniqueId':'s','lastMsgId':'123'}]}})
        with self.assertLogs('job_agent.platforms.boss.replies',level='WARNING'):
            self.assertEqual(BossReplySource().fetch(FakeSpace('t',pages=[p])),[])
        self.assertTrue(p.closed)
