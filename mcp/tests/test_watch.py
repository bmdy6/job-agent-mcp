"""Synthetic upstream snapshots; never connects to the running workbench."""
import json
import os
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import Settings
import notify
import watch


def job(id='fake:1', **kwargs):
    return {'id': id, 'title': '合成岗位', 'company': '合成公司', 'salary': '10-20K',
            'source_platform': 'boss', 'status': 'replied', 'jd': '合成 JD',
            'url': 'https://www.zhipin.com/job_detail/fake.html', **kwargs}


class WatchTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ, {'JOB_AGENT_HOME': self.temp.name, 'TG_TEST': 'PRIVATE'}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.jobs = [job()]
        self.history = [{'id': 1, 'job_id': 'fake:1', 'action': 'hr_reply_detected',
                         'detail': 'HR回复: 请问有空吗', 'created_at': '2026-11-23 00:00:00'}]
        self.calls = []
        self.cfg = Settings.model_validate({'notify': {'enabled': True, 'channels': {
            'tg': {'type': 'telegram', 'enabled': True, 'bot_token_env': 'TG_TEST', 'chat_id': '123'}}}})

    def api(self, path):
        self.calls.append(path)
        if path == '/api/workbench':
            return {'task': None, 'last_task': None}
        query = parse_qs(urlsplit(path).query)
        if path.startswith('/api/jobs?'):
            offset, limit = int(query['offset'][0]), int(query['limit'][0])
            return self.jobs[offset:offset+limit]
        if path.startswith('/api/history?'):
            return self.history[:int(query['limit'][0])]
        raise AssertionError('unexpected or mutating endpoint: '+path)

    def tick(self):
        return watch.poll_once(self.api, self.cfg)

    def test_disabled_no_network_or_state(self):
        self.cfg.notify.enabled = False
        with patch.object(notify, 'post') as post:
            self.assertEqual(self.tick()['sent'], 0)
        post.assert_not_called()
        self.assertEqual(self.calls, [])
        self.assertFalse(Path(self.temp.name, 'notified.jsonl').exists())

    def test_once_across_restart_summary_links_and_private_receipt(self):
        with patch.object(notify, 'post', return_value={'ok': True}) as post:
            self.assertEqual(self.tick()['sent'], 1)
            self.assertEqual(self.tick()['sent'], 0)
            # A fresh call has no in-memory deduplication dependency.
            self.assertEqual(watch.poll_once(self.api, self.cfg)['sent'], 0)
        self.assertEqual(post.call_count, 1)
        text = post.call_args.args[1]['text']
        for part in ['合成公司', '合成岗位', '10-20K', '请问有空吗', 'https://www.zhipin.com/web/geek/chat', self.jobs[0]['url']]:
            self.assertIn(part, text)
        state = Path(self.temp.name, 'notified.jsonl')
        self.assertEqual(state.stat().st_mode & 0o777, 0o600)
        self.assertNotIn('合成', state.read_text())
        self.assertNotIn('PRIVATE', state.read_text())

    def test_new_reply_same_job_sends_but_status_and_jd_changes_do_not(self):
        with patch.object(notify, 'post', return_value={'ok': True}) as post:
            self.tick()
            self.jobs[0].update(status='needs_resume', jd='changed', updated_at='tomorrow')
            self.assertEqual(self.tick()['sent'], 0)
            self.history.append({**self.history[0], 'id': 2, 'detail': '第二条回复'})
            self.assertEqual(self.tick()['sent'], 1)
            self.assertEqual(self.tick()['sent'], 0)
        self.assertEqual(post.call_count, 2)

    def test_status_fallback_once_and_history_disappearance_does_not_duplicate(self):
        with patch.object(notify, 'post', return_value={'ok': True}) as post:
            self.tick()
            self.history = []
            self.assertEqual(self.tick()['sent'], 0)
            self.jobs.append(job('fake:2', status='needs_resume'))
            self.assertEqual(self.tick()['sent'], 1)
            self.jobs[1]['status'] = 'replied'
            self.assertEqual(self.tick()['sent'], 0)
        self.assertEqual(post.call_count, 2)

    def test_unknown_platform_even_if_history_says_replied(self):
        self.jobs = [job(source_platform='unknown'), job('fake:2', status='sent')]
        with patch.object(notify, 'post') as post:
            self.assertEqual(self.tick()['sent'], 0)
        post.assert_not_called()

    def test_history_only_and_multiple_events_for_same_reply_not_duplicated(self):
        self.jobs[0]['status'] = 'resume_sent'
        with patch.object(notify, 'post', return_value={'ok': True}) as post:
            self.assertEqual(self.tick()['sent'], 1)
            self.assertEqual(self.tick()['sent'], 0)
        self.assertEqual(post.call_count, 1)

    def test_history_expands_and_jobs_paginate(self):
        self.jobs = [job(str(i), status='sent') for i in range(101)]
        self.history = [{'id': i, 'job_id': '0', 'action': 'other'} for i in range(100)]
        self.history.append({'id': 100, 'job_id': '100', 'action': 'hr_reply_detected', 'detail': '旧回复'})
        with patch.object(notify, 'post', return_value={'ok': True}):
            self.assertEqual(self.tick()['sent'], 1)
        self.assertTrue(any('offset=100' in p for p in self.calls))
        self.assertTrue(any('limit=200' in p for p in self.calls))

    def test_failed_channel_retried_without_resending_successful_channel(self):
        self.cfg.notify.channels['second'] = self.cfg.notify.channels['tg'].model_copy()
        with patch.object(notify.time, 'sleep'), patch.object(notify, 'post', side_effect=[
                {'ok': True}, *[{'ok': False, 'error_code': 403}]*3, {'ok': True}]) as post:
            self.assertEqual(self.tick()['sent'], 1)
            self.assertEqual(self.tick()['sent'], 1)
            self.assertEqual(self.tick()['sent'], 0)
        self.assertEqual(post.call_count, 5)

    def test_concurrent_pollers_only_send_once(self):
        with patch.object(notify, 'post', return_value={'ok': True}) as post:
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _: self.tick(), range(2)))
        self.assertEqual(sum(r['sent'] for r in results), 1)
        self.assertEqual(post.call_count, 1)

    def test_corrupt_receipt_fail_closed_before_send(self):
        Path(self.temp.name, 'notified.jsonl').write_text('broken JSON\n')
        with patch.object(notify, 'post') as post, self.assertRaises(ValueError):
            self.tick()
        post.assert_not_called()

    def test_draft_disabled_and_failure_still_sends_notification(self):
        with patch.object(watch, 'draft_reply') as draft, patch.object(notify, 'post', return_value={'ok': True}):
            self.tick()
        draft.assert_not_called()
        self.history.append({**self.history[0], 'id': 2})
        self.cfg.notify.draft_reply = True
        with patch.object(watch, 'draft_reply', side_effect=ValueError('PRIVATE')) as draft, \
                patch.object(notify, 'post', return_value={'ok': True}) as post:
            self.assertEqual(self.tick()['sent'], 1)
        self.assertNotIn('PRIVATE', post.call_args.args[1]['text'])

    def test_draft_uses_jd_resume_and_configured_model_user_only(self):
        resume = Path(self.temp.name, 'main.md')
        resume.write_text('合成主简历')
        self.cfg.notify.ai.resume_path = str(resume)
        self.cfg.notify.ai.model = 'test-model'
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'PRIVATE-KEY'}), patch.object(notify, 'post',
                return_value={'choices': [{'message': {'content': '您好，可以聊聊。'}}]}) as post:
            result = watch.draft_reply(self.jobs[0], 'HR摘要', self.cfg.notify.ai)
        self.assertEqual(result, '您好，可以聊聊。')
        url, body, headers = post.call_args.args
        self.assertEqual(url, 'https://api.openai.com/v1/chat/completions')
        self.assertEqual(body['model'], 'test-model')
        self.assertIn('合成 JD', str(body['messages']))
        self.assertIn('合成主简历', str(body['messages']))
        self.assertEqual(headers['Authorization'], 'Bearer PRIVATE-KEY')
        with patch.object(notify, 'post', return_value={'choices': []}), patch.dict(os.environ, {'OPENAI_API_KEY': 'x'}):
            with self.assertRaises(ValueError):
                watch.draft_reply(self.jobs[0], '', self.cfg.notify.ai)

    def test_poll_errors_are_isolated_per_job_and_no_enabled_channels_no_reads(self):
        self.cfg.notify.channels['tg'].enabled = False
        self.assertEqual(self.tick()['sent'], 0)
        self.assertEqual(self.calls, [])

    def test_notification_url_is_a_chat_url_and_job_link_survives_long_draft(self):
        title, text, url = watch.notification(job(chat_url='https://www.zhipin.com/web/geek/chat?uid=fake'), '摘要', '草稿'*5000)
        self.assertEqual(url, 'https://www.zhipin.com/web/geek/chat?uid=fake')
        cfg = self.cfg.notify.channels['tg']
        with patch.object(notify, 'post', return_value={'ok': True}) as post:
            notify.send_one(cfg, title, text, url)
        self.assertIn(self.jobs[0]['url'], post.call_args.args[1]['text'])
        self.assertIn(url, post.call_args.args[1]['text'])

    def test_status_first_then_same_reply_gets_history_not_sent_twice(self):
        original = self.history[0]
        self.history = []
        with patch.object(notify, 'post', return_value={'ok': True}) as post:
            self.assertEqual(self.tick()['sent'], 1)
            self.history = [original]
            self.assertEqual(self.tick()['sent'], 0)
            self.history.append({**original, 'id': 2, 'detail': '真正的第二条'})
            self.assertEqual(self.tick()['sent'], 1)
        self.assertEqual(post.call_count, 2)


