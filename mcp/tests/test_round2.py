import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from job_agent.config.engine import EngineSettings
from job_agent.browser.base import FakeBrowser
from job_agent.platforms.session import check_session

import test_humanize_rate as rate_tests
import test_engine as engine_tests
import test_collect as collect_tests
from test_collect import Source


class AccountTest(unittest.TestCase):
    def test_explicit_type_only(self):
        browser=FakeBrowser()
        # Use the same proven fake projection as the existing session suite.
        from unittest.mock import Mock
        page=Mock()
        page.evaluate.return_value=dict(logged_in=True,account_type='student',expected_host=True)
        browser=Mock(); browser.space.return_value.new_page.return_value=page
        self.assertTrue(check_session(browser,'liepin').ok)
        self.assertTrue(check_session(browser,'liepin',expected_account_type='student').ok)
        self.assertEqual(check_session(browser,'liepin',expected_account_type='professional').reason,'account_type')

    def test_defaults_preserve_override(self):
        cfg=EngineSettings()
        self.assertEqual(cfg.platforms['boss'].daily_limit,140)
        self.assertEqual(cfg.platforms['zhilian'].daily_limit,90)
        self.assertEqual(cfg.platforms['liepin'].daily_limit,1)
        self.assertEqual(EngineSettings(platforms={'boss':{'daily_limit':200}}).platforms['boss'].daily_limit,200)


class NonblockingRateTest(unittest.TestCase):
    setUp=rate_tests.RateTest.setUp
    sleep=rate_tests.RateTest.sleep
    def test_cooling_platform_does_not_wait(self):
        from job_agent.engine.rate import RateDeferred
        self.limiter.acquire('boss','collect',self.policy,block=False)
        now=self.now
        with self.assertRaises(RateDeferred) as caught:
            self.limiter.acquire('boss','collect',self.policy,block=False)
        self.assertEqual(caught.exception.code,'cooldown')
        self.assertEqual(self.now,now)
        self.limiter.acquire('zhilian','collect',self.policy,block=False)
        self.assertEqual(self.now,now)


class RotationTest(unittest.TestCase):
    setUp=engine_tests.EngineTest.setUp
    make_engine=engine_tests.EngineTest.make_engine
    jobs=engine_tests.EngineTest.jobs
    def test_rounds_shuffle_platforms_and_one_job_each(self):
        from job_agent.engine.models import Job
        import random
        self.engine.enqueue(self.jobs(4)+[Job('zhilian',str(i),'https://example.invalid/'+str(i)) for i in range(4)])
        self.engine.random=random.Random(3)
        names=[r.job.platform for r in self.engine._rounds(lambda:False)]
        for i in range(0,8,2): self.assertEqual(set(names[i:i+2]),{'boss','zhilian'})
        self.assertGreater(len({tuple(names[i:i+2]) for i in range(0,8,2)}),1)

    def test_delivery_rotates_before_waiting_and_keeps_platform_interval(self):
        import random
        from job_agent.config.engine import PlatformSettings
        from job_agent.engine.adapters import FakeAdapter
        from job_agent.engine.models import Job
        self.cfg.platforms['boss'].interval_seconds=45
        self.cfg.platforms['zhilian']=PlatformSettings(enabled=True,greeting='您好',greeting_max_length=10,daily_limit=10,interval_seconds=45)
        other=FakeAdapter()
        self.engine.adapters['zhilian']=other
        events=[]
        for name, adapter in self.engine.adapters.items():
            original=adapter.open_job
            def traced(job,space,original=original,name=name):
                events.append((name,self.now))
                return original(job,space)
            adapter.open_job=traced
        self.engine.sleep=lambda d:setattr(self,'now',self.now+d)
        self.engine.random=random.Random(3)
        self.engine.enqueue(self.jobs(2)+[Job('zhilian',str(i),'https://example.invalid/'+str(i)) for i in range(2)])
        self.engine.run()
        self.assertEqual(set(p for p,_ in events[:2]),{'boss','zhilian'})
        self.assertEqual(events[0][1],events[1][1])
        for platform in ('boss','zhilian'):
            times=[t for p,t in events if p==platform]
            self.assertGreaterEqual(times[1]-times[0],45)

    def test_new_batch_isolated_from_old_pending_configuration(self):
        self.engine.enqueue(self.jobs(2))
        jobs=self.jobs(2)
        original=self.cfg.model_dump()
        old=self.store.create_batch([jobs[0]],{'boss':'您好'},'resume',original)
        self.store.confirm_batch(old,config=original)
        newer=self.cfg.model_copy(deep=True)
        newer.platforms['boss'].daily_limit=20
        config=newer.model_dump()
        token=self.store.create_batch([jobs[1]],{'boss':'您好'},'resume',config)
        self.store.confirm_batch(token,config=config)
        with self.assertRaises(ValueError): self.store.prepare_run(config)
        self.store.prepare_run(config,batch_token=token)



