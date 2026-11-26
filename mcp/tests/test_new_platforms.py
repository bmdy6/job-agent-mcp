import unittest
from browser.base import FakePage, FakeSpace, RiskControl
from platforms.base import SearchQuery, JobListing


class NewSources(unittest.TestCase):
    def test_nowcoder_filters_recommendations(self):
        from platforms.nowcoder.pages import parse_search
        rows = [{'url':'https://www.nowcoder.com/jobs/detail/475028','title':'AI应用开发',
                 'company':'示例','salary':'11-15K','city':'苏州','education':'本科',
                 'experience':'2026届','activity':'HR今日在线'}]
        self.assertEqual(len(parse_search({'ready':True,'items':rows},SearchQuery('AI应用','苏州'))),1)
        for change in ({'city':'南京'},{'title':'测试工程师'},
                       {'url':'https://evil.example/jobs/detail/475028'}):
            self.assertEqual(parse_search({'ready':True,'items':[{**rows[0],**change}]},SearchQuery('AI应用','苏州')),[])

    def test_nowcoder_drift_is_error(self):
        from platforms.nowcoder.pages import parse_search, Unverified
        with self.assertRaises(Unverified): parse_search(None,SearchQuery('AI','苏州'))
        with self.assertRaises(Unverified): parse_search({'ready':False,'items':[]},SearchQuery('AI','苏州'))
        with self.assertRaises(RiskControl): parse_search({'risk':True},SearchQuery('AI','苏州'))

    def test_detail_rejects_wrong_identity(self):
        from platforms.nowcoder.pages import parse_detail, Unverified
        listing=JobListing('nowcoder','1','https://www.nowcoder.com/jobs/detail/1','示例','AI')
        with self.assertRaises(Unverified):parse_detail({'url':'https://www.nowcoder.com/jobs/detail/2','jd':'完整职责'},listing)

if __name__=='__main__':unittest.main()

class NowcoderWiring(unittest.TestCase):
    def test_new_platform_defaults_disabled_and_conservative(self):
        from config import Settings
        cfg=Settings()
        explicit=Settings.model_validate({'engine':{'platforms':{'nowcoder':{}}},'collect':{'platforms':{'nowcoder':{}}}})
        self.assertEqual(explicit.engine.platforms['nowcoder'].daily_limit,2)
        self.assertEqual(explicit.collect.platforms['nowcoder'].rate.hourly_limit,2)
        self.assertFalse(cfg.engine.platforms['nowcoder'].enabled)
        self.assertEqual(cfg.engine.platforms['nowcoder'].daily_limit,2)
        self.assertGreaterEqual(cfg.engine.platforms['nowcoder'].rate_policy().interval_seconds[0],120)
        self.assertFalse(cfg.collect.platforms['nowcoder'].enabled)
        self.assertNotIn('nowcoder',cfg.engine.monitor.platforms)

    def test_roster_empty_does_not_touch_cursor(self):
        from unittest.mock import Mock
        from platforms.nowcoder.replies import ReplySource
        page=FakePage('p1',{'nowcoder.guard':{'logged_in':True},'nowcoder.roster':{
            'status':200,'code':0,'data':{'datas':[],'totalCount':0,'totalPage':0}}})
        cursor=Mock()
        self.assertEqual(ReplySource().fetch(FakeSpace('test',pages=[page]),cursor=cursor),[])
        self.assertEqual(cursor.mock_calls,[])
        self.assertTrue(page.closed)
        self.assertFalse(any(a[0]=='click' for a in page.actions))

    def test_populated_roster_is_never_acked_or_opened(self):
        from unittest.mock import Mock
        from platforms.nowcoder.replies import ReplySource
        from platforms.nowcoder.pages import Unverified
        page=FakePage('p1',{'nowcoder.guard':{'logged_in':True},'nowcoder.roster':{
            'status':200,'code':0,'data':{'datas':[{'id':1}],'totalCount':1,'totalPage':1}}})
        cursor=Mock()
        with self.assertRaises(Unverified):ReplySource().fetch(FakeSpace('test',pages=[page]),cursor=cursor)
        self.assertEqual(cursor.mock_calls,[])
        self.assertTrue(page.closed)

    def test_source_cleanup_on_risk_and_query_failure(self):
        from platforms.nowcoder.search import JobSource
        page=FakePage('p1',{'nowcoder.guard':{'risk':True}})
        steps=JobSource().search_steps(FakeSpace('test',pages=[page]),SearchQuery('AI','苏州'))
        with self.assertRaises(RiskControl):next(steps)
        self.assertTrue(page.closed)
        self.assertEqual(JobSource.search_page_cost(SearchQuery('AI','苏州',2)),2)
