import importlib
import unittest
from unittest.mock import patch
from job_agent.browser.base import BrowserError, FakePage, FakeSpace
from job_agent.platforms.base import SearchQuery


class CollectATests(unittest.TestCase):
    def test_registry(self):
        from job_agent.platforms.registry import create_sources
        self.assertTrue({'boss', 'zhilian'} <= set(create_sources()))

    def test_url(self):
        for platform in ('boss', 'zhilian'):
            p = importlib.import_module(f'job_agent.platforms.{platform}.pages')
            self.assertIn('AI%E5%BA%94', p.search_url(SearchQuery('AI应用开发', '上海')))
            with self.assertRaises(ValueError):
                p.search_url(SearchQuery('AI', '未知城市'))

    def test_fail_closed_and_cleanup(self):
        for platform in ('boss', 'zhilian'):
            module = importlib.import_module(f'job_agent.platforms.{platform}.search')
            for data in ({'risk': True}, {'login_required': True}, {}, {'rows': []}):
                page = FakePage('p2', {'collect': data})
                space = FakeSpace('test', pages=[page])
                with patch.object(module.time, 'monotonic', side_effect=[0, 11]), self.assertRaises(BrowserError):
                    module.JobSource().search(space, SearchQuery('AI', '上海'))
                self.assertTrue(page.closed)

    def test_terminal(self):
        for platform in ('boss', 'zhilian'):
            module = importlib.import_module(f'job_agent.platforms.{platform}.search')
            page = FakePage('p2', {'collect': {'rows': [], 'has_more': False, 'ready': True}})
            self.assertEqual([], module.JobSource().search(FakeSpace('test', pages=[page]), SearchQuery('AI', '上海')))
            self.assertTrue(page.closed)

    def test_salary(self):
        from job_agent.platforms.boss.pages import salary_text
        self.assertEqual('18-35K·14薪', salary_text('\ue032\ue039-\ue034\ue036K·\ue032\ue035薪', '18-35K·14薪'))
        self.assertEqual('20-30K', salary_text('20-30K', ''))
        with self.assertRaises(BrowserError):
            salary_text('\ue032\ue039K', '')


class CollectAFixtureTests(unittest.TestCase):
    def load(self, platform, name):
        import json
        from pathlib import Path
        return json.loads((Path(__file__).parent / 'fixtures/platforms' / platform / name).read_text())

    def test_real_projections(self):
        for platform in ('boss', 'zhilian'):
            p = importlib.import_module(f'job_agent.platforms.{platform}.pages')
            m = importlib.import_module(f'job_agent.platforms.{platform}.search')
            data = self.load(platform, 'search-list.json')
            rows = p.search_rows(data)
            self.assertEqual(2, len(rows))
            self.assertEqual('collect-example-1', rows[0].platform_job_id)
            self.assertEqual('上海', rows[0].city)
            self.assertNotIn('\ue032', rows[0].salary)
            detail = self.load(platform, 'search-detail.json')
            page = FakePage('p2', {'collect': detail})
            source = m.JobSource()
            self.assertEqual(detail['jd'], source.detail(FakeSpace('test', pages=[page]), rows[0]))
            self.assertTrue(page.closed)
            self.assertFalse(any(a[0] in ('click', 'fill', 'press') for a in page.actions))
            with self.assertRaises(BrowserError):
                p.detail_text(detail, 'different-job')
            for flag in ('risk', 'login_required'):
                page = FakePage('p2', {'collect': {flag: True}})
                with self.assertRaises(BrowserError):
                    source.detail(FakeSpace('test', pages=[page]), rows[0])
                self.assertTrue(page.closed)

    def test_scroll_to_end(self):
        for platform in ('boss', 'zhilian'):
            m = importlib.import_module(f'job_agent.platforms.{platform}.search')
            data = self.load(platform, 'search-list.json')
            data['has_more'] = False
            page = FakePage('p2', {'collect': data})
            self.assertEqual([], m.JobSource().search(FakeSpace('test', pages=[page]), SearchQuery('AI', '上海', page=2)))
            self.assertFalse(any(a[0] in ('click', 'fill', 'press') for a in page.actions))

    def test_dedup_and_bad_id(self):
        for platform in ('boss', 'zhilian'):
            p = importlib.import_module(f'job_agent.platforms.{platform}.pages')
            data = self.load(platform, 'search-list.json')
            data['rows'] = [data['rows'][0], data['rows'][0]]
            self.assertEqual(1, len(p.search_rows(data)))
            data['rows'][0]['url'] = 'https://example.com/job/123'
            with self.assertRaises(BrowserError):
                p.search_rows(data)

class CollectAPaginationTests(unittest.TestCase):
    def test_partial_last_batch_and_cleanup(self):
        for platform in ('boss', 'zhilian'):
            m = importlib.import_module(f'job_agent.platforms.{platform}.search')
            p = importlib.import_module(f'job_agent.platforms.{platform}.pages')
            def row(i):
                return {'id': str(i), 'url': p.JOB_ORIGIN + p.JOB_CANONICAL.format(jid=i),
                        'title': 'AI', 'company': '示例', 'salary': '20K'}
            first = {'ready': True, 'has_more': True, 'rows': [row(i) for i in range(p.PAGE_SIZE)]}
            last = {'ready': True, 'has_more': False, 'rows': first['rows'] + [row(100), row(101)]}
            class ScrollPage(FakePage):
                def evaluate(self, js):
                    super().evaluate(js)
                    if 'collect.scroll' in js:
                        self.scrolled = True
                        return None
                    return last if getattr(self, 'scrolled', False) else first
            page = ScrollPage('p2')
            with patch.object(m.time, 'sleep'), patch.object(m.random, 'uniform', return_value=9):
                rows = m.JobSource().search(FakeSpace('test', pages=[page]), SearchQuery('AI', '上海', page=2))
            self.assertEqual(['100', '101'], [r.platform_job_id for r in rows])
            self.assertTrue(page.closed)
            self.assertEqual(1, sum(a[0] == 'goto' for a in page.actions))

    def test_no_more_and_risk_after_scroll(self):
        for platform in ('boss', 'zhilian'):
            m = importlib.import_module(f'job_agent.platforms.{platform}.search')
            class RiskPage(FakePage):
                def evaluate(self, js):
                    if 'collect.scroll' in js:
                        self.scrolled = True
                        return None
                    return {'risk': True} if getattr(self, 'scrolled', False) else {
                        'ready': True, 'has_more': True, 'rows': [{}]}
            page = RiskPage('p2')
            with patch.object(m.time, 'sleep'), self.assertRaises(BrowserError) as exc:
                m.JobSource().search(FakeSpace('test', pages=[page]), SearchQuery('AI', '上海', page=2))
            self.assertEqual('risk_control', exc.exception.code)
            self.assertTrue(page.closed)

    def test_navigation_failure_closes_page(self):
        for platform in ('boss', 'zhilian'):
            m = importlib.import_module(f'job_agent.platforms.{platform}.search')
            page = FakePage('p2')
            with patch.object(page, 'goto', side_effect=BrowserError('navigation failed')) as goto:
                with self.assertRaises(BrowserError):
                    m.JobSource().search(FakeSpace('test', pages=[page]), SearchQuery('AI', '上海'))
                goto.assert_called_once()
            self.assertTrue(page.closed)


if __name__ == '__main__':
    unittest.main()
