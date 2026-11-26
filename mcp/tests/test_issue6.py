"""Issue 6 acceptance, using synthetic jobs and a fake BossHunter transport only."""
import copy
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, unquote, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server
import user_data as data


def job(id='fake:1', **extra):
    return {'id': id, 'title': '合成岗位', 'company': id+'科技有限公司', 'salary': '10-20K',
            'source_platform': 'boss', 'city': '杭州', 'score': 90, 'status': 'approved',
            'greeting': '你好，有相关项目经验。', 'greeting_selection': 'original',
            'url': 'https://example.invalid/'+id, 'jd': '公司介绍：我们提供企业文档检索产品。', **extra}


class Issue6Test(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ, {'JOB_AGENT_HOME': self.temp.name}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.jobs = [job(), job('fake:2', status='ready', score=80)]
        self.requests = []
        self.task = None
        self.last_task = None
        self.deliver_error = None
        self.transport = patch.object(server, 'api', side_effect=self.api)
        self.transport.start()
        self.addCleanup(self.transport.stop)

    def api(self, path, body=None):
        self.requests.append((path, copy.deepcopy(body)))
        if path == '/api/stats':
            return {s: sum(j['status'] == s for j in self.jobs) for s in {j['status'] for j in self.jobs}}
        if path.startswith('/api/activity?'):
            return [{'day': '2026-11-23', 'action': 'greeting_edited', 'cnt': 116}]
        if path == '/api/workbench':
            sent = {'sent', 'replied', 'resume_sent', 'needs_resume', 'follow_up_sent'}
            return {'funnel': {'采集总数': len(self.jobs), '初筛通过': len(self.jobs), 'AI评分': len(self.jobs),
                              '人工确认': sum(j['status'] in sent | {'approved'} for j in self.jobs),
                              '发送': sum(j['status'] in sent for j in self.jobs),
                              '回复': sum(j['status'] in {'replied', 'resume_sent', 'needs_resume'} for j in self.jobs),
                              '拒绝': sum(j['status'] == 'rejected' for j in self.jobs)},
                    'task': self.task, 'last_task': self.last_task, 'send_quota': {'remaining': 30}}
        if path == '/api/agent/state':
            return {'preferences': {}}
        if path.startswith('/api/jobs?'):
            offset = int(parse_qs(urlsplit(path).query)['offset'][0])
            return copy.deepcopy(self.jobs[offset:offset+100])
        if path.startswith('/api/jobs/search?'):
            offset = int(parse_qs(urlsplit(path).query)['offset'][0])
            return {'items': copy.deepcopy(self.jobs[offset:offset+2]), 'total': len(self.jobs)}
        if path.startswith('/api/jobs/'):
            return copy.deepcopy(next(j for j in self.jobs if j['id'] == unquote(path.rsplit('/', 1)[1])))
        if path == '/api/workbench/deliver':
            if self.deliver_error:
                raise self.deliver_error
            return {'id': 'delivery-1', 'mode': 'deliver', 'status': 'running'}
        if path == '/api/agent/tasks':
            return {'task': {'id': 'monitor-1', 'mode': 'monitor', 'status': 'running'}}
        if path.endswith('/stop'):
            return {'status': 'stopping'}
        raise AssertionError(path)

    async def call(self, name, **args):
        return (await server.mcp.call_tool(name, args))[1]

    def posts(self, path='/api/workbench/deliver'):
        return [b for p, b in self.requests if p == path and b is not None]

    async def test_funnel_merges_manual_and_interview_events_once_and_groups(self):
        self.jobs[0]['status'] = 'replied'
        self.jobs[1].update(source_platform='liepin', city='南京', score=65)
        for event in ['manual_applied', 'manual_applied', 'interview_scheduled', 'interview_done', 'offer']:
            row = await self.call('log_event', job_id='fake:2', event=event)
            self.assertEqual((row['city'], row['score']), ('南京', 65))
        await self.call('log_event', job_id='fake:1', event='replied')
        result = await self.call('funnel_stats')
        self.assertEqual(result['total']['applied'], 2)
        self.assertEqual(result['total']['replied'], 2)
        self.assertEqual(result['total']['reply_rate'], 1)
        self.assertEqual(result['total']['interview_scheduled'], 1)
        self.assertEqual(result['total']['interview_done'], 1)
        self.assertEqual(result['total']['offers'], 1)
        self.assertEqual({g['key']: g['applied'] for g in result['groups']}, {'boss': 1, 'liepin': 1})
        self.assertEqual(result['bosshunter']['funnel']['发送'], 1)
        self.assertEqual(result['activity'][0]['cnt'], 116)
        for group_by, keys in [('city', {'杭州', '南京'}), ('score_band', {'80-100', '60-79'})]:
            grouped = await self.call('funnel_stats', group_by=group_by)
            self.assertEqual({g['key'] for g in grouped['groups']}, keys)
            self.assertEqual(sum(g['applied'] for g in grouped['groups']), 2)
        self.assertTrue(result['table'])

    async def test_stats_pages_full_job_list_and_rejects_repeated_pages(self):
        self.jobs = [job('fake:'+str(i), status='sent') for i in range(102)]
        result = await self.call('funnel_stats')
        self.assertEqual(result['total']['applied'], 102)
        self.assertEqual(sum(g['applied'] for g in result['groups']), 102)
        self.assertTrue(any('offset=100' in p for p, _ in self.requests))
        original_api = self.api
        def repeated(path, body=None):
            if path.startswith('/api/jobs?'):
                return self.jobs[:100]
            return original_api(path, body)
        with patch.object(server, 'api', side_effect=repeated), self.assertRaises(Exception):
            await self.call('funnel_stats')

    async def test_empty_stop_returns_no_effect(self):
        for name in ['stop_delivery', 'stop_reply_monitor']:
            self.assertFalse((await self.call(name))['stopped'])
        self.assertFalse(any(b is not None for p, b in self.requests))

    async def test_stats_include_blocked_company_jobs_hidden_from_search(self):
        self.jobs[0]['status'] = 'replied'
        original_api = self.api
        def hidden(path, body=None):
            if path.startswith('/api/jobs/search?'):
                return {'items': [], 'total': 0}
            return original_api(path, body)
        with patch.object(server, 'api', side_effect=hidden):
            result = await self.call('funnel_stats')
        self.assertEqual(result['total']['applied'], 1)
        self.assertEqual(sum(g['applied'] for g in result['groups']), 1)
        self.assertEqual(result['warnings'], [])

    async def test_aggregate_only_uses_three_allowed_gets_when_no_events(self):
        self.jobs[0]['status'] = 'sent'
        result = await self.call('funnel_stats', group_by=None, days=14)
        self.assertEqual(result['total']['applied'], 1)
        self.assertEqual(result['total']['reply_rate'], 0)
        self.assertEqual({p for p, b in self.requests}, {'/api/stats', '/api/activity?days=14', '/api/workbench'})
        self.assertTrue(all(b is None for p, b in self.requests))

    async def test_rejection_is_deduplicated_and_never_implies_application(self):
        self.jobs[0]['status'] = 'rejected'
        await self.call('log_event', job_id='fake:1', event='rejected')
        await self.call('log_event', job_id='fake:2', event='rejected')
        result = await self.call('funnel_stats')
        self.assertEqual(result['total']['rejected'], 2)
        self.assertEqual(result['total']['applied'], 0)
        self.assertIsNone(result['total']['reply_rate'])

    async def test_briefing_reminds_for_replied_and_scheduled_without_research(self):
        self.jobs[0]['status'] = 'replied'
        await self.call('log_event', job_id='fake:2', event='interview_scheduled')
        result = await self.call('briefing')
        reminders = result['todos']['company_briefs']
        self.assertEqual(reminders['count'], 2)
        self.assertTrue(all('公司简报' in item['message'] and item['url'] for item in reminders['items']))
        self.assertFalse(Path(self.temp.name, 'companies').exists())

    async def test_preview_is_readonly_and_success_consumes_token_once(self):
        preview = await self.call('preview_greetings')
        self.assertEqual(len(preview['items']), 2)
        self.assertTrue(all({'id', 'company', 'title', 'salary', 'greeting', 'url'} <= set(j) for j in preview['items']))
        self.assertFalse(any(b is not None for p, b in self.requests))
        self.assertGreater(preview['expires_at'], time.time())
        token = preview['confirm_token']
        await self.call('send_greetings', confirm_token=token, exclude_ids=['fake:2'])
        with self.assertRaises(Exception):
            await self.call('send_greetings', confirm_token=token)
        self.assertEqual(self.posts(), [{'job_ids': ['fake:1'], 'direct_send': True}])
        audit = Path(self.temp.name, 'activity.jsonl').read_text()
        self.assertNotIn(token, audit)
        self.assertNotIn('你好', audit)

    async def test_missing_invalid_and_expired_tokens_never_deliver(self):
        for args in [{}, {'confirm_token': ''}, {'confirm_token': '../bad'}, {'confirm_token': 'x'*64}]:
            with self.subTest(args=args), self.assertRaises(Exception):
                await self.call('send_greetings', **args)
        preview = await self.call('preview_greetings')
        with patch('time.time', return_value=preview['expires_at']+1), self.assertRaises(Exception):
            await self.call('send_greetings', confirm_token=preview['confirm_token'])
        self.assertEqual(self.posts(), [])

    async def test_changed_content_identity_status_and_excluded_item_all_reject(self):
        for field, changed in [('greeting', '文案变了'), ('id', 'fake:other'), ('status', 'sent'),
                               ('company', '另一家公司'), ('greeting_selection', 'pending')]:
            original = copy.deepcopy(self.jobs)
            preview = await self.call('preview_greetings')
            self.jobs[1][field] = changed
            with self.subTest(field=field), self.assertRaises(Exception):
                await self.call('send_greetings', confirm_token=preview['confirm_token'], exclude_ids=['fake:2'])
            self.jobs = original
        self.assertEqual(self.posts(), [])

    async def test_unknown_exclusions_and_empty_batch_reject(self):
        preview = await self.call('preview_greetings')
        for excludes in [['fake:unknown'], ['fake:1', 'fake:2']]:
            with self.assertRaises(Exception):
                await self.call('send_greetings', confirm_token=preview['confirm_token'], exclude_ids=excludes)
        self.assertEqual(self.posts(), [])

    async def test_timeout_consumes_token_and_endpoint_change_rejects(self):
        preview = await self.call('preview_greetings', job_ids=['fake:1'])
        self.deliver_error = RuntimeError('unknown delivery outcome')
        with self.assertRaises(Exception):
            await self.call('send_greetings', confirm_token=preview['confirm_token'])
        with self.assertRaises(Exception):
            await self.call('send_greetings', confirm_token=preview['confirm_token'])
        self.assertEqual(len(self.posts()), 1)
        preview = await self.call('preview_greetings')
        Path(self.temp.name, 'config.yaml').write_text('bosshunter: {port: 9999}')
        with self.assertRaises(Exception):
            await self.call('send_greetings', confirm_token=preview['confirm_token'])
        self.assertEqual(len(self.posts()), 1)

    async def test_greeting_targets_deduplicate_companies_and_expose_jd_evidence(self):
        self.jobs = [job('fake:'+str(i), company='公司'+str(i), score=100-i) for i in range(1, 7)]
        self.jobs.append(job('fake:copy', company='公司1科技有限公司', score=80))
        Path(self.temp.name, 'config.yaml').write_text('greetings: {top_n: 5}')
        result = await self.call('greeting_targets')
        self.assertEqual(len(result['items']), 5)
        self.assertEqual([j['score'] for j in result['items']], [99, 98, 97, 96, 95])
        for item in result['items']:
            sentence = '看到贵司提供企业文档检索产品，我想进一步聊聊。'
            self.assertIn('我们提供企业文档检索产品', item['jd'])
            self.assertLessEqual(len(item['greeting']+sentence), 120)
        self.assertTrue(all(b is None for p, b in self.requests))

    async def test_delivery_status_and_stop_only_matching_active_task(self):
        self.task = {'id': 'delivery-1', 'mode': 'deliver', 'status': 'running',
                     'progress': {'current': 2}, 'metrics': {'sent': 1}, 'logs': ['合成失败原因']}
        result = await self.call('delivery_status')
        self.assertEqual(result['task']['progress']['current'], 2)
        self.assertEqual(result['task']['logs'], ['合成失败原因'])
        await self.call('stop_delivery')
        self.assertEqual(self.posts('/api/workbench/task/delivery-1/stop'), [{}])
        with patch('service.monitor.stop', return_value={'stopped': False}) as stop:
            self.assertFalse((await self.call('stop_reply_monitor'))['stopped'])
            stop.assert_called_once()
        self.task['mode'] = 'collect'
        with self.assertRaises(Exception):
            await self.call('stop_delivery')

    async def test_monitor_requires_explicit_confirmation_and_uses_launchd(self):
        for confirm in [False, 'true', 1]:
            with self.assertRaises(Exception):
                await self.call('start_reply_monitor', confirm=confirm)
        self.assertEqual(self.posts('/api/agent/tasks'), [])
        with patch('service.monitor.start', return_value={'running': True}) as start:
            await self.call('start_reply_monitor', confirm=True)
            start.assert_called_once()
        with patch('service.monitor.stop', return_value={'stopped': True}) as stop:
            await self.call('stop_reply_monitor')
            stop.assert_called_once()
        self.assertFalse(any(body is not None for _, body in self.requests))

    async def test_empty_preview_unsupported_platform_and_unreviewed_greeting_reject(self):
        self.jobs[0]['source_platform'] = 'unknown'
        self.jobs[1]['greeting_selection'] = 'pending'
        with self.assertRaises(Exception):
            await self.call('preview_greetings')
        with self.assertRaises(Exception):
            await self.call('preview_greetings', job_ids=['fake:1'])
        self.assertEqual(self.posts(), [])

    async def test_new_top_candidate_changes_implicit_batch_but_not_explicit_selection(self):
        implicit = await self.call('preview_greetings', limit=1)
        explicit = await self.call('preview_greetings', job_ids=['fake:1'])
        self.jobs.append(job('fake:new', score=100))
        with self.assertRaises(Exception):
            await self.call('send_greetings', confirm_token=implicit['confirm_token'])
        await self.call('send_greetings', confirm_token=explicit['confirm_token'])
        self.assertEqual(self.posts(), [{'job_ids': ['fake:1'], 'direct_send': True}])

    async def test_funnel_keeps_workbench_totals_and_warns_on_snapshot_drift(self):
        original_api = self.api
        def changed(path, body=None):
            result = original_api(path, body)
            if path == '/api/workbench':
                result['funnel']['发送'] = 1
            return result
        with patch.object(server, 'api', side_effect=changed):
            result = await self.call('funnel_stats')
        self.assertEqual(result['total']['applied'], 1)
        self.assertTrue(result['warnings'])

    async def test_local_event_metadata_survives_job_removal_and_old_event_names_work(self):
        await self.call('log_event', job_id='fake:1', event='manual_applied')
        await self.call('log_event', job_id='fake:1', event='interviewed')
        self.jobs = self.jobs[1:]
        result = await self.call('funnel_stats', group_by='city')
        self.assertEqual(result['total']['applied'], 1)
        self.assertEqual(result['total']['interview_done'], 1)
        self.assertEqual(next(g for g in result['groups'] if g['key']=='杭州')['applied'], 1)
        legacy = await self.call('funnel')
        self.assertEqual(legacy['total']['greeted'], 1)
        self.assertEqual(legacy['total']['interviewed'], 1)

    async def test_top_n_zero_disables_targets_and_missing_jd_is_not_fabricated(self):
        Path(self.temp.name, 'config.yaml').write_text('greetings: {top_n: 0}')
        self.assertEqual((await self.call('greeting_targets'))['items'], [])
        Path(self.temp.name, 'config.yaml').write_text('greetings: {top_n: 1}')
        self.jobs[0]['jd'] = None
        self.assertIsNone((await self.call('greeting_targets'))['items'][0]['jd'])

    async def test_config_conservative_defaults_and_reject_invalid(self):
        from config import load_config
        config = load_config()
        self.assertEqual(config.greetings.top_n, 20)
        self.assertEqual(config.delivery.confirm_ttl_minutes, 30)
        self.assertEqual(config.delivery.preview_limit, 5)
        for text in ['greetings: {top_n: -1}', 'delivery: {confirm_ttl_minutes: 0}',
                     'delivery: {preview_limit: true}', 'analytics: {score_bands: [80, 60]}']:
            Path(self.temp.name, 'config.yaml').write_text(text)
            with self.subTest(text=text), self.assertRaises(ValueError):
                load_config()
