import os
from unittest.mock import patch
import tempfile
import unittest
from pathlib import Path

from browser.base import BrowserError, FakeBrowser
from config import Settings
from collect.config import CollectSettings
from collect.runner import Collector
from platforms.base import JobListing
from store import Store
from store.collection import CollectionRepository


class Source:
    def __init__(self, platform='boss', fail=False):
        self.platform, self.fail = platform, fail
        self.calls, self.details = [], []

    def search(self, space, query):
        self.calls.append(query)
        if self.fail:
            raise BrowserError('risk')
        if query.page > 2:
            return []
        return [JobListing(self.platform, '1', 'https://example.test/1', '公司', 'AI工程师', salary='10-20K'),
                JobListing(self.platform, '2', 'https://example.test/2', '外包', '销售', salary='5-7K')]

    def detail(self, space, listing):
        self.details.append(listing.platform_job_id)
        return 'JD正文'


class CollectTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ, JOB_AGENT_HOME=self.temp.name)
        env.start(); self.addCleanup(env.stop)
        self.path = Path(self.temp.name)/'jobs.sqlite3'
        self.store = Store(self.path)
        self.addCleanup(self.store.close)
        self.repo = CollectionRepository(self.store)
        self.browser = FakeBrowser()
        self.clock = [1795478400.0]
        self.sleeps = []
        self.settings = CollectSettings(keywords=['AI'], cities=['苏州'], fetch_jd=True,
            platforms={'boss': {'enabled': True, 'daily_page_limit': 20},
                       'liepin': {'enabled': True}},
            filters={'salary_min_k': 8, 'title_exclude': ['销售'], 'blocked_companies': ['外包']})

    def run_collect(self, sources, settings=None):
        def sleep(delay):
            self.sleeps.append(delay)
            self.clock[0] += delay
        return Collector(self.browser, sources, self.repo, settings or self.settings,
                         clock=lambda: self.clock[0], sleep=sleep, alert=lambda *a: None).run()

    def test_dedup_filter_and_details(self):
        source = Source()
        self.run_collect([source])
        self.assertEqual(len(self.store.list_jobs()), 2)
        self.assertEqual(source.details, ['1'])
        self.assertEqual(self.store.get_job(1)['jd'], 'JD正文')
        self.assertEqual(set(self.repo.filters(2)), {'salary_below_min', 'title_excluded', 'company_blocked'})
        self.assertTrue(all(0 < d <= .25 for d in self.sleeps))
        self.assertGreaterEqual(self.clock[0]-1795478400.0,24)
        self.run_collect([source])
        self.assertEqual(len(source.calls), 3)

    def test_budget_resume_next_day(self):
        limited = self.settings.model_copy(deep=True)
        limited.platforms['boss'].daily_page_limit = 1
        source = Source()
        self.run_collect([source], limited)
        self.run_collect([source], limited)
        self.assertEqual([q.page for q in source.calls], [1])
        self.clock[0] += 86400
        self.run_collect([source], limited)
        self.assertEqual([q.page for q in source.calls], [1, 2])

    def test_one_platform_error_does_not_block_other(self):
        bad, good = Source(fail=True), Source('liepin')
        result = self.run_collect([bad, good])
        self.assertEqual(result['platforms']['boss']['error'], 'browser_error')
        self.assertEqual(len(bad.calls), 1)
        self.assertEqual(len(good.calls), 3)
        self.assertEqual(len(self.store.list_jobs(platform='liepin')), 2)

    def test_interrupted_detail_replays_page_without_duplicate(self):
        source = Source()
        def interrupt(*a):
            raise KeyboardInterrupt
        source.detail = interrupt
        with self.assertRaises(KeyboardInterrupt):
            self.run_collect([source])
        source = Source()
        self.run_collect([source])
        self.assertEqual(source.calls[0].page, 1)
        self.assertEqual(len(self.store.list_jobs()), 2)
        self.assertEqual(self.repo.status()['pages_today']['boss'], 4)

    def test_config_validates_interval_and_platform(self):
        with self.assertRaises(ValueError):
            CollectSettings(interval_seconds=[15, 8])
        with self.assertRaises(ValueError):
            CollectSettings(platforms={'unknown': {}})
        self.assertEqual(Settings(collect={}).collect.task_space_name, 'job-agent 采集')

