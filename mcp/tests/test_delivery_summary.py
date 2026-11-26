"""Delivery notification tests use synthetic tasks, history and fake Telegram."""
import json
import sys
import unittest
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import delivery_summary as summary
import notify
from config import NotifySettings
import test_watch


def task(**changes):
    return {'id': 'synthetic-task', 'mode': 'deliver', 'status': 'completed',
            'created_at': '2026-11-24T10:45:00+08:00', 'updated_at': '2026-11-24T11:45:00+08:00',
            'metrics': {'send_requested': 3, 'send_success': 1, 'send_failed': 2, 'send_deferred': 0}, **changes}


def history(jid, action, detail='', **changes):
    return {'id': jid, 'job_id': jid, 'action': action, 'detail': detail,
            'company': '合成公司'+jid, 'source_platform': 'boss', 'created_at': '2026-11-24 03:00:00', **changes}


class DeliverySummaryTest(unittest.TestCase):
    tick = test_watch.WatchTest.tick
    # Reuse fixture helpers, without inheriting the reply test suite.
    def setUp(self):
        self.addCleanup(time.tzset)
        test_watch.WatchTest.setUp(self)
        os.environ['TZ'] = 'Asia/Shanghai'
        time.tzset()
        self.jobs = [test_watch.job('ok', status='sent'), test_watch.job('bad', status='error'), test_watch.job('bad2', status='error')]
        self.history = [history('ok', 'sent'), history('bad', 'error', '发送失败: attachment_selection_unverified', source_platform='51job'),
                        history('bad2', 'error', '发送失败: application_control_unverified', source_platform='zhilian')]
        self.workbench = {'task': None, 'last_task': task()}

    def api(self, path):
        if path == '/api/workbench':
            self.calls.append(path)
            return self.workbench
        return test_watch.WatchTest.api(self, path)

    def test_terminal_task_sent_once_across_restart_and_changed_metrics(self):
        with patch.object(notify, 'post', return_value={'ok': True}) as post:
            self.assertEqual(self.tick()['sent'], 1)
            self.workbench['last_task']['metrics']['send_success'] = 9
            self.assertEqual(self.tick()['sent'], 0)
        self.assertEqual(post.call_count, 1)
        text = post.call_args.args[1]['text']
        for value in ('投递完成（11-24 11:45）成功 1 / 失败 2（共 3）', '✅ BOSS 1：合成公司ok',
                      '❌ 51job 1：附件确认不了', '❌ 智联 1：没确认投上'):
            self.assertIn(value, text)
        state = Path(self.temp.name, 'notified.jsonl').read_text()
        self.assertIn('synthetic-task', state)
        self.assertNotIn('合成公司', state)

    def test_failed_task_with_no_metrics_still_notifies_without_invented_totals(self):
        self.workbench['last_task'] = task(status='failed', metrics={}, error='PRIVATE traceback')
        self.history = []
        with patch.object(notify, 'post', return_value={'ok': True}) as post:
            self.assertEqual(self.tick()['sent'], 1)
        text = post.call_args.args[1]['text']
        self.assertIn('投递任务失败', text)
        self.assertIn('总数未提供', text)
        self.assertNotIn('PRIVATE', text)

    def test_switches_and_non_terminal_or_other_mode_do_not_send(self):
        for changes in ({'status': 'running'}, {'status': 'stopped'}, {'mode': 'monitor'}):
            self.workbench['last_task'] = task(**changes)
            with patch.object(notify, 'post') as post:
                self.tick()
            post.assert_not_called()
        self.workbench['last_task'] = task()
        self.cfg.notify.delivery_summary = False
        with patch.object(notify, 'post') as post:
            self.tick()
        post.assert_not_called()
        self.assertTrue(NotifySettings().delivery_summary)

    def test_per_channel_failure_retries_without_resending_success(self):
        self.cfg.notify.channels['second'] = self.cfg.notify.channels['tg'].model_copy()
        with patch.object(notify, 'send_one', side_effect=[{'success': True}, {'success': False}, {'success': True}]) as send:
            self.assertEqual(self.tick()['sent'], 1)
            self.assertEqual(self.tick()['sent'], 1)
            self.assertEqual(self.tick()['sent'], 0)
        self.assertEqual(send.call_count, 3)

    def test_preseeded_manual_receipt_is_skipped(self):
        key = summary.event_key(self.cfg.bosshunter.port, 'synthetic-task')
        Path(self.temp.name, 'notified.jsonl').write_text(json.dumps({'event': key, 'job': key, 'channel': 'tg'})+'\n')
        with patch.object(notify, 'post') as post:
            self.assertEqual(self.tick()['sent'], 0)
        post.assert_not_called()

    def test_only_enabled_telegram_channels_receive_delivery(self):
        from config import WebhookSettings
        self.cfg.notify.channels = {'hook': WebhookSettings(enabled=True)}
        with patch.object(notify, 'post') as post:
            self.tick()
        post.assert_not_called()
        self.assertNotIn('/api/workbench', self.calls)

    def test_task_selection_considers_both_visible_slots(self):
        self.assertEqual(summary.task_from({'task': task(), 'last_task': task(mode='monitor')})['id'], 'synthetic-task')
        self.assertIsNone(summary.task_from({'last_task': task(status='running')}))
        self.assertIsNone(summary.task_from({}))

    def test_history_window_utc_dedup_latest_and_human_reasons(self):
        entries = [history('ok', 'sent'), history('old', 'sent', created_at='2026-11-24 02:44:59'),
                   history('future', 'sent', created_at='2026-11-24 03:45:01'),
                   history('unrelated', 'hr_reply_detected'), history('ok', 'sent'),
                   history('bad', 'error', '发送失败: confirmed_action_changed'),
                   history('gone', 'error', '岗位已关闭或下架'),
                   history('secret', 'error', 'PRIVATE provider error')]
        title, text = summary.notification(task(metrics={'send_requested': 4, 'send_success': 1, 'send_failed': 3}), entries, [])
        self.assertIn('BOSS 1：合成公司ok', text)
        for excluded in ('合成公司old', '合成公司future', 'PRIVATE', 'confirmed_action_changed'):
            self.assertNotIn(excluded, text)
        for included in ('动作变了', '岗位已下架', '其他原因'):
            self.assertIn(included, text)

    def test_deferred_and_missing_evidence_are_explicit_not_success(self):
        title, text = summary.notification(task(metrics={'send_requested': 7, 'send_success': 3,
            'send_failed': 1, 'send_deferred': 3}), [], [])
        self.assertIn('成功 3 / 失败 1（共 7）', title)
        self.assertIn('未执行 3', text)
        self.assertIn('成功明细缺少 3', text)
        self.assertIn('失败明细缺少 1', text)

    def test_long_company_list_is_bounded_and_keeps_failure_reasons(self):
        entries = [history(str(i), 'sent', company='很长的合成公司名称'*20) for i in range(200)]
        entries += [history('bad', 'error', '发送失败: attachment_selection_unverified', source_platform='51job')]
        title, text = summary.notification(task(metrics={'send_requested': 201, 'send_success': 200, 'send_failed': 1}), entries, [])
        self.assertLess(len((title+'\n\n'+text).encode('utf-16-le'))//2, 4096)
        self.assertIn('附件确认不了', text)

    def test_workbench_failure_keeps_existing_reply_path_working(self):
        original = self.api
        def broken(path):
            if path == '/api/workbench':
                raise ValueError('PRIVATE')
            return original(path)
        self.jobs = [test_watch.job()]
        self.history = [history('fake:1', 'hr_reply_detected')]
        with patch.object(notify, 'post', return_value={'ok': True}) as post:
            result = __import__('watch').poll_once(broken, self.cfg)
        self.assertEqual(result['sent'], 1)
        self.assertEqual(result['failed'], 1)
        self.assertEqual(post.call_count, 1)

    def test_concurrent_summary_pollers_only_send_once(self):
        with patch.object(notify, 'post', return_value={'ok': True}) as post:
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _: self.tick(), range(2)))
        self.assertEqual(sum(r['sent'] for r in results), 1)
        self.assertEqual(post.call_count, 1)

    def test_corrupt_receipt_prevents_summary_send(self):
        Path(self.temp.name, 'notified.jsonl').write_text('broken JSON\n')
        with patch.object(notify, 'post') as post, self.assertRaises(ValueError):
            self.tick()
        post.assert_not_called()

    def test_naive_local_task_dates_use_utc_history_and_latest_result(self):
        entries = [history('ok', 'error', '发送失败: confirmed_action_changed', id=1),
                   history('ok', 'sent', id=2)]
        title, text = summary.notification(task(created_at='2026-11-24T10:45:00',
            updated_at='2026-11-24T11:45:00'), entries, [])
        self.assertIn('11-24 11:45', title)
        self.assertIn('BOSS 1：合成公司ok', text)
        self.assertNotIn('动作变了', text)

    def test_master_or_all_channels_disabled_perform_no_reads(self):
        for master, channel in ((False, True), (True, False)):
            self.cfg.notify.enabled = master
            self.cfg.notify.channels['tg'].enabled = channel
            with patch.object(notify, 'post') as post:
                self.tick()
            post.assert_not_called()
        self.assertEqual(self.calls, [])

    def test_missing_or_invalid_timestamps_do_not_claim_company_results(self):
        for updated in (None, 'bad-time', '2026-11-24T09:00:00+08:00'):
            _, text = summary.notification(task(updated_at=updated), self.history, self.jobs)
            self.assertNotIn('✅', text)
            self.assertIn('成功明细缺少 1', text)
