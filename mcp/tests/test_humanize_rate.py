import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class PointerTest(unittest.TestCase):
    def test_seeded_trajectory_bounds_and_continuity(self):
        source = Path(__file__).resolve().parents[1] / 'browser' / 'humanize.js'
        script = source.read_text() + '''
const cfg = {shrink: .6, duration_ms:[250,900], steps:[20,60], step_ms:[8,25],
 noise_px:1.5, overshoot_probability:.3, overshoot_px:[2,12], hold_ms:[50,150], typing_ms:[60,180]};
const box={x:100,y:100,width:100,height:50}, viewport={width:1000,height:800};
const paths=Array.from({length:100},(_,seed)=>trajectory(box,viewport,null,cfg,seed));
console.log(JSON.stringify(paths));
'''
        data = json.loads(subprocess.check_output(['node', '--input-type=module', '-e', script], text=True))
        ends = []
        for item in data:
            points = item['points']
            self.assertTrue(21 <= len(points)-1 <= 61)
            self.assertTrue(250 <= sum(p['delay_ms'] for p in points) <= 900.001)
            x,y = points[-1]['x'],points[-1]['y']
            self.assertTrue(120 <= x <= 180 and 110 <= y <= 140)
            self.assertNotEqual((x,y),(150,125))
            self.assertTrue(all(0 <= p['x'] < 1000 and 0 <= p['y'] < 800 for p in points))
            self.assertTrue(all(8 <= p['delay_ms'] <= 25 for p in points[1:]))
            ends.append((x,y))
        self.assertGreater(len(set(ends)),90)
        self.assertEqual(len({json.dumps(x) for x in data}),100)
        script = source.read_text() + '''
const cfg=JSON.parse(process.argv[1]);
const a=trajectory({x:1,y:1,width:100,height:100},{width:500,height:500},[222,333],cfg,5);
const b=trajectory({x:1,y:1,width:100,height:100},{width:500,height:500},[222,333],cfg,5);
console.log(JSON.stringify({a,b}));'''
        cfg = dict(shrink=.6,duration_ms=[250,900],steps=[20,60],step_ms=[8,25],noise_px=1.5,overshoot_probability=.3,overshoot_px=[2,12])
        pair=json.loads(subprocess.check_output(['node','--input-type=module','-e',script,json.dumps(cfg)],text=True))
        self.assertEqual(pair['a'],pair['b'])
        self.assertEqual([pair['a']['points'][0][k] for k in ('x','y')],[222,333])


class RateTest(unittest.TestCase):
    def setUp(self):
        from store import Store
        from store.automation import AutomationRepository
        from engine.rate import RatePolicy, RateLimiter
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.store=Store(Path(self.temp.name)/'test.sqlite3'); self.addCleanup(self.store.close)
        self.repo=AutomationRepository(self.store)
        self.now=1795654800.0  # 2026-11-26 09:00 Shanghai
        self.policy=RatePolicy(interval_seconds=(45,120), hourly_limit=2,daily_limit=3,rest_every=(2,2),rest_seconds=(300,300))
        self.limiter=RateLimiter(self.repo,clock=lambda:self.now,sleep=self.sleep,seed=5)
    def sleep(self,d): self.now+=d
    def test_hour_day_rest_and_restart(self):
        from engine.rate import RateDeferred, RateLimiter
        self.limiter.acquire('boss','delivery',self.policy)
        first=self.now
        self.limiter.acquire('boss','delivery',self.policy)
        self.assertTrue(45<=self.now-first<=120)
        with self.assertRaises(RateDeferred) as caught:self.limiter.acquire('boss','delivery',self.policy)
        self.assertEqual(caught.exception.code,'hourly_limit')
        self.now=first+3600
        RateLimiter(self.repo,clock=lambda:self.now,sleep=self.sleep).acquire('boss','delivery',self.policy)
        self.now+=3600
        with self.assertRaises(RateDeferred) as caught:self.limiter.acquire('boss','delivery',self.policy)
        self.assertEqual(caught.exception.code,'daily_limit')
        self.now+=86400
        self.limiter.acquire('boss','delivery',self.policy)
    def test_rest_persisted_and_window_rechecked_after_wait(self):
        from engine.rate import RateDeferred, RateLimiter
        self.policy.hourly_limit=20
        self.limiter.acquire('boss','collect',self.policy)
        self.limiter.acquire('boss','collect',self.policy)
        last=self.now
        RateLimiter(self.repo,clock=lambda:self.now,sleep=self.sleep).acquire('boss','collect',self.policy)
        self.assertGreaterEqual(self.now-last,300)
        self.now=1795698000.0  # 21:00
        with self.assertRaises(RateDeferred) as caught:self.limiter.acquire('zhilian','collect',self.policy)
        self.assertEqual(caught.exception.code,'outside_hours')
    def test_risk_pause_shared_and_explicit_only(self):
        from engine.rate import RateDeferred
        self.repo.pause('boss','risk_control')
        self.now+=86400*30
        with self.assertRaises(RateDeferred) as caught:self.limiter.acquire('boss','collect',self.policy)
        self.assertEqual(caught.exception.code,'risk_control')
        self.assertIn('boss',self.repo.pauses())
    def test_legacy_fields_accepted(self):
        from engine.config import PlatformSettings
        from collect.config import CollectSettings
        self.assertEqual(PlatformSettings(interval_seconds=30,daily_limit=200).rate_policy().daily_limit,200)
        self.assertEqual(CollectSettings(interval_seconds=[8,15]).rate_policy('boss').interval_seconds,(8,15))

