"""Delivery notification tests use synthetic tasks, history and fake Telegram."""
import sys
import unittest
import os
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import job_agent.delivery_summary as summary


def task(**changes):
    return {'id': 'synthetic-task', 'mode': 'deliver', 'status': 'completed',
            'created_at': '2026-11-24T10:45:00+08:00', 'updated_at': '2026-11-24T11:45:00+08:00',
            'metrics': {'send_requested': 3, 'send_success': 1, 'send_failed': 2, 'send_deferred': 0}, **changes}


def history(jid, action, detail='', **changes):
    return {'id': jid, 'job_id': jid, 'action': action, 'detail': detail,
            'company': '合成公司'+jid, 'source_platform': 'boss', 'created_at': '2026-11-24 03:00:00', **changes}


class DeliverySummaryTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(time.tzset)
        env = patch.dict(os.environ, TZ="Asia/Shanghai")
        env.start(); self.addCleanup(env.stop)
        time.tzset()









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




    def test_naive_local_task_dates_use_utc_history_and_latest_result(self):
        entries = [history('ok', 'error', '发送失败: confirmed_action_changed', id=1),
                   history('ok', 'sent', id=2)]
        title, text = summary.notification(task(created_at='2026-11-24T10:45:00',
            updated_at='2026-11-24T11:45:00'), entries, [])
        self.assertIn('11-24 11:45', title)
        self.assertIn('BOSS 1：合成公司ok', text)
        self.assertNotIn('动作变了', text)


    def test_missing_or_invalid_timestamps_do_not_claim_company_results(self):
        self.history, self.jobs = [history("ok", "sent")], []
        for updated in (None, 'bad-time', '2026-11-24T09:00:00+08:00'):
            _, text = summary.notification(task(updated_at=updated), self.history, self.jobs)
            self.assertNotIn('✅', text)
            self.assertIn('成功明细缺少 1', text)
