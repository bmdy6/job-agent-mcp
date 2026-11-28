import unittest
from job_agent.browser.base import FakeBrowser, FakePage, FakeSpace
from job_agent.engine.models import Job
from job_agent.platforms.nowcoder import flow
from job_agent.platforms.nowcoder.apply import NowcoderAdapter

class ApplicationTests(unittest.TestCase):
    def adapter(self, resumes=None, *, opened=True):
        self.page=FakePage('p1',{'nowcoder.guard':{'logged_in':True},
            'nowcoder.application_job':{'id':1,'count':1,'applied':False,'official':False,'button':'立即申请','title':'AI','company':'Example'},
            'nowcoder.resumes':{'status':200,'code':0,'data':resumes or []}})
        a=NowcoderAdapter(FakeBrowser({'test':FakeSpace('test',pages=[self.page])}),{'greeting_mode':'none'})
        if opened:a.open_job(Job('nowcoder','1','https://www.nowcoder.com/jobs/detail/1'),'test')
        return a

    def test_missing_resume_never_clicks(self):
        a=self.adapter()
        with self.assertRaises(flow.NotStarted):a.send_greeting('')
        self.assertFalse(any(x[0]=='click' for x in self.page.actions))
        a.close();self.assertTrue(self.page.closed)

    def test_popup_question_does_not_submit(self):
        a=self.adapter([{'id':2,'resumeDone':True,'defaultResume':True,'attachment':0,'resumeName':'在线简历','uuid':'online-uuid'}])
        self.page.responses['nowcoder.application_popup']={'count':1,'job_id':1,'ready':True,'resume_id':2,'resume_done':True,'resume_name':'在线简历','resume_attachment':0,'resume_uuid':'online-uuid','forms':True,'submit_count':1}
        with self.assertRaises(flow.NotStarted):a.send_greeting('')
        self.assertEqual(len([x for x in self.page.actions if x[0]=='click']),1)

    def test_receipt_requires_exact_fresh_transition(self):
        row={'jobId':1,'deliverId':3,'transferList':[{'subStageName':'投递成功','createTime':1000000}]}
        self.assertTrue(flow.receipt([row],1,3,999,1001))
        for rows,jid,did,at in [([row],2,3,999),([row],1,4,999),([row,row],1,3,999),([row],1,3,1100),([],1,3,999)]:
            self.assertFalse(flow.receipt(rows,jid,did,at,1101))
        self.assertFalse(flow.receipt([{**row,'transferList':[],'latestUpdateTime':1000000}],1,3,999,1001))

    def test_ambiguous_or_incomplete_resume_is_not_started(self):
        for rows in [[],[{'id':2,'defaultResume':True,'resumeDone':False}], [{'id':2,'defaultResume':True,'resumeDone':True}]*2]:
            with self.assertRaises(flow.NotStarted):flow.resume({'status':200,'code':0,'data':rows})

    def test_dispatch_timeout_never_replays_or_becomes_not_started(self):
        from job_agent.browser.base import PageTimeout
        a=self.adapter([{'id':2,'resumeDone':True,'defaultResume':True,'attachment':0,'resumeName':'在线简历','uuid':'online-uuid'}])
        self.page.responses['nowcoder.application_popup']={'count':1,'job_id':1,'ready':True,'resume_id':2,'resume_done':True,'resume_name':'在线简历','resume_attachment':0,'resume_uuid':'online-uuid','forms':False,'submit_count':1}
        old=self.page.click
        def click(selector):
            old(selector)
            if selector==flow.SUBMIT:raise PageTimeout('submit timeout')
        self.page.click=click
        with self.assertRaises(PageTimeout):a.send_greeting('')
        self.assertTrue(a.submitted)
        with self.assertRaises(flow.pages.Unverified):a.send_greeting('')
        self.assertEqual(len([x for x in self.page.actions if x[0]=='click']),2)

    def test_success_requires_ui_ack_and_original_record(self):
        import time
        a=self.adapter([{'id':2,'resumeDone':True,'defaultResume':True,'attachment':0,'resumeName':'在线简历','uuid':'online-uuid'}])
        state={'count':1,'job_id':1,'ready':True,'resume_id':2,'resume_done':True,'resume_name':'在线简历','resume_attachment':0,'resume_uuid':'online-uuid','forms':False,'submit_count':1,
               'success':True,'ack':True,'deliver_id':3,'successful_resume_id':2}
        self.page.responses['nowcoder.application_popup']=state
        self.page.responses['nowcoder.application_records']={'status':200,'code':0,'data':{'deliverData':[
            {'jobId':1,'deliverId':3,'transferList':[{'subStageName':'投递成功','createTime':time.time()*1000}]}], 'pageInfo':{}}}
        a.send_greeting('')
        self.assertTrue(a.confirm_receipt())
        self.page.responses['nowcoder.application_records']['data']['deliverData']=[]
        self.assertFalse(a.confirm_receipt())


