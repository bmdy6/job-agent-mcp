import unittest
from job_agent.browser.base import FakePage, FakeSpace
from job_agent.platforms.base import SearchQuery, JobListing

class MaimaiTests(unittest.TestCase):
    def test_parse_public_cards(self):
        from job_agent.platforms.maimai.pages import parse_search
        data={'ready':True,'has_next':True,'items':[{'url':'https://maimai.cn/n/job/geo/Ab_-12',
          'title':'AI应用工程师','company':'示例','city':'苏州','salary':'15k-25k',
          'experience':'经验不限','education':'本科及以上'}]}
        row=parse_search(data,SearchQuery('AI','苏州'))[0]
        self.assertEqual((row.platform_job_id,row.experience,row.hr_activity),('Ab_-12','经验不限','unknown'))

    def test_filtered_page_does_not_claim_exhaustion(self):
        from job_agent.platforms.maimai.pages import parse_search, Unverified
        with self.assertRaises(Unverified):parse_search({'ready':True,'has_next':True,'items':[]},SearchQuery('AI','苏州'))
        self.assertEqual(parse_search({'ready':True,'has_next':False,'items':[]},SearchQuery('AI','苏州')),[])

    def test_reject_redirect_and_close_on_error(self):
        from job_agent.platforms.maimai.search import JobSource
        from job_agent.platforms.maimai.pages import Unverified, clean_job_url
        with self.assertRaises(ValueError):clean_job_url('https://evil.example/n/job/geo/Ab_-12')
        p=FakePage('p1',{'maimai.guard':{'url':'https://evil.example/'}})
        with self.assertRaises(Unverified):JobSource().search(FakeSpace('test',pages=[p]),SearchQuery('AI','苏州'))
        self.assertTrue(p.closed)

    def test_detail_identity(self):
        from job_agent.platforms.maimai.pages import parse_detail,Unverified
        job=JobListing('maimai','Ab_-12','https://maimai.cn/n/job/geo/Ab_-12','示例','AI')
        with self.assertRaises(Unverified):parse_detail({'url':'https://maimai.cn/n/job/geo/Other','jd':'职责'},job)

    def test_defaults_and_refusal(self):
        from job_agent.config import Settings
        from job_agent.platforms.maimai.apply import MaimaiAdapter
        from job_agent.platforms.maimai.replies import ReplySource
        cfg=Settings()
        from job_agent.engine.rate import RatePolicy
        typed=Settings.model_validate({'engine':{'platforms':{'maimai':{'rate':RatePolicy(hourly_limit=1)}}}})
        self.assertEqual(typed.engine.platforms['maimai'].rate.hourly_limit,1)
        self.assertFalse(cfg.engine.platforms['maimai'].enabled)
        self.assertFalse(cfg.collect.platforms['maimai'].enabled)
        self.assertNotIn('maimai',cfg.engine.monitor.platforms)
        self.assertEqual(cfg.engine.platforms['maimai'].rate_policy().hourly_limit,2)
        with self.assertRaises(NotImplementedError):MaimaiAdapter(None,None).open_job(None,'test')
        with self.assertRaises(NotImplementedError):ReplySource().fetch(FakeSpace('test'))
