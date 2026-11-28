from datetime import datetime
from zoneinfo import ZoneInfo
import unittest

from job_agent.config import Settings
from job_agent.config.calendar import delivery_policy, effective_limit
from job_agent.service.daily_schedule import active


def stamp(day, hour=12):
    return datetime(2026, 11, day, hour, tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()


class CalendarTests(unittest.TestCase):
    def config(self, **rules):
        return Settings(engine={**rules, 'platforms': {'boss': {
            'daily_limit': 10, 'enabled': True, 'greeting_max_length': 100,
            'day_limits': {'sun': 4}, 'day_allowed_hours': {'sun': ['14:00', '16:00']}}}},
            daily={'collect_enabled': False})

    def test_weekday_legacy_and_platform_override_minimum(self):
        cfg = self.config(day_limits={'fri': 8, 'sat': 3, 'sun': 2}, weekend_daily_limit=1)
        self.assertEqual(effective_limit(cfg.engine, 'boss', stamp(27)), 8)
        self.assertEqual(effective_limit(cfg.engine, 'boss', stamp(28)), 1)
        self.assertEqual(effective_limit(cfg.engine, 'boss', stamp(29)), 1)
        cfg.engine.weekend_daily_limit = None
        self.assertEqual(effective_limit(cfg.engine, 'boss', stamp(29)), 4)
        self.assertEqual(effective_limit(cfg.engine, 'boss', stamp(30)), 10)
        cfg.engine.day_limits = {'mon': 0}
        self.assertEqual(effective_limit(cfg.engine, 'boss', stamp(30)), 0)

    def test_rate_limit_cannot_raise_platform_or_calendar_cap(self):
        cfg = self.config(day_limits={'sat': 2})
        cfg.engine.platforms['boss'].rate.daily_limit = 100
        self.assertEqual(delivery_policy(cfg.engine, 'boss', stamp(28)).daily_limit, 2)
        cfg.engine.platforms['boss'].rate.daily_limit = 1
        self.assertEqual(effective_limit(cfg.engine, 'boss', stamp(28)), 1)

    def test_hours_platform_override_and_scheduler(self):
        cfg = self.config(day_allowed_hours={'sat': ['10:00', '11:00'], 'sun': ['09:00', '10:00']})
        self.assertFalse(active(cfg, stamp(28, 12)))
        self.assertTrue(active(cfg, stamp(28, 10)))
        self.assertFalse(active(cfg, stamp(29, 9)))
        self.assertTrue(active(cfg, stamp(29, 14)))
        self.assertEqual(delivery_policy(cfg.engine, 'boss', stamp(30)).allowed_hours, ('09:00', '21:00'))

    def test_engine_timezone_selects_day_and_invalid_rules_rejected(self):
        cfg = self.config(day_limits={'sat': 2})
        cfg.engine.timezone = 'UTC'
        self.assertEqual(effective_limit(cfg.engine, 'boss', stamp(28, 1)), 10)
        for rules in ({'day_limits': {'Saturday': 2}}, {'day_limits': {'sat': -1}},
                      {'day_limits': {'sat': True}}, {'day_allowed_hours': {'sat': ['25:00', '26:00']}},
                      {'day_allowed_hours': {'sat': ['09:00', '09:00']}}):
            with self.subTest(rules=rules), self.assertRaises(ValueError): self.config(**rules)

    def test_policy_handles_existing_validate_assignment_rate_migration(self):
        cfg = self.config(day_limits={'sat': 2})
        cfg.engine.platforms['boss'].daily_limit = 5
        self.assertEqual(effective_limit(cfg.engine, 'boss', stamp(28)), 2)
        self.assertEqual(delivery_policy(cfg.engine, 'boss', stamp(28)).daily_limit, 2)

    def test_shared_rule_gates_queue_planning_and_execution(self):
        import tempfile
        from pathlib import Path
        from job_agent.store import SQLiteDeliveryStore
        from job_agent.engine.models import Job, Status, Reason
        from job_agent.engine.queue import DeliveryEngine
        from job_agent.engine.adapters import FakeAdapter
        from job_agent.engine.preflight import FakePreflight
        from job_agent.engine.spaces import FakeTaskSpaces
        cfg = self.config(day_limits={'sat': 1})
        cfg.engine.platforms['boss'].interval_seconds = 0
        with tempfile.TemporaryDirectory() as home:
            with SQLiteDeliveryStore(Path(home)/'queue.sqlite3') as db:
                adapter = FakeAdapter()
                engine = DeliveryEngine(db, cfg.engine, {'boss': adapter}, FakePreflight(), FakeTaskSpaces(),
                                        clock=lambda: stamp(28), sleep=lambda _: None)
                engine.enqueue([Job('boss', str(i), 'https://example.invalid/'+str(i)) for i in range(2)])
                self.assertEqual(len(db.daily_candidates(cfg.engine, cutoff=100, clock=stamp(28))), 1)
                engine.run()
                records = db.records()
                self.assertEqual(sum(r.status == Status.SENT for r in records), 1)
                self.assertEqual(sum(r.reason == Reason.DAILY_LIMIT for r in records), 1)
