import json
import subprocess
import unittest
from unittest.mock import patch
from job_agent.platforms.boss import pages


class BossClosureEvidenceTests(unittest.TestCase):
    def project(self,text,visible=True,title='岗位招聘',body=''):
        script='''const status={innerText:TEXT,getClientRects:()=>VISIBLE?[{}]:[]};
        globalThis.document={title:TITLE,body:{innerText:BODY},querySelectorAll:s=>s==='.job-banner .job-status'?[status]:[]};
        globalThis.location={pathname:'/job_detail/test.html'};
        globalThis.getComputedStyle=()=>({visibility:'visible'});
        console.log(JSON.stringify((PROJECTION)()));'''
        for k,v in {'TEXT':json.dumps(text),'VISIBLE':json.dumps(visible),'TITLE':json.dumps(title),'BODY':json.dumps(body),'PROJECTION':pages.STATE_JS}.items():script=script.replace(k,v)
        return json.loads(subprocess.check_output(['node','-e',script],text=True))

    def test_closed_visible_status_retains_exact_evidence(self):
        data=self.project('职位已关闭')
        with self.assertRaises(pages.JobUnavailable) as ctx:pages.guard(data)
        self.assertIn('.job-banner .job-status',str(ctx.exception))
        self.assertIn('职位已关闭',str(ctx.exception))

    def test_hidden_status_or_unrelated_body_is_not_closure(self):
        for text,visible in [('职位已关闭',False),('招聘中',True),('提示：某职位已关闭',True)]:
            data=self.project(text,visible,body='推荐岗位已关闭，当前岗位仍招聘')
            self.assertFalse(data['unavailable'])
            pages.guard(data)

    def test_missing_page_retains_title_and_visible_text(self):
        data=self.project('',title='您访问的页面不存在-BOSS直聘',body='您访问的页面不存在～')
        with self.assertRaises(pages.JobUnavailable) as ctx:pages.guard(data)
        self.assertIn('document.title',str(ctx.exception));self.assertIn('页面不存在',str(ctx.exception))

    def test_readiness_uses_scoped_projection_and_keeps_evidence(self):
        from job_agent.platforms.availability import wait_job_ready,JobUnavailable
        from unittest.mock import Mock
        page=Mock()
        page.evaluate.return_value={'state':'unavailable','evidence':{'selector':'.job-banner .job-status','text':'职位已关闭'}}
        with self.assertRaises(JobUnavailable) as ctx:wait_job_ready(page,pages.JOB_READY,state_js=pages.STATE_JS)
        self.assertIn('职位已关闭',str(ctx.exception));self.assertIn('.job-banner .job-status',str(ctx.exception))
        page.wait_for.assert_not_called()

    def test_terminal_skip_evidence_survives_database_restart(self):
        import tempfile
        from pathlib import Path
        from job_agent.store import SQLiteDeliveryStore
        from job_agent.config.engine import EngineSettings
        from job_agent.engine.models import Job,Status,Reason
        from job_agent.engine.queue import DeliveryEngine
        from job_agent.engine.adapters import FakeAdapter
        from job_agent.engine.preflight import FakePreflight
        from job_agent.engine.spaces import FakeTaskSpaces
        cfg=EngineSettings(platforms={'boss':{'enabled':True,'greeting':'hi','greeting_max_length':10,'interval_seconds':0}})
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'db'
            with SQLiteDeliveryStore(path) as store:
                adapter=FakeAdapter();job=Job('boss','test','https://example.invalid/test')
                engine=DeliveryEngine(store,cfg,{'boss':adapter},FakePreflight(),FakeTaskSpaces())
                engine.enqueue([job])
                with patch.object(adapter,'open_job',side_effect=pages.JobUnavailable('.job-banner .job-status：职位已关闭')):engine.run()
                row=store.records()[0]
                self.assertEqual((row.status,row.reason),(Status.SKIPPED,Reason.JOB_UNAVAILABLE))
                self.assertNotIn('send_greeting',adapter.calls)
            with SQLiteDeliveryStore(path) as store:
                self.assertIn('职位已关闭',store.records()[0].error_detail)
                self.assertIn('.job-banner .job-status',store.list_deliveries()[-1]['error_detail'])
