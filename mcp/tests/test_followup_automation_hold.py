"""Followups obey the global TG pause switch from #118."""
import unittest
from unittest import mock


class FollowupHoldTests(unittest.TestCase):
    def _config(self, enabled=True):
        cfg = mock.Mock()
        cfg.followup.enabled = enabled
        return cfg

    def test_paused_mode_skips_followup_before_planning(self):
        from job_agent.service import followup
        store = mock.Mock()
        with mock.patch('job_agent.service.automation.mode', return_value='paused'):
            report = followup.run(store, self._config())
        self.assertEqual(report['mode'], 'held')
        self.assertEqual(report['reason_code'], 'automation_paused')
        store.connection.execute.assert_not_called()

    def test_only_reply_also_holds_followup(self):
        from job_agent.service import followup
        with mock.patch('job_agent.service.automation.mode', return_value='only_reply'):
            report = followup.run(mock.Mock(), self._config())
        self.assertEqual(report['reason_code'], 'automation_only_reply')

    def test_today_reports_disabled_and_held(self):
        from job_agent.service.commands import followup_summary
        self.assertEqual(followup_summary(None, self._config(False), 0, 'running')['status'], 'disabled')
        self.assertEqual(followup_summary(None, self._config(True), 0, 'paused')['status'], 'held')