class ResumeSelectionTests(unittest.TestCase):
    adapter = ApplicationTests.adapter
    def test_only_complete_default_online_resume_is_required(self):
        online={'id':2,'resumeDone':True,'defaultResume':True,'attachment':0,'resumeName':'在线简历','uuid':'online-uuid'}
        attachment={'id':4,'resumeDone':True,'defaultResume':False,'attachment':1,'resumeName':'resume.pdf','uuid':'pdf-uuid'}
        self.assertEqual(flow.resume({'status':200,'code':0,'data':[online,attachment]}),online)
        # Attachment default markers do not participate in online eligibility.
        self.assertEqual(flow.resume({'status':200,'code':0,'data':[online,{**attachment,'defaultResume':True}]}),online)
        with self.assertRaises(flow.NotStarted):flow.resume({'status':200,'code':0,'data':[{**online,'defaultResume':False},{**attachment,'defaultResume':True}]})

    def test_mismatched_popup_is_unverified_and_records_before_refusal(self):
        a=self.adapter([{'id':2,'resumeDone':True,'defaultResume':True,'attachment':0,'resumeName':'在线简历','uuid':'online-uuid'}])
        self.page.responses['nowcoder.application_popup']={'count':1,'job_id':1,'ready':True,'resume_id':4,'resume_done':True,
            'resume_name':'resume.pdf','resume_attachment':1,'resume_uuid':'pdf-uuid','forms':False,'submit_count':1}
        evidence=[]
        a.bind_resume_recorder(evidence.append)
        with self.assertRaises(flow.pages.Unverified) as caught:a.send_greeting('')
        self.assertNotIsInstance(caught.exception,flow.NotStarted)
        self.assertEqual(evidence[0]['selected']['id'],'4')
        self.assertEqual(evidence[0]['expected']['id'],'2')
        self.assertFalse(evidence[0]['matches_expected'])
        self.assertFalse(a.submitted)
        with self.assertRaises(flow.pages.Unverified):a.send_greeting('')
        self.assertEqual(len([x for x in self.page.actions if x[0]=='click']),1)

    def test_recorder_failure_never_submits(self):
        a=self.adapter([{'id':2,'resumeDone':True,'defaultResume':True,'attachment':0,'resumeName':'在线简历','uuid':'online-uuid'}])
        self.page.responses['nowcoder.application_popup']={'count':1,'job_id':1,'ready':True,'resume_id':2,'resume_done':True,
            'resume_name':'在线简历','resume_attachment':0,'resume_uuid':'online-uuid','forms':False,'submit_count':1}
        def broken(evidence):raise OSError('disk full')
        a.bind_resume_recorder(broken)
        with self.assertRaises(OSError):a.send_greeting('')
        self.assertFalse(a.submitted)
        self.assertEqual(len([x for x in self.page.actions if x[0]=='click']),1)

    def test_queue_persists_actual_resume_on_mismatch_and_blocks_replay(self):
        import json,tempfile
        from pathlib import Path
        from job_agent.store import SQLiteDeliveryStore
        from job_agent.config.engine import EngineSettings
        from job_agent.engine.queue import DeliveryEngine
        from job_agent.engine.preflight import FakePreflight
        from job_agent.engine.spaces import FakeTaskSpaces
        from job_agent.engine.models import Status
        from job_agent.engine.adapters import FakeAdapter
        a=self.adapter([{'id':2,'resumeDone':True,'defaultResume':True,'attachment':0,'resumeName':'在线简历','uuid':'online-uuid'}],opened=False)
        self.page.responses['nowcoder.application_popup']={'count':1,'job_id':1,'ready':True,'resume_id':4,'resume_done':True,
            'resume_name':'resume.pdf','resume_attachment':1,'resume_uuid':'pdf-uuid','forms':False,'submit_count':1}
        job=Job('nowcoder','1','https://www.nowcoder.com/jobs/detail/1')
        cfg=EngineSettings(platforms={'nowcoder':dict(enabled=True,greeting_mode='none',greeting_max_length=1,interval_seconds=0)})
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'jobs.db'
            with SQLiteDeliveryStore(path) as store:
                store.add(job);store.prepare_run(cfg.model_dump())
                engine=DeliveryEngine(store,cfg,{'nowcoder':a},FakePreflight(),FakeTaskSpaces())
                engine._deliver(job,cfg.platforms['nowcoder'],'test')
                rows=store.list_deliveries()
                self.assertEqual(store.records()[0].status,Status.UNVERIFIED)
                # The selection is durably appended before refusal and retained on final outcome.
                for row in rows[1:]:
                    data=json.loads(row['error_detail'])['resume_selection']
                    self.assertEqual(data['selected']['uuid'],'pdf-uuid')
                    self.assertEqual(row['attachment_name'],'resume.pdf')
            with SQLiteDeliveryStore(path) as reopened:
                self.assertTrue(reopened.history_blocked(1))
                fake=FakeAdapter()
                DeliveryEngine(reopened,cfg,{'nowcoder':fake},FakePreflight(),FakeTaskSpaces()).run()
                self.assertNotIn('send_greeting',fake.calls)

    def test_selection_persists_before_submit_and_survives_success_or_timeout(self):
        import json,tempfile,time
        from pathlib import Path
        from job_agent.store import SQLiteDeliveryStore
        from job_agent.config.engine import EngineSettings
        from job_agent.engine.queue import DeliveryEngine
        from job_agent.engine.preflight import FakePreflight
        from job_agent.engine.spaces import FakeTaskSpaces
        from job_agent.engine.models import Status
        from job_agent.browser.base import PageTimeout
        for timeout in (False,True):
            with self.subTest(timeout=timeout),tempfile.TemporaryDirectory() as tmp:
                a=self.adapter([{'id':2,'resumeDone':True,'defaultResume':True,'attachment':0,'resumeName':'在线简历','uuid':'online-uuid'}],opened=False)
                self.page.responses['nowcoder.application_popup']={'count':1,'job_id':1,'ready':True,'resume_id':2,'resume_done':True,
                    'resume_name':'在线简历','resume_attachment':0,'resume_uuid':'online-uuid','forms':False,'submit_count':1,
                    'success':True,'ack':True,'deliver_id':3,'successful_resume_id':2}
                self.page.responses['nowcoder.application_records']={'status':200,'code':0,'data':{'deliverData':[
                    {'jobId':1,'deliverId':3,'transferList':[{'subStageName':'投递成功','createTime':time.time()*1000}]}],'pageInfo':{}}}
                job=Job('nowcoder','1','https://www.nowcoder.com/jobs/detail/1')
                cfg=EngineSettings(platforms={'nowcoder':dict(enabled=True,greeting_mode='none',greeting_max_length=1,interval_seconds=0)})
                with SQLiteDeliveryStore(Path(tmp)/'jobs.db') as store:
                    store.add(job);store.prepare_run(cfg.model_dump())
                    click=self.page.click
                    def submit(selector):
                        if selector==flow.SUBMIT:
                            saved=json.loads(store.list_deliveries()[-1]['error_detail'])['resume_selection']
                            self.assertTrue(saved['matches_expected'])
                            self.assertEqual(saved['selected']['name'],'在线简历')
                            if timeout:raise PageTimeout('submit timeout')
                        click(selector)
                    self.page.click=submit
                    DeliveryEngine(store,cfg,{'nowcoder':a},FakePreflight(),FakeTaskSpaces())._deliver(job,cfg.platforms['nowcoder'],'test')
                    self.assertEqual(store.records()[0].status,Status.UNVERIFIED if timeout else Status.SENT)
                    final=store.list_deliveries()[-1]
                    self.assertEqual(json.loads(final['error_detail'])['resume_selection']['selected']['uuid'],'online-uuid')
                    self.assertEqual(final['attachment_name'],'')
                    self.assertTrue(store.history_blocked(1))

    def test_live_hr_enrichment_without_projection_keeps_existing_identity(self):
        from job_agent.platforms.hr import enrich_open_job
        from unittest.mock import Mock
        job=Job('nowcoder','1','https://www.nowcoder.com/jobs/detail/1')
        store,page=Mock(),Mock()
        enrich_open_job(store,job,page)
        page.evaluate.assert_not_called()
        store.upsert_job.assert_not_called()

    def test_live_delivery_branch_reaches_submission_and_retains_selection(self):
        import json,tempfile,time
        from pathlib import Path
        from job_agent.store import SQLiteDeliveryStore
        from job_agent.config.engine import EngineSettings
        from job_agent.engine.queue import DeliveryEngine
        from job_agent.engine.preflight import FakePreflight
        from job_agent.engine.spaces import FakeTaskSpaces
        from job_agent.engine.models import Status
        a=self.adapter([{'id':2,'resumeDone':True,'defaultResume':True,'attachment':0,'resumeName':'在线简历','uuid':'online-uuid'}],opened=False)
        self.page.responses['nowcoder.application_popup']={'count':1,'job_id':1,'ready':True,'resume_id':2,'resume_done':True,
            'resume_name':'在线简历','resume_attachment':0,'resume_uuid':'online-uuid','forms':False,'submit_count':1,
            'success':True,'ack':True,'deliver_id':3,'successful_resume_id':2}
        self.page.responses['nowcoder.application_records']={'status':200,'code':0,'data':{'deliverData':[
            {'jobId':1,'deliverId':3,'transferList':[{'subStageName':'投递成功','createTime':time.time()*1000}]}],'pageInfo':{}}}
        click=self.page.click
        def native_css_click(selector):
            from job_agent.browser.base import BrowserError
            if ':has-text(' in selector:
                raise BrowserError('Ego click.target_geometry failed: selector is not native CSS')
            click(selector)
        self.page.click=native_css_click
        cfg=EngineSettings(dry_run=False,live_send_enabled=True,platforms={'nowcoder':dict(enabled=True,greeting_mode='none',greeting_max_length=1)})
        job=Job('nowcoder','1','https://www.nowcoder.com/jobs/detail/1')
        with tempfile.TemporaryDirectory() as tmp,SQLiteDeliveryStore(Path(tmp)/'live.db',dry_run=False) as store:
            store.upsert_job(platform='nowcoder',platform_job_id='1',url=job.url,company='Example',title='AI')
            token=store.create_batch([job],{'nowcoder':''},'',cfg.model_dump())
            store.confirm_batch(token,config=cfg.model_dump());store.prepare_run(cfg.model_dump(),batch_token=token)
            # Exercise the live-only enrichment branch with an offline adapter page.
            DeliveryEngine(store,cfg,{'nowcoder':a},FakePreflight(),FakeTaskSpaces())._deliver(job,cfg.platforms['nowcoder'],'test')
            self.assertEqual(store.records()[0].status,Status.SENT)
            self.assertEqual([x[1] for x in self.page.actions if x[0]=='click'],
                [flow.ENTRY,flow.SUBMIT])
            self.assertEqual(json.loads(store.list_deliveries()[-1]['error_detail'])['resume_selection']['selected']['id'],'2')
            self.assertEqual(store.get_job(1)['company'],'Example')

    def test_popup_projection_reads_success_after_form_is_replaced(self):
        import json,shutil,subprocess
        node=shutil.which('node')
        if not node:self.skipTest('Node unavailable for the DOM projection contract')
        script=r'''
const dialog={getClientRects:()=>[{}],querySelector:()=>null,querySelectorAll:()=>[],innerText:'投递成功',
 parentElement:{__vue__:{$options:{name:'DeliveryResumePopup'},jobId:1,loading:false,submitLoading:false,
 checkedResume:{id:2,resumeName:'在线简历',resumeDone:true,attachment:0,uuid:'online-uuid'},
 deliverySucceeded:true,serverAcknowledged:true,successfulDeliveryId:3,successfulResume:{id:2}}}};
const document={querySelectorAll:()=>[dialog]};
console.log(JSON.stringify((PROJECTION)()));
'''.replace('PROJECTION',flow.POPUP_JS)
        result=subprocess.run([node,'-e',script],check=True,capture_output=True,text=True)
        data=json.loads(result.stdout)
        self.assertTrue(data.get('success'))
        self.assertTrue(data.get('ack'))
        self.assertEqual((data.get('job_id'),data.get('deliver_id'),data.get('successful_resume_id')),(1,3,2))
