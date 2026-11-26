import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine.config import EngineSettings
from engine.models import Job, Status, Reason
from engine.queue import DeliveryEngine
from store import SQLiteDeliveryStore
from engine.adapters import FakeAdapter, BossAdapter
from engine.preflight import FakePreflight
from engine.spaces import FakeTaskSpaces

class EngineTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'queue.sqlite3'
        self.store = SQLiteDeliveryStore(self.path)
        self.addCleanup(self.store.close)
        self.cfg = EngineSettings(platforms={'boss': dict(enabled=True, greeting='您好', greeting_max_length=10, daily_limit=10, interval_seconds=0, attachment_name='resume')})
        self.adapter = FakeAdapter()
        self.preflight = FakePreflight()
        self.spaces = FakeTaskSpaces()
        self.now = 1800000000.0
        self.engine = self.make_engine()

    def make_engine(self, store=None):
        return DeliveryEngine(store or self.store, self.cfg, {'boss': self.adapter}, self.preflight, self.spaces, clock=lambda: self.now, sleep=lambda _: None)

    def jobs(self, count=3):
        return [Job('boss', str(i), 'https://example.invalid/' + str(i)) for i in range(count)]

    def test_normal_and_duplicate(self):
        self.assertEqual(self.engine.enqueue(self.jobs() + self.jobs()), 3)
        self.engine.run()
        self.assertEqual([r.status for r in self.store.records()], [Status.SENT] * 3)
        self.assertEqual(self.engine.enqueue(self.jobs()), 0)
        self.engine.run()
        self.assertEqual(self.adapter.calls.count('send_greeting'), 3)
        self.assertEqual(self.adapter.calls.count('close'), 3)
        self.assertTrue(all(d['greeting']=='您好' and d['attachment_name']=='resume' for d in self.store.list_deliveries()))

    def test_driver_handover_stops_remaining_queue(self):
        from browser.base import SpaceUnavailable
        from unittest.mock import patch
        self.engine.enqueue(self.jobs())
        with patch.object(self.spaces, 'ensure', side_effect=SpaceUnavailable('user control')):
            self.engine.run()
        records = self.store.records()
        self.assertEqual(records[0].reason, Reason.TASK_SPACE)
        self.assertEqual([r.status for r in records], [Status.FAILED, Status.PENDING, Status.PENDING])
        self.assertNotIn('send_greeting', self.adapter.calls)

    def test_stop_and_restart(self):
        self.engine.enqueue(self.jobs())
        self.engine.run(should_stop=lambda: self.adapter.calls.count('close') >= 1)
        self.assertEqual([r.status for r in self.store.records()], [Status.SENT, Status.PENDING, Status.PENDING])
        self.store.close()
        reopened = SQLiteDeliveryStore(self.path)
        self.addCleanup(reopened.close)
        self.make_engine(reopened).run()
        self.assertEqual([r.status for r in reopened.records()], [Status.SENT] * 3)
        self.assertEqual(self.adapter.calls.count('send_greeting'), 3)

    def test_unverified_blocks_after_restart(self):
        self.adapter.receipt = False
        self.engine.enqueue(self.jobs())
        self.engine.run()
        self.assertEqual(self.store.records()[0].status, Status.UNVERIFIED)
        self.make_engine().run()
        self.assertEqual(self.adapter.calls.count('send_greeting'), 1)
        self.assertEqual(self.store.records()[1].status, Status.PENDING)

    def test_positive_application_without_greeting_stays_no_retry(self):
        cfg=EngineSettings(platforms={'zhilian':dict(enabled=True,greeting='您好',greeting_max_length=10,
            daily_limit=10,interval_seconds=0)})
        adapter=FakeAdapter(receipt=False)
        adapter.application_receipt_at='2026-11-25T12:00:00+08:00'
        engine=DeliveryEngine(self.store,cfg,{'zhilian':adapter},self.preflight,self.spaces,
                              clock=lambda:self.now,sleep=lambda _:None)
        engine.enqueue([Job('zhilian','a','https://example.invalid/a'),Job('zhilian','b','https://example.invalid/b')])
        engine.run()
        records=self.store.records()
        self.assertEqual(records[0].status,Status.UNVERIFIED)
        self.assertEqual(records[0].reason,Reason.APPLICATION_CONFIRMED)
        self.assertEqual(records[1].status,Status.PENDING)
        engine.run()
        self.assertEqual(adapter.calls.count('send_greeting'),1)

    def test_daily_limit_defers_and_resumes_next_day(self):
        self.cfg.platforms['boss'].daily_limit = 1
        self.engine.enqueue(self.jobs(2))
        self.engine.run()
        self.make_engine().run()
        self.assertEqual(self.adapter.calls.count('send_greeting'), 1)
        self.assertEqual(self.store.records()[1].reason, Reason.DAILY_LIMIT)
        self.now += 86400
        self.make_engine().run()
        self.assertEqual(self.adapter.calls.count('send_greeting'), 2)

    def test_oversized_greeting_rejected_without_truncation(self):
        with self.assertRaises(ValueError):
            EngineSettings(platforms={'boss': dict(greeting='您好', greeting_max_length=1)})

    def test_dry_run_never_accepts_real_adapter(self):
        self.engine.adapters['boss'] = BossAdapter()
        self.engine.enqueue(self.jobs(1))
        with self.assertRaises(ValueError):
            self.engine.run()
        self.assertEqual(self.store.records()[0].status, Status.PENDING)

    def test_preflight_failures_never_send(self):
        from unittest.mock import patch
        for check, reason in [('task_space_valid', Reason.TASK_SPACE), ('logged_in', Reason.LOGIN_REQUIRED),
                              ('attachment_matches', Reason.ATTACHMENT)]:
            store = SQLiteDeliveryStore(Path(self.temp.name) / (check + '.sqlite3'))
            self.addCleanup(store.close)
            engine = self.make_engine(store)
            job = Job('boss', check, 'https://example.invalid/job')
            engine.enqueue([job])
            with patch.object(self.preflight, check, return_value=False):
                engine.run()
            self.assertEqual(store.records()[-1].reason, reason)
            self.assertEqual(store.records()[-1].status, Status.FAILED)
        self.assertNotIn('send_greeting', self.adapter.calls)

    def test_missing_or_ambiguous_attachment(self):
        for names in [[], ['resume', 'resume'], ['other']]:
            self.preflight.attachments = names
            self.engine.enqueue([Job('boss', str(names), 'https://example.invalid')])
            self.engine.run()
            self.assertEqual(self.store.records()[-1].reason, Reason.ATTACHMENT)
        self.assertEqual(self.adapter.calls, [])

    def test_already_applied_skips(self):
        self.adapter.applied = True
        self.engine.enqueue(self.jobs(1))
        self.engine.run()
        self.assertEqual(self.store.records()[0].status, Status.SKIPPED)
        self.assertEqual(self.store.records()[0].reason, Reason.ALREADY_APPLIED)
        self.assertNotIn('send_greeting', self.adapter.calls)

    def test_unknown_check_or_attachment_blocks(self):
        for kind in ['applied', 'attachment']:
            with self.subTest(kind=kind):
                # Independent queue because unresolved outcomes intentionally never reset.
                store = SQLiteDeliveryStore(Path(self.temp.name) / (kind + '.sqlite3'))
                self.addCleanup(store.close)
                adapter = FakeAdapter(applied=None if kind == 'applied' else False,
                                      attachment=False if kind == 'attachment' else True)
                engine = self.make_engine(store)
                engine.adapters = {'boss': adapter}
                engine.enqueue(self.jobs())
                engine.run()
                self.assertEqual(store.records()[0].status, Status.UNVERIFIED)
                self.assertEqual(store.records()[1].status, Status.PENDING)
                self.assertEqual(adapter.calls.count('open_job'), 1)

    def test_effect_exception_blocks_and_closes(self):
        from unittest.mock import patch
        self.engine.enqueue(self.jobs())
        with patch.object(self.adapter, 'send_greeting', side_effect=TimeoutError):
            self.engine.run()
        self.assertEqual(self.store.records()[0].reason, Reason.EFFECT_ERROR)
        self.assertEqual(self.store.records()[1].status, Status.PENDING)
        self.assertEqual(self.adapter.calls.count('close'), 1)

    def test_cleanup_failure_stops_without_erasing_sent(self):
        from unittest.mock import patch
        self.engine.enqueue(self.jobs())
        with patch.object(self.adapter, 'close', side_effect=RuntimeError):
            with self.assertRaises(RuntimeError):
                self.engine.run()
        self.assertEqual(self.store.records()[0].status, Status.SENT)
        self.assertEqual(self.store.records()[1].status, Status.PENDING)

    def test_other_platform_continues_and_same_id_is_distinct(self):
        self.cfg.platforms['zhilian'] = self.cfg.platforms['boss'].model_copy()
        other = FakeAdapter()
        self.engine.adapters['zhilian'] = other
        self.adapter.receipt = False
        self.engine.enqueue(self.jobs(2) + [Job('zhilian', '0', 'https://example.invalid')])
        self.engine.run()
        self.assertEqual([r.status for r in self.store.records()],
                         [Status.UNVERIFIED, Status.PENDING, Status.SENT])

    def test_disabled_and_zero_budget_are_pending(self):
        self.cfg.platforms['boss'].enabled = False
        self.engine.enqueue(self.jobs(1))
        self.engine.run()
        self.assertEqual(self.store.records()[0].reason, Reason.DISABLED)
        self.cfg.platforms['boss'].enabled = True
        self.cfg.platforms['boss'].daily_limit = 0
        self.engine.run()
        self.assertEqual(self.store.records()[0].reason, Reason.DAILY_LIMIT)
        self.assertEqual(self.adapter.calls, [])

    def test_interval_survives_restart_and_stop_is_responsive(self):
        self.cfg.platforms['boss'].interval_seconds = 2
        self.engine.enqueue(self.jobs(2))
        self.engine.run(should_stop=lambda: self.adapter.calls.count('close') == 1)
        delays = []
        def sleep(delay):
            delays.append(delay)
            self.now += delay
        resumed = self.make_engine()
        resumed.sleep = sleep
        resumed.run(should_stop=lambda: len(delays) == 1)
        self.assertEqual(self.adapter.calls.count('send_greeting'), 1)
        resumed.run()
        self.assertEqual(sum(delays), 2)
        self.assertEqual(self.adapter.calls.count('send_greeting'), 2)

    def test_named_space_reused_and_recreated(self):
        self.engine.enqueue(self.jobs(2))
        self.engine.run(should_stop=lambda: self.adapter.calls.count('close') == 1)
        self.spaces.names.clear()
        self.engine.run()
        self.assertEqual(self.spaces.created, ['job-agent 投递'] * 2)
        self.spaces.ensure('job-agent 投递')
        self.assertEqual(len(self.spaces.created), 2)

    def test_store_mode_and_runner_lock(self):
        with self.assertRaises(ValueError):
            SQLiteDeliveryStore(self.path, dry_run=False)
        other = SQLiteDeliveryStore(self.path)
        self.addCleanup(other.close)
        with self.store.exclusive():
            with self.assertRaises(RuntimeError):
                self.make_engine(other).run()
        self.cfg.dry_run = False
        with self.assertRaises(NotImplementedError):
            self.engine.run()

    def test_actual_process_death_at_effect_boundary(self):
        import subprocess
        script = '''
import os, sys
from unittest.mock import patch
from engine.config import EngineSettings
from engine.models import Job
from store import SQLiteDeliveryStore
from engine.queue import DeliveryEngine
from engine.adapters import FakeAdapter
from engine.preflight import FakePreflight
from engine.spaces import FakeTaskSpaces
store = SQLiteDeliveryStore(sys.argv[1])
cfg = EngineSettings(platforms={'boss': dict(enabled=True, greeting='你好', greeting_max_length=10, daily_limit=10, interval_seconds=0)})
adapter = FakeAdapter()
engine = DeliveryEngine(store, cfg, {'boss': adapter}, FakePreflight(), FakeTaskSpaces())
engine.enqueue([Job('boss', 'crash', 'https://example.invalid'), Job('boss', 'remaining', 'https://example.invalid')])
with patch.object(adapter, 'send_greeting', side_effect=lambda _: os._exit(23)):
    engine.run()
'''
        path = Path(self.temp.name) / 'crash.sqlite3'
        result = subprocess.run([sys.executable, '-c', script, str(path)], cwd=Path(__file__).resolve().parents[1], timeout=10)
        self.assertEqual(result.returncode, 23)
        store = SQLiteDeliveryStore(path)
        self.addCleanup(store.close)
        self.assertEqual(store.records()[0].reason, Reason.INTERRUPTED)
        self.make_engine(store).run()
        self.assertEqual(store.records()[1].status, Status.PENDING)
        self.assertEqual(self.adapter.calls, [])

    def test_example_config_loads_and_limits_not_guessed(self):
        import yaml
        from config import Settings
        example = Path(__file__).resolve().parents[2] / 'config.example.yaml'
        cfg = Settings.model_validate(yaml.safe_load(example.read_text()))
        self.assertTrue(cfg.engine.dry_run)
        self.assertTrue(all(not p.enabled and p.greeting_max_length == 0 for p in cfg.engine.platforms.values()))
        with self.assertRaises(ValueError):
            EngineSettings(platforms={'boss': {'enabled': True}})
        with self.assertRaises(ValueError):
            EngineSettings(platforms={'unknown': {}})


    def test_unknown_applied_exception_stops_platform(self):
        from unittest.mock import patch
        self.engine.enqueue(self.jobs())
        with patch.object(self.adapter, 'check_applied', side_effect=TimeoutError):
            self.engine.run()
        self.assertEqual(self.store.records()[0].status, Status.UNVERIFIED)
        self.assertEqual(self.store.records()[0].reason, Reason.RECEIPT_UNKNOWN)
        self.assertEqual(self.store.records()[1].status, Status.PENDING)
        self.assertEqual(self.adapter.calls.count('open_job'), 1)
        self.assertNotIn('send_greeting', self.adapter.calls)

    def test_full_greeting_above_legacy_300_limit(self):
        self.cfg.platforms['boss'].greeting_max_length = 500
        self.cfg.platforms['boss'].greeting = '您' * 500
        self.engine.enqueue(self.jobs(1))
        self.engine.run()
        self.assertEqual(self.adapter.greetings, ['您' * 500])
        self.assertEqual(self.store.records()[0].status, Status.SENT)
        with self.assertRaises(ValueError):
            EngineSettings(platforms={'boss': dict(greeting='您' * 501, greeting_max_length=500)})

    def test_all_platform_stubs_are_unimplemented(self):
        from engine.adapters import BossAdapter, ZhilianAdapter, LiepinAdapter, Job51Adapter
        for cls in (BossAdapter, ZhilianAdapter, LiepinAdapter, Job51Adapter):
            for method, args in [('open_job', (self.jobs(1)[0], 'space')), ('check_applied', ()),
                                 ('send_greeting', ('您好',)), ('choose_attachment', ('resume',)),
                                 ('confirm_receipt', ()), ('close', ())]:
                with self.subTest(adapter=cls.__name__, method=method):
                    with self.assertRaisesRegex(NotImplementedError, 'P0'):
                        getattr(cls(), method)(*args)
