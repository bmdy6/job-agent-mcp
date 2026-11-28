"""A second daily trigger while one is running is a skip, not a failure (11-27 incident)."""
import json
import os
import tempfile
import unittest
from unittest import mock
from pathlib import Path


class DailyAlreadyRunningTests(unittest.TestCase):
    def test_concurrent_trigger_exits_zero_without_alert(self):
        from job_agent.service import daily
        from job_agent import cli
        home = Path(os.environ['JOB_AGENT_HOME'])
        with daily.run_lock(home), mock.patch('job_agent.observability.report_error') as reported, \
                mock.patch('job_agent.cli.report_error') as cli_reported, \
                mock.patch('builtins.print') as printed:
            code = cli.main(['daily', '--once', '--scheduled'])
        self.assertEqual(code, 0)
        reported.assert_not_called()
        cli_reported.assert_not_called()
        self.assertEqual(json.loads(printed.call_args[0][0])['reason'], 'already_running')

    def test_still_a_runtime_error_for_old_callers(self):
        from job_agent.service import daily
        self.assertTrue(issubclass(daily.DailyAlreadyRunning, RuntimeError))