class MultiPlatformWatchTest(unittest.TestCase):
    setUp = WatchTest.setUp
    api = WatchTest.api
    tick = WatchTest.tick
    def test_four_platform_history_events_notify_once_with_site_links(self):
        platforms = ('boss', 'liepin', 'zhilian', '51job')
        self.jobs = [job('fake:'+p, source_platform=p, url='https://example.invalid/'+p) for p in platforms]
        self.history = [{'id': i, 'job_id': 'fake:'+p, 'action': 'hr_reply_detected',
                         'detail': '平台反馈: 企业对你有意', 'created_at': '2026-11-23 00:00:00'}
                        for i,p in enumerate(platforms)]
        with patch.object(notify, 'post', return_value={'ok': True}) as post:
            self.assertEqual(self.tick()['sent'], 4)
            self.assertEqual(self.tick()['sent'], 0)
        texts = [c.args[1]['text'] for c in post.call_args_list]
        for p in platforms:
            self.assertTrue(any('https://example.invalid/'+p in text for text in texts))
        for p in ('liepin','zhilian','51job'):
            text = next(text for text in texts if 'https://example.invalid/'+p in text)
            self.assertNotIn('BOSS 聊天', text)
            self.assertNotIn('https://www.zhipin.com/web/geek/chat', text)

    def test_external_status_alone_is_not_new_reply_evidence(self):
        self.jobs = [job(source_platform='51job')]
        self.history = []
        with patch.object(notify, 'post') as post:
            self.assertEqual(self.tick()['sent'], 0)
        post.assert_not_called()
