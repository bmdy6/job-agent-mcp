import unittest
from unittest.mock import Mock


class HistoryTabsTests(unittest.TestCase):
    def test_scans_every_status_tab_and_pagination_without_visiting_jobs(self):
        from job_agent.platforms.zhilian.reconciliation import read_history, FILTERS
        class Page:
            def __init__(self):self.tab=FILTERS[0];self.number=1;self.actions=[]
            def evaluate(self,script):
                if 'zhilian.reconcile_settle' in script:return {'settled':True}
                if 'zhilian.state' in script:return {}
                if 'zhilian.reconcile_next' in script:return self.tab==FILTERS[0] and self.number==1
                if 'zhilian.application_records' in script:
                    return {'origin':'https://i.zhaopin.com','path':'/schedule','rows':[{
                        'url':f'https://www.zhaopin.com/jobdetail/{self.tab}{self.number}.htm','time':'2026-11-27 10:00:00','status':self.tab}]}
                raise AssertionError(script[:80])
            def click_projected(self,script):
                self.actions.append('filter')
                self.tab=next(t for t in FILTERS if 'const label="'+t+'"' in script);self.number=1
            def click(self,selector):self.actions.append('next');self.number+=1
        page=Page();data=read_history(page)
        self.assertEqual(len(data['rows']),6)
        self.assertEqual(page.actions,['next']+['filter']*4)

    def test_unsettled_filter_is_unknown_instead_of_old_filter_data(self):
        from job_agent.platforms.zhilian.reconciliation import read_history
        from job_agent.browser.base import PageTimeout
        page=Mock()
        page.evaluate.side_effect=lambda s: ({'rows':[],'origin':'https://i.zhaopin.com','path':'/schedule'}
            if 'application_records' in s else False if 'reconcile_next' in s else {'settled':False} if 'reconcile_settle' in s else {})
        with self.assertRaises(PageTimeout):read_history(page)

    def test_view_time_never_becomes_application_time(self):
        from job_agent.platforms.reconciliation import application_outcome
        job=dict(platform_job_id='J1',company='公司',title='岗位',attempted_at=1795744800)
        data={'origin':'https://i.zhaopin.com','path':'/schedule','rows':[dict(url='https://www.zhaopin.com/jobdetail/J1.htm',
            company='公司',title='岗位',status='被HR查看',time='2026-11-27 10:00:05')]}
        result=application_outcome(job,data,checked_epoch=1795745400)
        self.assertEqual(result['status'],'still_unknown')
        self.assertEqual(result['evidence']['code'],'application_time_unavailable')

class SettleProjectionTests(unittest.TestCase):
    def test_waits_for_new_rows_and_does_not_rewrite_record_text_as_code(self):
        import json
        import subprocess
        from job_agent.platforms.zhilian.reconciliation import settle_script
        old=[dict(title='READ STATE BEFORE LABEL',url='https://example.test/job',time='old')]
        script='''let tick=0;global.setTimeout=f=>{tick++;queueMicrotask(f)};
        global.document={querySelectorAll:()=>[{innerText:'被查看'}]};
        const observedRows=()=>({rows:tick<2?OLD:[{time:'new'}]});const guard=()=>({});
        '''.replace('OLD',json.dumps(old))
        # Run the real settling algorithm with controlled DOM projections.
        from job_agent.platforms.zhilian import pages
        projection=settle_script('被查看',json.dumps(old,ensure_ascii=False,separators=(',',':')))
        projection=projection.replace(pages.APPLICATIONS_JS,'observedRows').replace(pages.STATE_JS,'guard')
        script+='\nconst result=await ('+projection+')();console.log(JSON.stringify({result,tick}));'
        result=json.loads(subprocess.check_output(['node','--input-type=module'],input=script,text=True,timeout=5))
        self.assertTrue(result['result']['settled'])
        self.assertGreaterEqual(result['tick'],5)
