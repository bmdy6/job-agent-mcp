"""A collection timeout is an incomplete round, not a platform stop (#67)."""
import unittest
from unittest import mock


class TimeoutNotAlertTests(unittest.TestCase):
    def test_collection_timeout_does_not_alert(self):
        from job_agent import observability
        from job_agent.collect.deadline import CollectionTimeout
        with mock.patch.object(observability, '_alert') as alert:
            details = observability.report_error(CollectionTimeout('timed out'), platform='boss', stage='collection')
        self.assertEqual(details['reason_code'], 'collection_timeout')
        alert.assert_not_called()

    def test_real_error_still_alerts(self):
        from job_agent import observability
        with mock.patch.object(observability, '_alert') as alert, \
                mock.patch('job_agent.config.load_config'):
            observability.report_error(RuntimeError('boom'), platform='boss', stage='collection')
        alert.assert_called_once()
