import unittest
from unittest.mock import Mock
from pydantic import ValidationError
import test_monitor
from test_monitor import Source
from job_agent.config.monitor import MonitorSettings
from job_agent.config.rate import RatePolicy


class MonitorHoursTests(unittest.TestCase):
    setUp = test_monitor.MonitorTests.setUp

    def test_defaults_and_validation(self):
        self.assertEqual(MonitorSettings().allowed_hours, ('09:00', '21:00'))
        self.assertEqual(MonitorSettings(allowed_hours=['21:00','09:00']).allowed_hours, ('21:00','09:00'))
        for hours in [('24:00','21:00'), ('9:00','21:00'), ('09:00','09:00'), ('xx:00','21:00')]:
            with self.subTest(hours=hours), self.assertRaises(ValidationError):
                MonitorSettings(allowed_hours=hours)

    def test_monitor_uses_own_hours_without_changing_delivery_policy(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from job_agent.monitor import Monitor
        from job_agent.engine.rate import RateDeferred
        self.config.engine.platforms['boss'].rate.allowed_hours=('10:00','18:00')
        original=self.config.engine.platforms['boss'].model_dump()
        night=datetime(2026,11,28,22,tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
        policies=[]
        def acquire(platform, scope, policy):
            policies.append(policy)
            if not policy.allowed(night):
                raise RateDeferred('outside_hours')
        limiter=Mock();limiter.acquire.side_effect=acquire
        source=Source('boss')
        settings=MonitorSettings(allowed_hours=['08:00','23:00'])
        row=Monitor(self.browser,[source],self.store,self.config,settings,limiter=limiter).poll_once()['platforms']['boss']
        self.assertEqual(row['status'],'ok')
        self.assertEqual(policies[-1].allowed_hours,('08:00','23:00'))
        self.assertIsNone(policies[-1].daily_limit)
        self.assertEqual(self.config.engine.platforms['boss'].model_dump(),original)
        row=Monitor(self.browser,[source],self.store,self.config,MonitorSettings(),limiter=limiter).poll_once()['platforms']['boss']
        self.assertEqual(row['status'],'outside_hours')
        self.assertEqual(len(source.calls),1)
        self.sender.assert_not_called()
        self.assertEqual(self.config.engine.platforms['boss'].model_dump(),original)

    def test_default_window_boundaries_and_overnight(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        for hours, cases in [(MonitorSettings().allowed_hours, [('08:59',False),('09:00',True),('20:59',True),('21:00',False)]),
                              (MonitorSettings(allowed_hours=['21:00','09:00']).allowed_hours,[('22:00',True),('08:59',True),('09:00',False)])]:
            policy=RatePolicy(allowed_hours=hours)
            for clock,expected in cases:
                stamp=datetime.fromisoformat('2026-11-28T'+clock).replace(tzinfo=ZoneInfo(policy.timezone)).timestamp()
                self.assertEqual(policy.allowed(stamp),expected)