class RateBoundaryTest(RateTest):
    def test_hourly_boundary_is_rolling_and_failed_reservations_count(self):
        from engine.rate import RateDeferred
        self.policy.rest_every=(100,100)
        self.limiter.acquire('boss','collect',self.policy)
        first=self.now
        self.limiter.acquire('boss','collect',self.policy)
        self.now=first+3599.999
        with self.assertRaises(RateDeferred):self.limiter.acquire('boss','collect',self.policy)
        self.now=first+3600
        self.limiter.acquire('boss','collect',self.policy)
    def test_window_open_close_overnight_timezone_and_midnight(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from engine.rate import RatePolicy
        policy=RatePolicy()
        ts=lambda s:datetime.fromisoformat(s).replace(tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
        for s,allowed in [('2026-11-26T08:59:59',False),('2026-11-26T09:00:00',True),('2026-11-26T20:59:59',True),('2026-11-26T21:00:00',False)]:
            self.assertEqual(policy.allowed(ts(s)),allowed)
        policy.allowed_hours=('21:00','09:00')
        self.assertTrue(policy.allowed(ts('2026-11-27T00:00:00')))
        self.assertFalse(policy.allowed(ts('2026-11-27T09:00:00')))
    def test_interval_wait_rechecks_window_and_pause_and_stop(self):
        from engine.rate import RateDeferred
        self.policy.rest_every=(100,100)
        self.now=1795697999.0 # 20:59:59
        self.limiter.acquire('boss','collect',self.policy)
        with self.assertRaises(RateDeferred) as caught:self.limiter.acquire('boss','collect',self.policy)
        self.assertEqual(caught.exception.code,'outside_hours')
    def test_zero_limits_and_invalid_config(self):
        from engine.rate import RateDeferred,RatePolicy
        from browser.input_config import InputSettings
        self.policy.hourly_limit=0
        with self.assertRaises(RateDeferred):self.limiter.acquire('boss','collect',self.policy)
        for values in [dict(interval_seconds=(-1,2)),dict(rest_every=(0,1)),dict(rest_seconds=(float('nan'),1)),dict(allowed_hours=('25:00','21:00'))]:
            with self.assertRaises(ValueError):RatePolicy(**values)
        for values in [dict(shrink=0),dict(duration_ms=(500,250)),dict(steps=(1,1))]:
            with self.assertRaises(ValueError):InputSettings(**values)
    def test_legacy_attempts_seed_budget_without_double_counting_new_reservations(self):
        self.policy.hourly_limit=3
        self.policy.rest_every=(100,100)
        history=[self.now-300,self.now-200]
        self.limiter.acquire('boss','delivery',self.policy,history=history)
        history.append(self.now+.1)
        from engine.rate import RateDeferred
        with self.assertRaises(RateDeferred):self.limiter.acquire('boss','delivery',self.policy,history=history)

class SerializationTest(unittest.TestCase):
    def test_frozen_batch_config_and_effective_rate_are_stable(self):
        from engine.config import EngineSettings
        from store.delivery import config_hash
        for data in [dict(),{'platforms':{'boss':{'rate':{'hourly_limit':10}}}},
                     {'platforms':{'boss':{'interval_seconds':30,'rate':{'hourly_limit':10}}}}]:
            first=EngineSettings(**data)
            second=EngineSettings.model_validate(first.model_dump())
            self.assertEqual(config_hash(first.model_dump()),config_hash(second.model_dump()))
            self.assertEqual(first.platforms['boss'].rate_policy(),second.platforms['boss'].rate_policy())
        self.assertEqual(EngineSettings().platforms['boss'].rate_policy().interval_seconds,(45,120))
    def test_collection_serialization_keeps_default_and_legacy_rates(self):
        from collect.config import CollectSettings
        for data in [{},{'interval_seconds':[8,15]},{'platforms':{'boss':{'rate':{'hourly_limit':10}}}}]:
            first=CollectSettings(**data)
            second=CollectSettings.model_validate(first.model_dump())
            self.assertEqual(first.rate_policy('boss'),second.rate_policy('boss'))

class SharedPacingTest(RateTest):
    def test_collect_and_delivery_share_interval_and_rest_but_not_daily_quota(self):
        self.policy.hourly_limit=20
        self.limiter.acquire('boss','collect',self.policy)
        first=self.now
        self.limiter.acquire('boss','delivery',self.policy)
        self.assertTrue(45<=self.now-first<=120)
        second=self.now
        self.limiter.acquire('boss','collect',self.policy)
        self.assertGreaterEqual(self.now-second,300)
        self.assertEqual(len(self.repo.operations('boss','delivery')),1)
        self.assertEqual(len(self.repo.operations('boss','collect')),2)
