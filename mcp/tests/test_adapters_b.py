import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from job_agent.browser.base import FakeBrowser, FakePage, FakeSpace
from job_agent.engine.models import Job
from job_agent.platforms.liepin.apply import LiepinAdapter
from job_agent.platforms.liepin.replies import LiepinReplySource
from job_agent.platforms.liepin import pages as lp
from job_agent.platforms.job51.apply import Job51Adapter
from job_agent.platforms.job51.replies import Job51ReplySource
from job_agent.platforms.job51 import pages as jp

ROOT = Path(__file__).parent / 'fixtures/platforms'

def fixture(platform, name):
    return json.loads((ROOT/platform/(name+'.json')).read_text())


def setup(adapter, responses, job=None):
    page = FakePage('p2', responses)
    space = FakeSpace('test', pages=[page])
    browser = FakeBrowser({'test':space})
    a = adapter(browser, SimpleNamespace(greeting_max_length=800))
    a.open_job(job or Job('liepin', '90000003', 'https://www.liepin.com/job/1990000003.shtml'), 'test')
    return a, page


def job51(applied=False):
    return dict(ready=True, loggedIn=True, jobId='172037288', isApply=applied,
                controls=[dict(text='已投递' if applied else '立即投递', **{'class':'apply-btn-new big disabled' if applied else 'apply-btn-new big'})])


def attachment(selected=False, receipt=False):
    return dict(visible=True, cvLogIdList=['cv-1'], selectedAttachmentResumeId='a-1' if selected else '',
                rows=[dict(index=0,attachResumeName='示例候选人_简历',attachResumeId='a-1',selected=selected)],
                receipt=dict(sendStatus=1,params=dict(cvLogIdList=['cv-1'],attachResumeId='a-1')) if receipt else None)

class SequencePage(FakePage):
    """FakePage with successive DOM projections for a transition, no arbitrary callbacks."""
    def evaluate(self, js):
        for key, values in self.responses.items():
            if key in js and isinstance(values, list):
                self.actions.append(('evaluate',key))
                return values.pop(0) if len(values)>1 else values[0]
        return super().evaluate(js)

