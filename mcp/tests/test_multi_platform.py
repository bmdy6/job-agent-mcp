"""Multi-platform approvals use synthetic jobs and a fake workbench only."""
import unittest
import test_issue6 as fixtures
job = fixtures.job
from unittest.mock import patch
import copy
import delivery


class MultiPlatformTest(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.Issue6Test.setUp
    api = fixtures.Issue6Test.api
    call = fixtures.Issue6Test.call
    posts = fixtures.Issue6Test.posts
    def capabilities(self, enabled=True, liepin_action='apply_resume'):
        return {p: {'enabled': enabled, 'action_id': liepin_action if p == 'liepin' else 'apply_resume',
                    'action_label': '发招呼语' if p == 'liepin' and liepin_action == 'greeting' else '投递简历'}
                for p in ('liepin', 'zhilian', '51job')}

    def with_capabilities(self, capabilities):
        original = self.api
        def api(path, body=None):
            result = original(path, body)
            if path == '/api/workbench':
                result['delivery_capabilities'] = copy.deepcopy(capabilities)
            return result
        return patch('server.api', side_effect=api)

    async def test_preview_all_platforms_and_send_exact_actions(self):
        self.jobs = [job('fake:'+p, source_platform=p, greeting='' if p != 'boss' else '你好')
                     for p in ('boss', 'liepin', 'zhilian', '51job')]
        with self.with_capabilities(self.capabilities()):
            preview = await self.call('preview_greetings')
            self.assertEqual({j['source_platform'] for j in preview['items']}, {'boss', 'liepin', 'zhilian', '51job'})
            for item in preview['items']:
                self.assertEqual(item['action_id'], 'greeting' if item['source_platform'] == 'boss' else 'apply_resume')
                self.assertEqual(item['action_label'], '发招呼语' if item['source_platform'] == 'boss' else '投递简历')
            self.assertEqual(self.posts(), [])
            await self.call('send_greetings', confirm_token=preview['confirm_token'])
        self.assertEqual(self.posts()[0]['expected_platforms'], {j['id']: j['source_platform'] for j in preview['items']})
        self.assertEqual(self.posts()[0]['expected_actions'], {j['id']: j['action_id'] for j in preview['items']})

    async def test_disabled_or_old_engine_can_preview_but_cannot_consume_or_send(self):
        self.jobs = [job(source_platform='51job', greeting='')]
        preview = await self.call('preview_greetings')
        self.assertFalse(preview['items'][0]['delivery_enabled'])
        with self.assertRaises(Exception):
            await self.call('send_greetings', confirm_token=preview['confirm_token'])
        self.assertEqual(self.posts(), [])
        # Enabling requires a fresh preview, never upgrades a previously shown batch.
        with self.with_capabilities(self.capabilities()):
            with self.assertRaises(Exception):
                await self.call('send_greetings', confirm_token=preview['confirm_token'])

    async def test_changed_platform_action_or_enablement_invalidates_approval(self):
        self.jobs = [job(source_platform='liepin')]
        caps = self.capabilities()
        with self.with_capabilities(caps):
            preview = await self.call('preview_greetings')
            caps['liepin']['action_id'] = 'greeting'
            caps['liepin']['action_label'] = '发招呼语'
            with self.assertRaises(Exception):
                await self.call('send_greetings', confirm_token=preview['confirm_token'])
            caps['liepin']['action_id'] = 'apply_resume'
            caps['liepin']['enabled'] = False
            with self.assertRaises(Exception):
                await self.call('send_greetings', confirm_token=preview['confirm_token'])
        self.assertEqual(self.posts(), [])

    async def test_funnel_platform_counts_include_each_platform_once(self):
        self.jobs = [job('fake:'+p, source_platform=p, status='replied') for p in ('boss', 'liepin', 'zhilian', '51job')]
        result = await self.call('funnel_stats')
        self.assertEqual({g['key']: (g['applied'],g['replied']) for g in result['groups']},
                         {p:(1,1) for p in ('boss','liepin','zhilian','51job')})

    async def test_same_action_platform_change_invalidates_token(self):
        self.jobs = [job(source_platform='zhilian', greeting='')]
        with self.with_capabilities(self.capabilities()):
            preview = await self.call('preview_greetings')
            self.jobs[0]['source_platform'] = '51job'
            with self.assertRaises(Exception):
                await self.call('send_greetings', confirm_token=preview['confirm_token'])
        self.assertEqual(self.posts(), [])

    async def test_boss_advertised_disabled_is_respected_and_modern_boss_is_bound(self):
        self.jobs = [job()]
        caps = {'boss': {'enabled': False, 'action_id': 'greeting'}}
        with self.with_capabilities(caps):
            preview = await self.call('preview_greetings')
            self.assertFalse(preview['items'][0]['delivery_enabled'])
            with self.assertRaises(Exception):
                await self.call('send_greetings', confirm_token=preview['confirm_token'])
            caps['boss']['enabled'] = True
            preview = await self.call('preview_greetings')
            await self.call('send_greetings', confirm_token=preview['confirm_token'])
        self.assertEqual(self.posts(), [{'job_ids': ['fake:1'], 'direct_send': True,
                                         'expected_platforms': {'fake:1': 'boss'},
                                         'expected_actions': {'fake:1': 'greeting'}}])
