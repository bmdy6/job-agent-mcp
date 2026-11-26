"""Safety regression: newline is never typed as a possible send key."""
import json
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from browser.ego import EgoBrowser, _SCRIPT
from browser.input_config import InputSettings
from test_ego_browser import result
import test_adapters_boss_zhilian as boss_tests
import test_adapters_b as other_tests


class NewlineInputTests(unittest.TestCase):
    def run_input(self, text, mode=None):
        request = dict(op='page', space_id=1, label='p2', anchor='p1', method='fill',
                       args=['#input', text], previous=[10,20], seed=42,
                       input=InputSettings().model_dump())
        if mode is not None:
            request['args'].append({'newline':mode})
        setup = r'''
const actions=[];
globalThis.setTimeout=fn=>{fn();return 0;};
const page={waitForSelector:async()=>{},evaluate:async(js,args)=>{
 if(typeof args==='string')return {x:100,y:100,width:80,height:30,viewport:{width:800,height:600}};
 return typeof args==='object';
},mouse:{move:async()=>actions.push(['move']),down:async()=>{},up:async()=>{}},
keyboard:{press:async k=>actions.push(['key',k]),type:async c=>actions.push(['type',c]),
 insertText:async c=>actions.push(['insert',c])}};
globalThis.listTaskSpaces=async()=>[{id:1,ownership:'agent'}];
globalThis.taskSpace=async()=>({ownership:'agent',tabs:async()=>[{label:'p2',openedBy:'agent'}],page:()=>page});
'''
        output=subprocess.check_output(['node','--input-type=module'],
            input='const request='+json.dumps(request)+';\n'+setup+_SCRIPT+
            '\nconsole.log("ACTIONS"+JSON.stringify(actions));',text=True,timeout=5)
        payload=json.loads(next(x.removeprefix('__JOB_AGENT_RESULT__') for x in output.splitlines() if x.startswith('__JOB_AGENT_RESULT__')))
        actions=json.loads(next(x.removeprefix('ACTIONS') for x in output.splitlines() if x.startswith('ACTIONS')))
        return payload,actions

    def test_platform_newline_never_reaches_type_or_plain_enter(self):
        for mode, expected in [('shift_enter',['key','Shift+Enter']),('insert_text',['insert','\n'])]:
            with self.subTest(mode=mode):
                payload,actions=self.run_input('甲\n乙\n丙',mode)
                self.assertTrue(payload['ok'],payload)
                self.assertEqual([a for a in actions if a[0] in {'type','key','insert'}],
                    [['key','ControlOrMeta+A'],['key','Backspace'],['type','甲'],expected,
                     ['type','乙'],expected,['type','丙']])

    def test_undeclared_or_unsafe_mode_rejects_before_any_input(self):
        for text,mode in [('甲\n乙',None),('甲\r乙',None),('甲\n乙','Enter'),('甲\u2028乙',None)]:
            payload,actions=self.run_input(text,mode)
            self.assertFalse(payload['ok'])
            self.assertEqual(actions,[])

    def test_python_fill_passes_explicit_rule_and_rejects_default(self):
        from browser.base import BrowserError
        with patch('browser.ego.subprocess.run') as run:
            run.return_value=result({'space_id':1,'anchor':'p1'})
            browser=EgoBrowser(keepalive_seconds=0)
            space=browser.space('test')
            run.return_value=result({'label':'p2'})
            page=space.new_page()
            before=run.call_count
            with self.assertRaises(BrowserError):page.fill('#input','甲\n乙')
            self.assertEqual(run.call_count,before)
            run.return_value=result({})
            page.fill('#input','甲\n乙',newline='shift_enter')
            request=json.loads(run.call_args.kwargs['input'].split('const request = ',1)[1].split(';\n',1)[0])
            self.assertEqual(request['args'][2],{'newline':'shift_enter'})