class AdapterBTests(unittest.TestCase):
    def test_job51_failed_attachment_event_is_reported_without_success_timeout(self):
        from job_agent.browser.base import PageTimeout
        class FailedReceiptPage(SequencePage):
            def wait_for(self, selector, **kwargs):
                if selector == 'html[data-job-agent-attachment-status="1"]':
                    raise PageTimeout('No success marker: the actual event is failure')
                return super().wait_for(selector, **kwargs)
        failure=attachment(True,True)
        failure['receipt']['sendStatus']=0
        p=FailedReceiptPage('p2',{'job51.job':job51(), 'job51.observe':True,'job51.prepare_apply':True,
                               'job51.attachment':[attachment(),attachment(True),failure]})
        a=Job51Adapter(FakeBrowser({'test':FakeSpace('test',pages=[p])}),SimpleNamespace(greeting_max_length=800))
        a.open_job(Job('51job','172037288','https://jobs.51job.com/all/172037288.html'),'test')
        a.send_greeting('全文')
        with self.assertRaises(jp.AttachmentUnverified) as raised:a.choose_attachment('示例候选人_简历')
        self.assertIn('sendStatus=0',str(raised.exception))
        self.assertNotIn('PageTimeout',str(raised.exception))

    def test_job51_attachment_diagnostics_are_safe_and_risk_is_preserved(self):
        from job_agent.browser.base import BrowserError, RiskControl
        a,p=setup(Job51Adapter,{'job51.job':job51(),'job51.observe':True,'job51.prepare_apply':True},Job('51job','172037288','https://jobs.51job.com/all/172037288.html'))
        a.send_greeting('全文')
        with patch.object(p,'wait_for',side_effect=BrowserError('private page text')):
            with self.assertRaises(jp.AttachmentUnverified) as raised:a.choose_attachment('示例候选人_简历')
        self.assertIn('wait_rows',str(raised.exception))
        self.assertNotIn('private page text',str(raised.exception))
        with patch.object(p,'wait_for',side_effect=RiskControl('risk')):
            with self.assertRaises(RiskControl):a.choose_attachment('示例候选人_简历')

    def test_liepin_p0_application_and_contact_are_distinct(self):
        for name in ('applied-job','applied-lptjob','chat-only-job'):
            a,p=setup(LiepinAdapter,{'liepin.guard':{},'liepin.job':fixture('liepin',name)})
            with self.assertRaises(lp.Unverified):
                a.check_applied()
            self.assertFalse(any(x[0] in ('click','fill','press') for x in p.actions))
            a.close();self.assertTrue(p.closed)
        a,p=setup(LiepinAdapter,{'liepin.guard':{},'liepin.job':fixture('liepin','unapplied-job')})
        self.assertIs(a.check_applied(),False)
        self.assertFalse(any(x[0] in ('click','fill','press') for x in p.actions))
        a.close()
        self.assertIs(lp.applied({'controls':[{'text':'已投递'}]}),True)
        with self.assertRaises(lp.Unverified):
            lp.applied({'controls':[{'text':'已投递'},{'text':'投简历'}]})

    def test_unknown_states_raise(self):
        for value in (None,{},dict(controls=[])):
            a,_=setup(LiepinAdapter,{'liepin.guard':{},'liepin.job':value})
            with self.assertRaises(lp.Unverified):a.check_applied()
        for value in (None,{},dict(job51(),ready=False),dict(job51(),jobId='other'),dict(job51(),controls=[])):
            with self.assertRaises(jp.Unverified):jp.applied(value,'172037288')

    def test_login_and_risk(self):
        for data,exc in [(dict(login=True),lp.LoginRequired),(dict(risk=True),lp.RiskControl)]:
            with self.assertRaises(exc):setup(LiepinAdapter,{'liepin.guard':data})
        for data,exc in [(dict(loggedIn=False),jp.LoginRequired),(dict(risk=True,loggedIn=True),jp.RiskControl)]:
            with self.assertRaises(exc):jp.applied(data,'172037288')

    def test_liepin_long_greeting_no_effect_including_utf16(self):
        for text in ('测'*801,'😀'*401):
            a,p=setup(LiepinAdapter,{'liepin.guard':{}})
            with self.assertRaises(ValueError):a.send_greeting(text)
            self.assertFalse(any(x[0] in ('click','fill','press') for x in p.actions))

    def test_liepin_full_greeting_receipt_is_new_outgoing_text(self):
        before=dict(ready=True,items=[dict(msgId='old',msg='全文',direction='0',extType=1)])
        after=dict(ready=True,items=before['items']+[dict(msgId='new',msg='全文',direction='0',extType=1)])
        p=SequencePage('p2',{'liepin.guard':{},'liepin.job':fixture('liepin','unapplied-job'),
                             'liepin.messages':[before,after], 'liepin.editor':'全文'})
        a=LiepinAdapter(FakeBrowser({'test':FakeSpace('test',pages=[p])}),SimpleNamespace(greeting_max_length=800))
        a.open_job(Job('liepin','90000003','https://www.liepin.com/job/1990000003.shtml'),'test')
        a.send_greeting('全文');self.assertTrue(a.confirm_greeting_receipt())
        self.assertFalse(a.confirm_receipt())  # Text alone never proves formal resume delivery.
        self.assertIn(('fill',lp.EDITOR,'全文'),p.actions)
        with self.assertRaises(lp.Unverified):a.send_greeting('全文')
        p.responses['liepin.messages']=[before];self.assertFalse(a.confirm_greeting_receipt())
        with self.assertRaises(lp.Unverified):a.choose_attachment('resume')

    def test_editor_truncation_never_sends(self):
        a,p=setup(LiepinAdapter,{'liepin.guard':{},'liepin.job':fixture('liepin','unapplied-job'),
                               'liepin.messages':dict(ready=True,items=[]),'liepin.editor':'全'})
        with self.assertRaises(lp.Unverified):a.send_greeting('全文')
        self.assertFalse(any(x[0]=='press' for x in p.actions))

    def test_job51_positive_and_transient(self):
        self.assertTrue(jp.applied(job51(True),'172037288'))
        self.assertFalse(jp.applied(job51(False),'172037288'))
        transient=job51(False);transient['controls']=fixture('job51','applied-job')['controls']
        with self.assertRaises(jp.Unverified):jp.applied(transient,'172037288')

    def test_job51_attachment_flow_and_receipt(self):
        p=SequencePage('p2',{'job51.job':job51(), 'job51.observe':True,'job51.prepare_apply':True,
                             'job51.attachment':[attachment(),attachment(True),attachment(True,True)]})
        a=Job51Adapter(FakeBrowser({'test':FakeSpace('test',pages=[p])}),SimpleNamespace(greeting_max_length=800))
        a.open_job(Job('51job','172037288','https://jobs.51job.com/shanghai/172037288.html'),'test')
        a.send_greeting('全文')
        self.assertTrue(a.choose_attachment('示例候选人_简历'))
        self.assertTrue(a.confirm_receipt())
        clicks=[x[1] for x in p.actions if x[0]=='click']
        self.assertEqual(clicks,[jp.APPLY,jp.ROWS+' >> nth=0',jp.SEND_ATTACHMENT])
        with self.assertRaises(jp.Unverified):a.send_greeting('全文')
        a.close();self.assertTrue(p.closed)

    def test_applied_jobs_cannot_produce_effects(self):
        for cls, responses, job in [
            (LiepinAdapter, {'liepin.guard':{},'liepin.job':fixture('liepin','applied-job')}, Job('liepin','90000002','https://www.liepin.com/job/1990000002.shtml')),
            (Job51Adapter, {'job51.job':job51(True)}, Job('51job','172037288','https://jobs.51job.com/shanghai/172037288.html'))]:
            a,p=setup(cls,responses,job)
            with self.assertRaises(RuntimeError):a.send_greeting('全文')
            self.assertFalse(any(x[0] in ('click','press','fill') for x in p.actions))

    def test_job51_attachment_send_unknown_raises(self):
        p=SequencePage('p2',{'job51.job':job51(), 'job51.observe':True,'job51.prepare_apply':True,
                             'job51.attachment':[attachment(),attachment(True),attachment(True)]})
        a=Job51Adapter(FakeBrowser({'test':FakeSpace('test',pages=[p])}),SimpleNamespace(greeting_max_length=800))
        a.open_job(Job('51job','172037288','https://jobs.51job.com/shanghai/172037288.html'),'test')
        a.send_greeting('全文')
        with self.assertRaisesRegex(jp.AttachmentUnverified,'已申请、附件未确认'):
            a.choose_attachment('示例候选人_简历')
        self.assertFalse(a.confirm_receipt())
        self.assertEqual(sum(x==('click',jp.APPLY) for x in p.actions),1)

    def test_job51_failed_readback_no_attachment_send(self):
        for data in (None,{},dict(visible=False),attachment(),dict(attachment(True),selectedAttachmentResumeId='wrong')):
            a,p=setup(Job51Adapter,{'job51.job':job51(),'job51.observe':True,'job51.prepare_apply':True,'job51.attachment':data},Job('51job','172037288','https://jobs.51job.com/shanghai/172037288.html'))
            a.send_greeting('全文')
            with self.assertRaisesRegex(jp.AttachmentUnverified,'已申请、附件未确认'):
                a.choose_attachment('示例候选人_简历')
            self.assertNotIn(('click',jp.SEND_ATTACHMENT),p.actions)

    def test_attachment_unique_matching_and_exact_receipt(self):
        d=attachment();d['rows']*=2
        with self.assertRaises(jp.AttachmentUnverified):jp.attachment_selection(d,'示例候选人_简历')
        for d in (attachment(True),dict(attachment(True,True),receipt={'sendStatus':0}),dict(attachment(True,True),receipt={'sendStatus':1,'params':{'cvLogIdList':['wrong'],'attachResumeId':'a-1'}})):
            self.assertFalse(jp.attachment_receipt(d,'a-1',['cv-1']))
        a,p=setup(Job51Adapter,{'job51.job':job51(),'job51.observe':False},Job('51job','172037288','https://jobs.51job.com/shanghai/172037288.html'))
        with self.assertRaises(jp.Unverified):a.send_greeting('全文')
        self.assertFalse(any(x[0]=='click' for x in p.actions))
        with self.assertRaises(ValueError):a.send_greeting('测'*801)

    def test_reply_fixture_filters_and_stable_ids(self):
        d=fixture('liepin','employer-messages');d['ready']=True
        events=lp.parse_messages(d,job_id='90000002')
        self.assertTrue(events)
        self.assertFalse(any(e.direction=='incoming' for e in events))
        self.assertEqual(lp.parse_messages(d,job_id='90000002',since=10**12),[])
        # Explicitly synthetic human incoming sample, not live HR evidence.
        d=dict(ready=True,contact=dict(company='示例公司'),items=[dict(msgId='stable',msgTime=1795489317000,msg='请问何时到岗？',direction='1',extType=1,msgType='txt',bizData={})])
        with self.assertRaises(lp.Unverified):lp.parse_messages(d)
        e=lp.parse_messages(d,job_id='123')[0]
        self.assertEqual((e.message_id,e.body,e.direction),('stable','请问何时到岗？','incoming'))
        d['items'][0]['bsData']={'aiMark':True}
        self.assertEqual(lp.parse_messages(d,job_id='123')[0].direction,'system')
        d['items'][0]['msgId']=None;self.assertEqual(lp.parse_messages(d,job_id='123'),[])

    def test_recommendation_conversation_is_not_hr_reply(self):
        d=fixture('liepin','incoming-recommendations');d['ready']=True
        events=lp.parse_messages(d)
        self.assertTrue(events)
        self.assertTrue(all(e.direction=='system' for e in events))

    def test_sources_cleanup_and_51job_gap_not_empty_inbox(self):
        p=FakePage('p2',{'liepin.guard':{},'liepin.contacts':[{'index':0,'id':'contact-1'}],
                          'liepin.messages':dict(ready=True,items=[])})
        self.assertEqual(LiepinReplySource().fetch(FakeSpace('test',pages=[p])),[])
        self.assertTrue(p.closed)
        self.assertFalse(any(x[0] in ('press','fill') for x in p.actions))
        p=FakePage('p2',{'job51.applications':dict(loggedIn=True,ready=False)})
        with self.assertRaises(jp.Unverified):Job51ReplySource().fetch(FakeSpace('test',pages=[p]))
        self.assertTrue(p.closed)
        self.assertFalse(any(x[0] in ('click','press','fill') for x in p.actions))

if __name__=='__main__':unittest.main()
