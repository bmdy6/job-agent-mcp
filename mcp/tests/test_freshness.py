import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

from job_agent.config.engine import EngineSettings
from job_agent.engine.models import Job, Status
from job_agent.engine.adapters import FakeAdapter
from job_agent.engine.preflight import FakePreflight
from job_agent.engine.spaces import FakeTaskSpaces
from job_agent.engine.queue import DeliveryEngine
from job_agent.platforms.availability import JobUnavailable
from job_agent.store import SQLiteDeliveryStore


class FreshnessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = SQLiteDeliveryStore(Path(self.tmp.name)/'jobs.sqlite3')
        self.addCleanup(self.store.close)
        self.cfg = EngineSettings(platforms={'boss':dict(enabled=True,greeting='你好',greeting_max_length=10,
            daily_limit=2,interval_seconds=0)})
        self.adapter = FakeAdapter()
        self.engine = DeliveryEngine(self.store,self.cfg,{'boss':self.adapter},FakePreflight(),FakeTaskSpaces(),clock=lambda:1795834800)
        self.job = Job('boss','1','https://example.invalid/1')
        self.engine.enqueue([self.job])
        self.store.upsert_job(platform='boss',platform_job_id='1',collected_at='2026-11-20T00:00:00+00:00')

    def test_default_and_boundary(self):
        from job_agent.engine.freshness import needs_check
        self.assertEqual(self.cfg.max_job_age_hours,48)
        self.assertFalse(needs_check('2026-11-26T03:00:00+00:00',48,1795834800))
        self.assertTrue(needs_check('2026-11-26T02:59:59+00:00',48,1795834800))
        self.assertTrue(needs_check('bad',48,1795834800))

    def test_old_closed_has_no_send_no_quota_and_next_job_can_use_slot(self):
        second = Job('boss','2','https://example.invalid/2')
        self.engine.enqueue([second])
        self.store.upsert_job(platform='boss',platform_job_id='2',collected_at='2026-11-28T02:00:00+00:00')
        with patch.object(self.adapter,'check_freshness',return_value='closed'):
            rows = self.engine.run()
        self.assertEqual(rows[0].status,Status.SKIPPED)
        self.assertEqual(rows[0].reason.value,'job_closed')
        self.assertIsNone(rows[0].attempted_at)
        self.assertEqual(rows[1].status,Status.SENT)
        self.assertEqual(len(self.store.attempt_times('boss')),1)
        self.assertEqual(self.adapter.calls.count('send_greeting'),1)

    def test_old_unknown_stays_pending_without_effect_or_quota(self):
        with patch.object(self.adapter,'check_freshness',return_value='unknown'):
            row = self.engine.run()[0]
        self.assertEqual(row.status,Status.PENDING)
        self.assertEqual(row.reason.value,'freshness_unknown')
        self.assertEqual(self.store.attempt_times('boss'),[])
        self.assertNotIn('send_greeting',self.adapter.calls)

    def test_active_stale_checked_and_delivery_continues(self):
        self.assertEqual(self.engine.run()[0].status,Status.SENT)
        self.assertEqual(self.adapter.freshness_checks,1)
        self.assertEqual(self.adapter.calls.count('send_greeting'),1)

    def test_fresh_job_does_not_get_additional_open(self):
        self.store.upsert_job(platform='boss',platform_job_id='1',collected_at='2026-11-28T02:00:00+00:00')
        self.assertEqual(self.engine.run()[0].status,Status.SENT)
        self.assertEqual(self.adapter.calls.count('open_job'),1)

    def test_live_closed_stale_job_never_reserves_daily_or_hourly_quota(self):
        from job_agent.platforms.boss.apply import BossAdapter
        from job_agent.platforms.preflight import AdapterPreflight
        from job_agent.engine.spaces import TaskSpaces
        cfg = self.cfg.model_copy(deep=True)
        cfg.dry_run=False
        cfg.live_send_enabled=True
        with SQLiteDeliveryStore(Path(self.tmp.name)/'live.sqlite3',dry_run=False) as store:
            store.upsert_job(platform='boss',platform_job_id='1',url=self.job.url,
                             collected_at='2026-11-20T00:00:00+00:00')
            token=store.create_batch([self.job],{'boss':'你好'},'',cfg.model_dump())
            store.confirm_batch(token,config=cfg.model_dump())
            browser=Mock()
            adapter=BossAdapter(browser,cfg.platforms['boss'])
            engine=DeliveryEngine(store,cfg,{'boss':adapter},AdapterPreflight(browser,{'boss':adapter}),
                TaskSpaces(browser),clock=lambda:1795834800,safety=Mock())
            with patch.object(adapter,'open_job',side_effect=JobUnavailable('岗位已关闭')), \
                 patch('job_agent.engine.rate.RateLimiter.acquire') as quota, \
                 patch.object(adapter,'send_greeting') as send:
                row=engine.run(batch_token=token)[0]
            quota.assert_not_called()
            send.assert_not_called()
            self.assertEqual(row.reason.value,'job_closed')
            self.assertEqual(store.attempt_times('boss'),[])