class CollectRotationTest(unittest.TestCase):
    setUp=collect_tests.CollectTest.setUp
    def test_real_limiter_rotation_spacing_and_pause(self):
        from job_agent.engine.rate import RateLimiter
        from job_agent.store.automation import AutomationRepository
        from job_agent.collect.runner import Collector
        from job_agent.service.safety import PlatformSafety
        from job_agent.platforms.session import SessionResult
        from job_agent.config import Settings
        from datetime import datetime
        from zoneinfo import ZoneInfo
        self.clock[0]=datetime(2026,11,26,10,tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
        events=[]
        class TracedSource(Source):
            def search(inner,space,query):
                events.append((inner.platform,self.clock[0],'page'))
                return super().search(space,query)
            def detail(inner,space,listing):
                events.append((inner.platform,self.clock[0],'detail'))
                return super().detail(space,listing)
        def sleep(d): self.clock[0]+=d
        repo=AutomationRepository(self.store)
        limiter=RateLimiter(repo,clock=lambda:self.clock[0],sleep=sleep,seed=2)
        # Keep paused source present to verify safety excludes it before search.
        from job_agent.config.collect import PlatformSettings
        self.settings.platforms['51job']=PlatformSettings(enabled=True)
        repo.pause('51job','risk_control')
        safety=PlatformSafety(self.browser,self.store,Settings(),check=lambda b,p,s:SessionResult(p,True,'',True),alert=lambda *a:None)
        runner=Collector(self.browser,[TracedSource('boss'),TracedSource('liepin'),TracedSource('51job')],self.repo,self.settings,clock=lambda:self.clock[0],sleep=sleep,limiter=limiter,safety=safety,seed=4)
        runner.run()
        self.assertNotIn('51job',[p for p,_,_ in events])
        self.assertEqual(set(p for p,_,_ in events[:2]),{'boss','liepin'})
        for platform in ('boss','liepin'):
            times=[t for p,t,_ in events if p==platform]
            self.assertTrue(all(b-a>=45 for a,b in zip(times,times[1:])))


class HumanDetailTest(unittest.TestCase):
    def test_seeded_hesitation_backtracks(self):
        from job_agent.config.input import InputSettings
        cfg=InputSettings(hesitation_probability=1).model_dump()
        js=(Path(__file__).resolve().parents[1]/"job_agent"/"browser"/"humanize.js").read_text()+'''\nconst cfg=JSON.parse(process.argv[1]);const a=trajectory({x:500,y:300,width:100,height:50},{width:1000,height:800},[10,10],cfg,42); console.log(JSON.stringify(a));'''
        args=['node','--input-type=module','-e',js,json.dumps(cfg)]
        a=json.loads(subprocess.check_output(args,text=True));b=json.loads(subprocess.check_output(args,text=True))
        self.assertEqual(a,b)
        self.assertTrue(any(p['delay_ms']>=100 for p in a['points']))

    def test_detail_wait_and_scrolls_seeded(self):
        from job_agent.browser.ego import _SCRIPT
        from job_agent.config.input import InputSettings
        setup='''
let now=0;const actions=[];
Date.now=()=>now;
globalThis.setTimeout=fn=>fn();
const request={op:'page',space_id:1,label:'p2',anchor:'p1',method:'read_detail',input:CONFIG,seed:42};
globalThis.setTimeout=(fn,ms)=>{now+=ms;fn();};
const page={mouse:{move:async()=>{},wheel:async(x,y)=>actions.push([x,y])}};
globalThis.listTaskSpaces=async()=>[{id:1,ownership:'agent'}];
globalThis.taskSpace=async()=>({ownership:'agent',tabs:async()=>[{label:'p2',openedBy:'agent'}],page:()=>page});
'''.replace('CONFIG',json.dumps(InputSettings().model_dump()))
        p=subprocess.run(['node','--input-type=module'],input=setup+_SCRIPT,text=True,capture_output=True,check=True)
        payload=json.loads(p.stdout.split('__JOB_AGENT_RESULT__')[1])
        self.assertTrue(payload['ok'])
        self.assertTrue(3000<=payload['value']['duration_ms']<=12000)
        self.assertTrue(1<=payload['value']['scrolls']<=3)