class GreetingContractTests(unittest.TestCase):
    def test_preview_engine_and_stored_history_use_identical_effective_text(self):
        import tempfile
        from pathlib import Path
        from config import Settings
        from engine.models import Job
        from engine.adapters import FakeAdapter
        from engine.preflight import FakePreflight
        from engine.spaces import FakeTaskSpaces
        from engine.queue import DeliveryEngine
        from service.deliver import preview
        from store import SQLiteDeliveryStore
        from platforms.greeting import greeting_version
        for platform,expected in [('boss','甲\n乙'),('liepin','甲；乙')]:
            with self.subTest(platform=platform), tempfile.TemporaryDirectory() as temp:
                cfg=Settings(engine={'platforms':{platform:dict(enabled=True,greeting='甲\n乙',greeting_max_length=10)}})
                with SQLiteDeliveryStore(Path(temp)/'db.sqlite3') as store:
                    store.upsert_job(platform=platform,platform_job_id='one',url='https://example.invalid/one')
                    batch=json.loads(preview(store,cfg))
                    self.assertEqual(batch['greetings'][platform],expected)
                    self.assertEqual(batch['greeting_versions'][platform],greeting_version(expected))
                    store.confirm_batch(batch['token'],config=cfg.engine.model_dump())
                    changed=cfg.engine.model_dump()
                    changed['platforms'][platform]['greeting_newline_separator']=' '
                    with self.assertRaises(ValueError):
                        store.prepare_run(changed,batch_token=batch['token'])
                    adapter=FakeAdapter()
                    engine=DeliveryEngine(store,cfg.engine,{platform:adapter},FakePreflight(),FakeTaskSpaces(),sleep=lambda _:None)
                    engine.run(batch_token=batch['token'])
                    self.assertEqual(adapter.greetings,[expected])
                    self.assertTrue(all(r['greeting']==expected for r in store.list_deliveries()))

    def test_platform_transform_and_content_version(self):
        from platforms.greeting import prepare_greeting, greeting_version
        text='甲\r\n乙\r丙\u2028丁\u2029戊'
        self.assertEqual(prepare_greeting('boss',text),'甲\n乙\n丙\n丁\n戊')
        for p in ('liepin','zhilian','51job'):
            self.assertEqual(prepare_greeting(p,text),'甲；乙；丙；丁；戊')
            self.assertEqual(prepare_greeting(p,text,' '),'甲 乙 丙 丁 戊')
        self.assertEqual(greeting_version('甲\n乙'),greeting_version('甲\n乙'))
        self.assertNotEqual(greeting_version('甲\n乙'),greeting_version('甲乙'))
        from engine.config import PlatformSettings
        with self.assertRaises(ValueError):PlatformSettings(greeting_newline_separator='\n')

    def test_boss_chat_and_modal_keep_full_newline_and_block_bad_readback(self):
        fixture=boss_tests.Adapters()
        for readback in ('甲\n乙','甲乙','甲\n乙\n'):
            a,p=fixture.make(fixture.cases[0])
            p.responses['#chat-input']=readback
            if readback=='甲\n乙':
                a.send_greeting('甲\n乙')
                self.assertIn(('fill',boss_tests.boss.INPUT,'甲\n乙','shift_enter'),p.actions)
                self.assertIn(('press',boss_tests.boss.SEND),p.actions)
            else:
                with self.assertRaises(boss_tests.boss.PlatformStateError):a.send_greeting('甲\n乙')
                self.assertFalse(any(x[0]=='press' for x in p.actions))
        for readback in ('甲\n乙','甲乙'):
            a,p,state=fixture.editable(receipt=False)
            def fill(selector,text,**kwargs):
                p.actions.append(('fill',selector,text,kwargs.get('newline')))
                state['value']=readback
            p.fill=fill
            if readback=='甲乙':
                with self.assertRaises(boss_tests.boss.PlatformStateError):a.send_greeting('甲\n乙')
                self.assertEqual([x for x in p.actions if x[0]=='click'],[('click',boss_tests.boss.START)])
            else:
                a.send_greeting('甲\n乙')
                self.assertIn(('fill',boss_tests.boss.CONTACT_INPUT,'甲\n乙','insert_text'),p.actions)

    def test_liepin_fallback_full_readback_before_send(self):
        for separator in ('；',' '):
            for exact in (True,False):
                expected='甲'+separator+'乙'
                a,p=other_tests.setup(other_tests.LiepinAdapter,{'liepin.guard':{},
                    'liepin.job':other_tests.fixture('liepin','unapplied-job'),
                    'liepin.messages':dict(ready=True,items=[]),'liepin.editor':expected if exact else '甲'})
                a.settings=SimpleNamespace(greeting_max_length=800,greeting_newline_separator=separator)
                if exact:
                    a.send_greeting('甲\n乙')
                    self.assertIn(('fill',other_tests.lp.EDITOR,expected),p.actions)
                else:
                    with self.assertRaises(other_tests.lp.Unverified):a.send_greeting('甲\n乙')
                    self.assertFalse(any(x[0]=='press' for x in p.actions))
