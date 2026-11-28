from state_helpers import read_state_file, write_state_file
import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from job_agent.config import Settings
from job_agent.service import heartbeat


class HeartbeatTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.config = Settings()
        self.sent = []
        self.send = lambda title, text: self.sent.append((title, text)) or True

    def runtime(self, last_poll_at, state='running'):
        write_state_file(self.home / 'monitor-runtime.json', json.dumps({'state': state, 'last_poll_at': last_poll_at}))

    def test_stale_monitor_alerts_once_then_recovers_once(self):
        now = 1_800_000_000.0
        interval = self.config.engine.monitor.interval_minutes * 60
        self.runtime(now - interval * (self.config.observability.heartbeat_stale_polls + 1))
        self.assertEqual(heartbeat.run_once(self.config, self.home, send=self.send, now=now)['alerted'], ['monitor_stale'])
        self.assertEqual(heartbeat.run_once(self.config, self.home, send=self.send, now=now + 60)['alerted'], [])
        self.assertEqual(len(self.sent), 1)
        self.runtime(now + 120)
        result = heartbeat.run_once(self.config, self.home, send=self.send, now=now + 180)
        self.assertEqual(result['recovered'], ['monitor_stale'])
        self.assertIn('已恢复', self.sent[-1][1])

    def test_fresh_monitor_and_disabled_daily_are_quiet(self):
        now = 1_800_000_000.0
        self.runtime(now - 30)
        self.config.daily.enabled = False
        self.assertEqual(heartbeat.problems(self.config, self.home, now), {})

    def test_missing_or_stopped_monitor_is_reported(self):
        self.assertIn('monitor_missing', heartbeat.problems(self.config, self.home, 1_800_000_000.0))
        self.runtime(1_800_000_000.0, state='failed')
        self.assertIn('monitor_state', heartbeat.problems(self.config, self.home, 1_800_000_000.0))

    def test_missed_daily_run_after_grace(self):
        self.config.daily.enabled = True
        self.config.daily.hour, self.config.daily.minute = 8, 0
        day = datetime(2026, 11, 27, 10, 0)
        self.runtime(day.timestamp())
        write_state_file(self.home / 'daily-last-run.json', json.dumps({'started_at': datetime(2026, 11, 26, 8, 0).isoformat()}))
        self.assertIn('daily_missed', heartbeat.problems(self.config, self.home, day.timestamp()))
        write_state_file(self.home / 'daily-last-run.json', json.dumps({'started_at': datetime(2026, 11, 27, 8, 1).isoformat()}))
        self.assertNotIn('daily_missed', heartbeat.problems(self.config, self.home, day.timestamp()))

    def test_failed_send_is_retried_next_run(self):
        now = 1_800_000_000.0
        self.runtime(0)
        heartbeat.run_once(self.config, self.home, send=lambda t, x: False, now=now)
        self.assertEqual(heartbeat.run_once(self.config, self.home, send=self.send, now=now + 60)['alerted'], ['monitor_stale'])


class LogSetupTest(unittest.TestCase):
    def test_rotating_file_and_warning_only_stderr(self):
        import logging
        from logging.handlers import RotatingFileHandler
        from job_agent.logsetup import setup_logging
        with tempfile.TemporaryDirectory() as tmp:
            path = setup_logging('monitor', home=Path(tmp), max_bytes=1000, backups=2)
            handlers = logging.getLogger().handlers
            rotating = [h for h in handlers if isinstance(h, RotatingFileHandler)]
            self.assertEqual(len(rotating), 1)
            self.assertEqual((rotating[0].maxBytes, rotating[0].backupCount), (1000, 2))
            self.assertEqual(path, Path(tmp) / 'logs' / 'monitor.log')
            streams = [h for h in handlers if not isinstance(h, RotatingFileHandler)]
            self.assertTrue(all(h.level == logging.WARNING for h in streams))
            for h in rotating:
                h.close()
            logging.getLogger().handlers.clear()


if __name__ == '__main__':
    unittest.main()

class ResumeHeartbeatTest(HeartbeatTest):
    def test_alert_repeats_only_after_configured_cooldown(self):
        now=1800000000.
        self.runtime(1)
        cooldown=self.config.observability.heartbeat_cooldown_minutes*60
        for offset,expected in [(0,['monitor_stale']),(cooldown-1,[]),(cooldown,['monitor_stale'])]:
            self.assertEqual(heartbeat.run_once(self.config,self.home,send=self.send,now=now+offset)['alerted'],expected)
        self.assertIn('原因码',self.sent[0][1])
        self.assertIn('最后成功时间',self.sent[0][1])

    def test_failed_polls_do_not_refresh_success(self):
        now=1800000000.
        write_state_file(self.home/'monitor-runtime.json', json.dumps(dict(state='running',last_poll_at=now,
            last_success_at=now-86400,result={'failed':1})))
        self.assertIn('monitor_stale',heartbeat.problems(self.config,self.home,now))

    def test_daily_stalled_and_outside_hours_and_completed(self):
        self.config.daily.enabled=True
        now=datetime(2026,11,27,14).timestamp()
        report=dict(status='running',started_at=datetime(2026,11,27,9).isoformat(),
                    updated_at=datetime.fromtimestamp(now).isoformat(),current_step='delivery',reason_code='network_error')
        path=self.home/'daily-last-run.json'
        write_state_file(path, json.dumps(report))
        self.runtime(now)
        problems=heartbeat.problems(self.config,self.home,now)
        self.assertIn('daily_stale',problems)
        self.assertIn('network_error',problems['daily_stale'])
        self.assertNotIn('daily_stale',heartbeat.problems(self.config,self.home,datetime(2026,11,27,22).timestamp()))
        report['status']='completed';write_state_file(path, json.dumps(report))
        self.assertNotIn('daily_stale',heartbeat.problems(self.config,self.home,now))
