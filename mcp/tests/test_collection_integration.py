import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from job_agent.config import Settings, load_config
from job_agent.platforms.base import SearchQuery
from job_agent.platforms.registry import create_sources
import test_collect
from test_collect import Source

class BudgetIntegrationTest(unittest.TestCase):
    setUp = test_collect.CollectTest.setUp
    run_collect = test_collect.CollectTest.run_collect

    def test_replayed_batches_reserved_before_io(self):
        for name, expected in [('boss', 3), ('zhilian', 3), ('51job', 2), ('liepin', 1)]:
            self.assertEqual(create_sources()[name].search_page_cost(SearchQuery('AI', '苏州', 3)), expected)
        source = Source()
        source.search_page_cost = lambda q: q.page
        settings = self.settings.model_copy(deep=True)
        settings.fetch_jd = False
        settings.platforms['boss'].daily_page_limit = 2
        result = self.run_collect([source], settings)
        self.assertEqual([q.page for q in source.calls], [1])
        self.assertEqual(self.repo.status()['pages_today']['boss'], 1)
        self.assertTrue(result['platforms']['boss']['limited'])
        settings.platforms['boss'].daily_page_limit = 3
        self.run_collect([source], settings)
        self.assertEqual([q.page for q in source.calls], [1, 2])
        self.assertEqual(self.repo.status()['pages_today']['boss'], 3)

    def test_failure_consumes_full_reservation(self):
        source = Source(fail=True)
        source.search_page_cost = lambda q: 2
        self.run_collect([source])
        self.assertEqual(self.repo.status()['pages_today']['boss'], 2)

class CollectionToolsTest(unittest.TestCase):
    def test_own_routes_and_confirmation(self):
        import job_agent.server as server
        with patch('job_agent.server.load_config', return_value=Settings(engine={'backend': 'own'})), \
                patch('job_agent.service.collection.check_collection', return_value={'ok': True}) as check, \
                patch('job_agent.service.collection.start_collection', return_value={'started': True}) as start:
            self.assertTrue(server.check_collection(['boss'])['ok'])
            check.assert_called_once_with(['boss'])
            for value in [False, 1, 'true', None]:
                with self.assertRaises(ValueError):
                    server.start_collection(['boss'], confirm=value)
            start.assert_not_called()
            self.assertTrue(server.start_collection(['boss'], confirm=True)['started'])
            start.assert_called_once_with(['boss'])
            with self.assertRaises(ValueError):
                server.check_collection(['boss', 'boss'])

    def test_check_without_browser(self):
        from job_agent.service.collection import check_collection
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {'JOB_AGENT_HOME': home}), \
                patch('job_agent.browser.ego.EgoBrowser', side_effect=AssertionError('browser')):
            Path(home, 'config.yaml').write_text('collect:\n  platforms:\n    boss:\n      enabled: true\n')
            self.assertEqual(load_config().collect.task_space_name, 'job-agent 采集')
            self.assertEqual(check_collection(['boss'])['platforms'], ['boss'])
            self.assertFalse(check_collection(['liepin'])['ok'])
