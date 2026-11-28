import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import job_agent.server as server
from job_agent.config import Settings


def job(i, **values):
    return {'id': f'fake:{i}', 'title': '测试职位', 'company': '合成公司', 'city': '杭州',
            'source_platform': 'boss', 'url': 'https://example.invalid/job',
            'created_at': '2026-11-18 00:00:00', 'jd': '任职要求：\n熟悉 SQL。', **values}


class JDTest(unittest.TestCase):
    def setUp(self):
        config=patch.object(server,'load_config',return_value=Settings())
        config.start(); self.addCleanup(config.stop)

    def test_paging_details_filters_and_metadata(self):
        calls = []
        def search(**params):
            calls.append(params)
            return {'total': 3, 'items': [job(1), job(2)] if params['offset'] == 0 else [job(3)]}
        details = {'fake:1': job(1, jd='任职要求：\n熟悉 SQL；理解事务。\n福利待遇：午餐。'),
                   'fake:2': job(2, created_at='2026-11-17 23:59:59'), 'fake:3': job(3, jd='欢迎加入。')}
        with patch.object(server.own, 'search_jobs', side_effect=search), patch.object(server.own, 'get_job', side_effect=details.__getitem__), patch.object(server, 'load_config', return_value=Settings(cities=['杭州'], platforms=['boss'])):
            result = server.jd_requirements(since='2026-11-18', limit=2)
        self.assertEqual(result['jobs'], 2)
        self.assertEqual(result['requirements_jobs'], 1)
        self.assertEqual(result['full_jobs'], 1)
        self.assertEqual(result['section_success_rate'], 0.5)
        self.assertEqual([s['text'] for s in result['items']], ['熟悉 SQL', '理解事务', '欢迎加入'])
        self.assertEqual(result['items'][0]['job_id'], 'fake:1')
        for key in ['title', 'company', 'city', 'platform', 'url', 'section']:
            self.assertIn(key, result['items'][0])
        self.assertEqual(calls[0]['cities'], ['杭州'])
        self.assertEqual(calls[0]['platforms'], ['boss'])
        self.assertEqual([c['offset'] for c in calls], [0, 2])

    def test_direction_filter_and_empty_jd(self):
        jobs = [job(1, title='测试设计', jd='无标题的合成描述。'), job(2), job(3, title='设计岗位', jd='')]
        with patch.object(server.own, 'search_jobs', return_value={'total': 3, 'items': jobs}), patch.object(server.own, 'get_job', side_effect=lambda ident: jobs[int(ident[-1])-1]), patch.object(server, 'load_config', return_value=Settings(directions=['设计'])):
            result = server.jd_requirements(cities=[], platforms=[], limit=20)
        self.assertEqual(result['jobs'], 2)
        self.assertEqual(result['missing_jd_job_ids'], ['fake:3'])
        self.assertEqual(result['section_success_rate'], 0)

    def test_since_timezone_and_invalid_values(self):
        with patch.object(server.own, 'search_jobs') as api:
            for since in ['yesterday', '2026-99-01']:
                with self.assertRaises(ValueError):
                    server.jd_requirements(since=since)
            api.assert_not_called()
        self.assertEqual(server.parse_since('2026-11-18T08:00:00+08:00'), datetime(2026, 11, 18, tzinfo=timezone.utc))

    def test_empty_pool_and_http_failure(self):
        with patch.object(server.own, 'search_jobs', return_value={'total': 0, 'items': []}), patch.object(server, 'load_config', return_value=Settings()):
            result = server.jd_requirements()
        self.assertEqual(result['jobs'], 0)
        self.assertIsNone(result['section_success_rate'])
        with patch.object(server.own, 'search_jobs', side_effect=RuntimeError('unavailable')):
            with self.assertRaises(RuntimeError):
                server.jd_requirements()