class MoreCollectTest(unittest.TestCase):
    setUp = CollectTest.setUp
    run_collect = CollectTest.run_collect
    def test_restart_uses_persisted_cursor(self):
        limited = self.settings.model_copy(deep=True)
        limited.platforms['boss'].daily_page_limit = 1
        self.run_collect([Source()], limited)
        self.store.close()
        self.store = Store(self.path)
        self.addCleanup(self.store.close)
        self.repo = CollectionRepository(self.store)
        source = Source()
        self.clock[0] += 86400
        self.run_collect([source], limited)
        self.assertEqual(source.calls[0].page, 2)

    def test_failed_request_consumes_quota(self):
        source = Source(fail=True)
        limited = self.settings.model_copy(deep=True)
        limited.platforms['boss'].daily_page_limit = 1
        self.run_collect([source], limited)
        self.run_collect([source], limited)
        self.assertEqual(len(source.calls), 1)
        self.assertEqual(self.repo.status()['pages_today']['boss'], 1)

    def test_lock_prevents_second_worker(self):
        with self.repo.exclusive() as acquired:
            self.assertTrue(acquired)
            result = self.run_collect([Source()])
        self.assertTrue(result['busy'])
        self.assertFalse(self.browser.spaces)

    def test_filter_is_visible_to_existing_tools_and_reversible(self):
        self.run_collect([Source()])
        self.assertEqual(self.store.get_job(2)['legacy_status'], 'filtered')
        listing = JobListing('boss', '2', '', '正常公司', 'AI', salary='10-20K')
        self.repo.ingest(listing, [])
        self.assertIsNone(self.store.get_job(2)['legacy_status'])
        self.assertEqual(self.repo.filters(2), [])

    def test_refresh_preserves_imported_state_unless_hard_filtered(self):
        listing = JobListing('boss', 'old', '', '公司', 'AI', salary='10-20K')
        row_id = self.store.upsert_job(platform='boss', platform_job_id='old',
                                       legacy_status='approved', legacy_score=90, legacy_score_reason='原评分')
        self.repo.ingest(listing, [])
        self.assertEqual(self.store.get_job(row_id)['legacy_status'], 'approved')
        self.assertEqual(self.store.get_job(row_id)['legacy_score_reason'], '原评分')
        self.repo.ingest(listing, ['company_blocked'])
        self.assertEqual(self.store.get_job(row_id)['legacy_status'], 'filtered')
        self.assertEqual(self.store.get_job(row_id)['legacy_score'], 90)

    def test_space_takeover_stops_all_sources(self):
        from browser.base import SpaceUnavailable
        self.browser.unavailable.add(self.settings.task_space_name)
        bad, good = Source(), Source('liepin')
        result = self.run_collect([bad, good])
        self.assertTrue(result['stopped'])
        self.assertFalse(bad.calls + good.calls)

    def test_detail_error_keeps_listing_and_stops_only_platform(self):
        bad, good = Source(), Source('liepin')
        def fail(*a):
            raise BrowserError('risk')
        bad.detail = fail
        result = self.run_collect([bad, good])
        self.assertEqual(result['failed'], 1)
        self.assertEqual(len(bad.calls), 1)
        self.assertEqual(len(good.calls), 3)
        self.assertEqual(self.store.list_jobs(platform='boss')[0]['jd'], '')
        self.assertEqual(len(self.store.list_jobs(platform='boss')), 2)

    def test_completed_queries_do_not_starve_unfinished_query_next_day(self):
        limited = self.settings.model_copy(deep=True)
        limited.keywords = ['AI', 'Agent']
        limited.platforms['boss'].daily_page_limit = 4
        self.run_collect([Source()], limited)
        self.clock[0] += 86400
        source = Source()
        self.run_collect([source], limited)
        self.assertEqual((source.calls[0].keyword, source.calls[0].page), ('Agent', 2))


class RuntimeCollectTest(unittest.TestCase):
    def test_dry_run_never_uses_live_browser_or_production_db(self):
        import os
        from unittest.mock import patch
        from collect.runtime import collect_once
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {'JOB_AGENT_HOME': home}), \
                patch('browser.ego.EgoBrowser', side_effect=AssertionError('live browser')), \
                patch('notify.send_one', side_effect=AssertionError('notification')):
            Path(home, 'config.yaml').write_text('collect:\n  keywords: [AI]\n  cities: [苏州]\n')
            result = collect_once(['boss'], dry_run=True)
            self.assertEqual(result['failed'], 0)
            self.assertFalse(Path(home, 'jobs.sqlite3').exists())
            with Store(Path(home, 'collect-dry-run.sqlite3'), dry_run=True) as store:
                self.assertEqual(len(store.list_jobs()), 1)

    def test_telegram_alert_only_enabled_telegram_channels(self):
        from unittest.mock import patch
        from collect.runtime import telegram_alert
        config = Settings(notify={'enabled': True, 'channels': {
            'tg': {'type': 'telegram', 'enabled': True, 'chat_id': 'test'},
            'off': {'type': 'telegram', 'enabled': False},
            'web': {'type': 'webhook', 'enabled': True}}})
        with patch('notify.send_one', return_value={'success': True}) as send:
            telegram_alert(config, 'boss', 'browser_error')
            self.assertEqual(send.call_count, 1)

    def test_service_launch_and_status_do_not_access_browser(self):
        import os
        from unittest.mock import patch
        from service.collection import start_collection, collection_status
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {'JOB_AGENT_HOME': home}):
            Path(home, 'config.yaml').write_text('collect:\n  platforms:\n    boss:\n      enabled: true\n')
            self.assertFalse(collection_status()['running'])
            with patch('service.collection.subprocess.Popen') as popen:
                popen.return_value.pid = 42
                result = start_collection(['boss'])
                self.assertEqual(result['pid'], 42)
                self.assertIn('--once', popen.call_args.args[0])


class FilterCollectTest(unittest.TestCase):
    def test_monthly_annual_and_unknown_salary(self):
        from collect.filters import salary_lower_k
        for raw, expected in [('15-24K·14薪',15), ('0.7-1.2万/月',7), ('12-24万/年',10),
                              ('7000-9000元/月',7), ('面议',None), ('300元/天',None), ('50元/小时',None)]:
            with self.subTest(raw=raw):
                self.assertEqual(salary_lower_k(raw), expected)

    def test_registry_factory_does_not_receive_incompatible_arguments(self):
        from unittest.mock import patch
        from collect.runtime import create_sources
        from platforms import registry
        source = Source()
        with patch.object(registry, 'create_sources', create=True, return_value={'boss':source}) as factory:
            sources = create_sources(FakeBrowser(), CollectSettings(), ['boss','liepin'])
            factory.assert_called_once_with()
            self.assertIs(sources[0], source)
            self.assertEqual(sources[1].platform, 'liepin')
