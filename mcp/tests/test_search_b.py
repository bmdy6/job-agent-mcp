import json
from pathlib import Path
import unittest
from job_agent.browser.base import BrowserError, FakePage, FakeSpace
from job_agent.platforms.base import SearchQuery
from job_agent.platforms.liepin.search import LiepinJobSource
from job_agent.platforms.job51.search import Job51JobSource
from job_agent.platforms.liepin import pages as lp
from job_agent.platforms.job51 import pages as jp

ROOT=Path(__file__).parent/'fixtures/platforms'

def fixture(platform,name):
    return json.loads((ROOT/platform/f'search-{name}.json').read_text())

class SearchBTests(unittest.TestCase):
    def test_list_and_detail_fixtures(self):
        for platform,cls,mod in [('liepin',LiepinJobSource,lp),('job51',Job51JobSource,jp)]:
            with self.subTest(platform=platform):
                data=fixture(platform,'list')
                jobs=mod.parse_search(data,SearchQuery('AI应用开发','上海'))
                self.assertEqual(len(jobs),2)
                self.assertEqual(jobs[0].city,'上海-浦东新区' if platform=='liepin' else '上海·奉贤区')
                self.assertEqual(jobs[0].platform_job_id,data['items'][0]['id'])
                detail=fixture(platform,'detail')
                self.assertEqual(mod.parse_detail(detail,jobs[0]),detail['jd'])
                for raw in [dict(data,ready=False),{},None]:
                    with self.assertRaises(BrowserError):mod.parse_search(raw,SearchQuery('AI','上海'))
                with self.assertRaises(BrowserError):mod.parse_detail(dict(detail,id='999'),jobs[0])

    def test_end_and_wrong_page(self):
        for mod in (lp,jp):
            q=SearchQuery('AI','上海',page=3)
            self.assertEqual(mod.parse_search(dict(ready=True,loggedIn=True,risk=False,page=3,empty=True,items=[]),q),[])
            with self.assertRaises(BrowserError):mod.parse_search(dict(ready=True,loggedIn=True,page=1,items=[]),q)
            with self.assertRaises(BrowserError):mod.parse_search(dict(ready=True,loggedIn=True,page=3,items=[]),q)

    def test_guards_and_cleanup(self):
        for cls,mod in [(LiepinJobSource,lp),(Job51JobSource,jp)]:
            for bad,code in [(dict(risk=True),'risk_control'),(dict(loggedIn=False),'login_required')]:
                p=FakePage('p2',{'collect.':dict(ready=True,**bad)})
                with self.assertRaises(BrowserError) as exc:cls().search(FakeSpace('test',pages=[p]),SearchQuery('AI','上海'))
                self.assertEqual(exc.exception.code,code)
                self.assertTrue(p.closed)
                self.assertFalse(any(a[0] in ('click','fill','press') for a in p.actions))
            for city in ('未知城',''):
                with self.assertRaises(ValueError):mod.search_url(SearchQuery('AI',city))
            with self.assertRaises(ValueError):mod.search_url(SearchQuery('AI','上海',page=0))

    def test_liepin_agency_links_keep_cid_identity(self):
        data = fixture('liepin', 'list')
        data['items'][0]['url'] = 'https://www.liepin.com/a/81705229.shtml'
        job = lp.parse_search(data, SearchQuery('AI', '苏州'))[0]
        self.assertEqual(job.platform_job_id, data['items'][0]['id'])
        self.assertEqual(job.url, data['items'][0]['url'])
        with self.assertRaises(BrowserError):
            lp.clean_job_url('https://www.liepin.com/a/anything.shtml')

    def test_51job_company_microsite_does_not_drop_regular_jobs(self):
        data = fixture('job51', 'list')
        data['items'][0]['url'] = 'https://mahle.51job.com/jobdetail.html'
        jobs = jp.parse_search(data, SearchQuery('AI', '苏州'))
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].platform_job_id, data['items'][1]['id'])
        data['items'] = data['items'][:1]
        with self.assertRaises(BrowserError):
            jp.parse_search(data, SearchQuery('AI', '苏州'))

    def test_registry(self):
        from job_agent.platforms.registry import SOURCES,create_sources
        self.assertIs(SOURCES['liepin'],LiepinJobSource)
        self.assertIs(SOURCES['51job'],Job51JobSource)
        self.assertEqual(create_sources()['51job'].platform,'51job')

    def test_sources_success_and_detail_are_read_only(self):
        for platform,cls,mod in [('liepin',LiepinJobSource,lp),('job51',Job51JobSource,jp)]:
            p=FakePage('p2',{'collect.':fixture(platform,'list')})
            q=SearchQuery('AI应用开发','上海')
            jobs=cls().search(FakeSpace('test',pages=[p]),q)
            self.assertTrue(p.closed)
            self.assertEqual(len(jobs),2)
            detail=FakePage('p3',{'collect.':fixture(platform,'detail')})
            self.assertEqual(cls().detail(FakeSpace('test',pages=[detail]),jobs[0]),fixture(platform,'detail')['jd'])
            self.assertTrue(detail.closed)
            for page in (p,detail):
                self.assertFalse(any(a[0] in ('click','fill','press') for a in page.actions))
            for url in ('https://example.com/1','https://jobs.51job.com.evil/1.html','javascript:alert(1)'):
                with self.assertRaises(BrowserError):mod.clean_job_url(url)
            for state in (dict(risk=True),dict(loggedIn=False),dict(ready=False)):
                bad=FakePage('p4',{'collect.':dict(fixture(platform,'detail'),**state)})
                with self.assertRaises(BrowserError):cls().detail(FakeSpace('test',pages=[bad]),jobs[0])
                self.assertTrue(bad.closed)

    def test_second_page_fixture_and_51job_turn(self):
        from unittest.mock import patch
        from urllib.parse import parse_qs,urlsplit
        for platform,cls,mod in [('liepin',LiepinJobSource,lp),('job51',Job51JobSource,jp)]:
            data=fixture(platform,'page2')
            q=SearchQuery('AI应用开发','上海',page=2)
            self.assertEqual(len(mod.parse_search(data,q)),2)
            params=parse_qs(urlsplit(mod.search_url(q)).query)
            self.assertEqual(params['currentPage' if platform=='liepin' else 'pageNum'],['1' if platform=='liepin' else '2'])
        p=FakePage('p2',{'collect.job51.search':fixture('job51','list'),'collect.job51.pagination':fixture('job51','page2')})
        # FakePage matches in insertion order; pagination contains the search expression too.
        p.responses={'collect.job51.pagination':fixture('job51','page2'),'collect.job51.search':fixture('job51','list')}
        with patch('job_agent.platforms.job51.search.time.sleep') as sleep:
            self.assertEqual(len(Job51JobSource().search(FakeSpace('test',pages=[p]),q)),2)
            self.assertGreaterEqual(sleep.call_args.args[0],8)
            self.assertLessEqual(sleep.call_args.args[0],15)
        self.assertTrue(p.closed)

    def test_explicit_end_cleans_up_and_invalid_rows_stop(self):
        for platform,cls,mod in [('liepin',LiepinJobSource,lp),('job51',Job51JobSource,jp)]:
            data=dict(ready=True,loggedIn=True,page=1,empty=True,items=[])
            p=FakePage('p2',{'collect.':data})
            self.assertEqual(cls().search(FakeSpace('test',pages=[p]),SearchQuery('AI','上海')),[])
            self.assertTrue(p.closed)
            rows=fixture(platform,'list')
            rows['items'][0]['id']=''
            with self.assertRaises(BrowserError):mod.parse_search(rows,SearchQuery('AI','上海'))
        for interval in [(0,1),(15,8),(8,float('nan'))]:
            with self.assertRaises(ValueError):Job51JobSource(interval_seconds=interval)
