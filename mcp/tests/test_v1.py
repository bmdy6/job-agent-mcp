import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, unquote, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server
import user_data as data


def job(id='fake:1', **values):
    return {'id': id, 'title': '合成职位', 'company': 'ACME（杭州）科技有限公司',
            'source_platform': 'boss', 'url': 'https://example.invalid/'+id,
            'status': 'ready', 'jd': '任职要求：熟悉 SQL', 'salary': '10-20K', **values}


class V1Test(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ, {'JOB_AGENT_HOME': self.temp.name}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.calls = []
        self.jobs = [job(), job('fake:2', status='sent'), job('fake:3', status='replied')]
        self.preferences = {'cities': ['合成城市'], 'keywords': ['合成关键词'], 'salary': {'min': 7},
                            'blocked_companies': ['Other科技'], 'deal_breakers': ['灰产'],
                            'jd_deal_breakers': ['夜班'], 'score_threshold': 60}
        self.api_patch = patch.object(server, 'api', side_effect=self.api)
        self.api_patch.start()
        self.addCleanup(self.api_patch.stop)

    def api(self, path, body=None):
        self.calls.append((path, body))
        if path == '/api/agent/state':
            return {'preferences': self.preferences}
        if path.startswith('/api/jobs/search?'):
            offset = int(parse_qs(urlsplit(path).query)['offset'][0])
            return {'total': len(self.jobs), 'items': self.jobs[offset:offset+2]}
        if path.startswith('/api/jobs/') and not path.endswith('/greeting'):
            return next(j for j in self.jobs if j['id'] == unquote(path.split('/')[-1]))
        if path == '/api/config':
            return {'profile': {'resume_path': self.temp.name+'/resume.md'}}
        if path == '/api/agent/onboarding':
            return {'missing': []}
        if path.startswith('/api/agent/evaluations/pending?'):
            return {'jobs': [job()], 'resume': {'content': 'PRIVATE RESUME'}, 'extra': ['keep']}
        if path == '/api/agent/evaluations' or path.endswith('/greeting'):
            return {'success': True, 'extra': {'unchanged': [1, 2]}}
        raise AssertionError(path)

    async def call(self, name, **args):
        return (await server.mcp.call_tool(name, args))[1]

    async def test_profile_local_context_and_audit(self):
        Path(self.temp.name, 'config.yaml').write_text('mode: practice\npractice: {max_interviews_per_week: 2}\nstretch_salary_min: 25\n')
        result = await self.call('profile')
        self.assertEqual(result['preferences'], self.preferences)
        self.assertEqual(result['mode'], 'practice')
        self.assertEqual(result['practice']['max_interviews_per_week'], 2)
        self.assertEqual(result['stretch_salary_min'], 25)
        first = await self.call('experience_add', title='PRIVATE title', md='PRIVATE markdown')
        self.assertEqual((await self.call('experience_get', id=first['id']))['item']['md'], 'PRIVATE markdown')
        self.assertEqual(len((await self.call('experience_list'))['items']), 1)
        await self.call('company_save', company='ACME', md='PRIVATE company', source_url='https://example.invalid')
        self.assertEqual((await self.call('company_get', company='ACME有限公司'))['item']['md'], 'PRIVATE company')
        self.assertNotIn('PRIVATE', Path(self.temp.name, 'activity.jsonl').read_text())
        self.assertEqual(len(Path(self.temp.name, 'activity.jsonl').read_text().splitlines()), 6)

    async def test_log_history_and_funnel_deduplicate_jobs(self):
        for event in ['greeted', 'greeted', 'replied', 'interview_scheduled', 'interviewed', 'interviewed', 'offer']:
            row = await self.call('log_event', job_id='fake:1', event=event,
                                  resume_version='v2', variant='tailored', chat_url='https://example.invalid/chat')
            self.assertEqual(row['company'], self.jobs[0]['company'])
            self.assertEqual(row['platform'], 'boss')
            self.assertEqual(row['url'], self.jobs[0]['url'])
            self.assertTrue(row['time'])
        history = await self.call('company_history', company='ＡＣＭＥ股份')
        self.assertEqual(len(history['events']), 7)
        self.assertEqual(len(history['jobs']), 3)
        self.assertEqual(history['jobs'][0]['chat_url'], 'https://example.invalid/chat')
        self.assertTrue(all(j['url'] for j in history['jobs']))
        result = await self.call('funnel')
        self.assertEqual(result['total']['greeted'], 3)
        self.assertEqual(result['total']['replied'], 2)
        self.assertEqual(result['total']['reply_rate'], 2/3)
        self.assertEqual(result['total']['interview_rate'], 1/3)
        self.assertEqual(result['total']['interviewed'], 1)
        self.assertEqual(result['total']['offers'], 1)
        grouped = await self.call('funnel', group_by='variant')
        self.assertEqual({g['key']: g['greeted'] for g in grouped['groups']}, {'tailored': 1, 'unknown': 2})
        for group_by in ['resume_version', 'platform']:
            result = await self.call('funnel', group_by=group_by)
            self.assertEqual(sum(g['greeted'] for g in result['groups']), 3)
        with self.assertRaises(Exception):
            await self.call('log_event', job_id='fake:1', event='invalid')
        with self.assertRaises(Exception):
            await self.call('funnel', group_by='invalid')
        self.assertEqual(len(data.read_events()), 7)

    async def test_forward_evaluations_and_strict_greeting_gate(self):
        pending = await self.call('pending_evaluations', limit=4)
        self.assertEqual(pending['extra'], ['keep'])
        self.assertEqual(self.calls[-1], ('/api/agent/evaluations/pending?limit=4&include_resume=true', None))
        evaluations = [{'job_id': 'fake:1', 'score': {'custom': [1, {'raw': True}]}, 'greeting': 'PRIVATE'}]
        self.assertEqual((await self.call('submit_evaluations', evaluations=evaluations))['extra'], {'unchanged': [1, 2]})
        self.assertEqual(self.calls[-1], ('/api/agent/evaluations', {'evaluations': evaluations}))
        count = len(self.calls)
        for confirm in [False, 'true', 1, None]:
            with self.assertRaises(Exception):
                await self.call('set_greeting', job_id='fake:1', greeting='PRIVATE', confirm=confirm)
        self.assertEqual(len(self.calls), count)
        await self.call('set_greeting', job_id='fake:1', greeting='PRIVATE', confirm=True)
        self.assertEqual(self.calls[-1], ('/api/jobs/fake%3A1/greeting', {'greeting': 'PRIVATE', 'confirmed': True}))
        with patch.object(server, 'api', side_effect=ValueError('upstream rejected PRIVATE')):
            with self.assertRaises(Exception):
                await self.call('submit_evaluations', evaluations=evaluations)
        raw = Path(self.temp.name, 'activity.jsonl').read_text()
        self.assertNotIn('PRIVATE', raw)
        self.assertEqual(len(raw.splitlines()), 8)

    async def test_briefing_overview_and_job_context(self):
        data.experience_add('素材', 'SQL 的真实经历')
        data.company_save('ACME', '公司材料', 'https://example.invalid/company')
        data.append_event({'job_id': 'fake:2', 'event': 'greeted', 'company': 'ACME',
                           'url': self.jobs[1]['url'], 'chat_url': 'https://example.invalid/chat'})
        overview = await self.call('briefing')
        self.assertEqual(overview['mode'], 'job')
        self.assertEqual({key: value['count'] for key, value in overview['todos'].items()},
                         {'pending_confirmation': 1, 'sent_no_reply': 1, 'new_replies': 1, 'company_briefs': 1})
        self.assertTrue(all(item['url'] for group in overview['todos'].values() for item in group['items']))
        detail = await self.call('briefing', job_id='fake:1')
        self.assertEqual(detail['job']['jd'], self.jobs[0]['jd'])
        self.assertEqual(len(detail['experiences']), 1)
        self.assertEqual(detail['company']['md'], '公司材料')
        self.assertEqual(detail['company_history']['jobs'][1]['chat_url'], 'https://example.invalid/chat')
        self.assertEqual(detail['exclusions']['matches'], [])
        self.jobs[0].update(company='Other科技有限公司', title='灰产岗位', jd='夜班', salary='3-5K')
        detail = await self.call('briefing', job_id='fake:1')
        self.assertEqual({v['field'] for v in detail['exclusions']['matches']},
                         {'blocked_companies', 'deal_breakers', 'jd_deal_breakers', 'salary_min'})
        self.assertTrue(any('offset=2' in path for path, _ in self.calls))

    async def test_setup_lists_each_missing_dependency_and_does_not_launch(self):
        import v1
        with patch.object(v1, 'check_cdp', return_value={'ok': False, 'reason': 'ConnectionRefusedError'}):
            result = await self.call('setup_check')
        self.assertEqual(set(result['missing']), {'config', 'resume', 'experience', 'browser_cdp'})
        Path(self.temp.name, 'config.yaml').write_text('mode: job\n')
        Path(self.temp.name, 'resume.md').write_text('synthetic')
        data.experience_add('测试', 'synthetic')
        with patch.object(v1, 'check_cdp', return_value={'ok': True}):
            self.assertEqual((await self.call('setup_check'))['missing'], [])
        Path(self.temp.name, 'config.yaml').write_text('mode: invalid\n')
        with patch.object(v1, 'check_cdp') as cdp:
            result = await self.call('setup_check')
        self.assertFalse(result['checks']['config']['ok'])
        self.assertIn('bosshunter', result['missing'])
        cdp.assert_not_called()

    async def test_import_markdown_no_storage_and_reject_unsupported(self):
        path = Path(self.temp.name, 'source.md')
        path.write_text('# synthetic\n原文', encoding='utf-8')
        self.assertEqual((await self.call('resume_import', path=str(path)))['text'], '# synthetic\n原文')
        with self.assertRaises(Exception):
            await self.call('resume_import', path=str(path.with_suffix('.txt')))
        self.assertFalse(Path(self.temp.name, 'resumes').exists())
        self.assertEqual(self.calls, [])
