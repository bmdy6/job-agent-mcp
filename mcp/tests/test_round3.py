import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from job_agent.browser.ego import EgoBrowser, _SCRIPT
from job_agent.config.input import InputSettings
from test_ego_browser import result


class SparseInputTests(unittest.TestCase):
    def test_sparse_special_points_stay_within_configured_count(self):
        cfg = InputSettings(hesitation_probability=1, overshoot_probability=1).model_dump()
        script = (Path(__file__).resolve().parents[1]/"job_agent"/"browser"/"humanize.js").read_text() + '''
const cfg=JSON.parse(process.argv[1]);
console.log(JSON.stringify(Array.from({length:100},(_,seed)=>trajectory(
 {x:600,y:300,width:100,height:50},{width:1000,height:800},[10,10],cfg,seed))));
'''
        paths = json.loads(subprocess.check_output(['node','--input-type=module','-e',script,json.dumps(cfg)],text=True))
        for plan in paths:
            self.assertTrue(5 <= len(plan['points']) <= 8)
            kinds = [p.get('kind') for p in plan['points']]
            self.assertEqual(kinds.count('overshoot'),1)
            self.assertEqual(kinds.count('hesitation'),1)
            self.assertEqual([plan['points'][-1][k] for k in ('x','y')],plan['end'])

    def test_slow_mouse_budget_is_added_to_page_timeout_and_rolling_samples(self):
        cfg=InputSettings(mouse_sample_window=2)
        with patch('job_agent.browser.ego.subprocess.run') as run:
            run.return_value=result({'space_id':1,'anchor':'p1'})
            browser=EgoBrowser(timeout_s=30,keepalive_seconds=0,human_input=cfg)
            space=browser.space('test')
            run.return_value=result({'label':'p2'})
            page=space.new_page()
            run.return_value=result({'mouse':[100,100], 'trace':[], 'gesture':{
                'planned_points':8,'duration_ms':24000,'move_ms':[900,1500,3000]}})
            with self.assertLogs('job_agent.browser.ego',level='INFO') as logs:
                page.hover('#input')
            first=run.call_args.kwargs['timeout']
            self.assertGreater(first,30+8*1.1)
            self.assertIn('planned_points=8',' '.join(logs.output))
            self.assertEqual(browser.mouse_point_ms,2250)
            run.return_value=result({'mouse':[100,100],'trace':[]})
            page.hover('#input')
            self.assertGreater(run.call_args.kwargs['timeout'],first)
            request=json.loads(run.call_args.kwargs['input'].split(';\n',1)[0].removeprefix('const request = '))
            self.assertEqual(request['mouse_point_ms'],2250)

    def test_shipped_script_measures_slow_calls_and_keeps_per_character_delays(self):
        cfg=InputSettings(long_pause_probability=0).model_dump()
        setup=r"""
let now=0;const typed=[],delays=[],moves=[];Date.now=()=>now;
globalThis.setTimeout=(fn,ms)=>{now+=ms;delays.push(ms);fn();};
const request={op:'page',space_id:1,label:'p2',anchor:'p1',method:'fill',
 args:['#input','你好ab'],seed:42,previous:[10,20],input:CONFIG};
const page={waitForSelector:async(s,opts)=>{if(opts.timeout!==5000)throw Error('wait changed');},
 evaluate:async(js,args)=>{
  if(typeof args==='string')return {x:200,y:200,width:100,height:50,viewport:{width:1000,height:800}};
  if(typeof args==='object')return true;
  return false;
 },mouse:{move:async(x,y)=>{now+=1000;moves.push([x,y]);},down:async()=>{now+=1000;},up:async()=>{now+=1000;}},
 keyboard:{press:async()=>{},type:async(c)=>typed.push(c)}};
globalThis.listTaskSpaces=async()=>[{id:1,ownership:'agent'}];
globalThis.taskSpace=async()=>({ownership:'agent',tabs:async()=>[{label:'p2',openedBy:'agent'}],page:()=>page});
""".replace('CONFIG',json.dumps(cfg))
        output=subprocess.check_output(['node','--input-type=module'],input=setup+_SCRIPT+
            "\nconsole.log('TYPED'+JSON.stringify({typed,delays}));",text=True)
        value=json.loads(next(line.removeprefix('__JOB_AGENT_RESULT__') for line in output.splitlines()
                             if line.startswith('__JOB_AGENT_RESULT__')))['value']
        timing=value['gesture']
        self.assertTrue(5 <= timing['planned_points'] <= 8)
        self.assertEqual(timing['move_ms'],[1000]*timing['planned_points'])
        self.assertGreaterEqual(timing['duration_ms'],(timing['planned_points']+2)*1000)
        self.assertLess(timing['duration_ms'],timing['budget_ms'])
        typed=json.loads(next(line.removeprefix('TYPED') for line in output.splitlines() if line.startswith('TYPED')))
        self.assertEqual(typed['typed'],list('你好ab'))
        self.assertTrue(all(60 <= ms <= 180 for ms in typed['delays'][-4:]))

    def test_transport_timeout_retains_completed_progress_and_never_replays(self):
        from job_agent.browser.base import PageTimeout
        payload={'gesture':{'planned_points':8,'duration_ms':2100,'move_ms':[1000,1100]}}
        partial='__JOB_AGENT_GESTURE__'+json.dumps(payload)+'\n'
        with patch('job_agent.browser.ego.subprocess.run',side_effect=subprocess.TimeoutExpired('ego',60,output=partial)) as run:
            b=EgoBrowser(keepalive_seconds=0)
            with self.assertLogs('job_agent.browser.ego',level='INFO') as logs:
                with self.assertRaises(PageTimeout):b._call({'method':'hover'})
            self.assertEqual(run.call_count,1)
            self.assertEqual(b.mouse_point_ms,1050)
            self.assertIn('planned_points=8',' '.join(logs.output))
            self.assertIn('isTrusted=unknown',' '.join(logs.output))

    def test_failed_gesture_keeps_timing_samples(self):
        from job_agent.browser.base import BrowserError
        payload={'ok':False,'code':'browser_error','trace':[], 'gesture':{
            'planned_points':6,'duration_ms':2200,'move_ms':[1100,1100]}}
        response=result(None)
        response.stdout='__JOB_AGENT_RESULT__'+json.dumps(payload)
        with patch('job_agent.browser.ego.subprocess.run',return_value=response):
            b=EgoBrowser(keepalive_seconds=0)
            with self.assertLogs('job_agent.browser.ego',level='INFO'):
                with self.assertRaises(BrowserError):b._call({'method':'hover'})
        self.assertEqual(b.mouse_point_ms,1100)


class Unverified704Test(unittest.TestCase):
    def test_704_blocks_preview_and_confirm_even_if_queue_is_failed(self):
        from job_agent.config import Settings
        from job_agent.engine.models import Job
        from job_agent.service.deliver import preview
        from job_agent.store import SQLiteDeliveryStore
        with tempfile.TemporaryDirectory() as tmp:
            with SQLiteDeliveryStore(Path(tmp)/'queue.sqlite') as db:
                cfg=Settings(engine={'platforms':{'zhilian':{'enabled':True,'greeting_max_length':500,'greeting_mode':'platform_default'}}})
                db.connection.execute("INSERT INTO jobs(id,platform,platform_job_id,url,collected_at) VALUES(704,'zhilian','704','https://example.com/704','2026-11-26')")
                job=Job('zhilian','704','https://example.com/704')
                token=db.create_batch([job],{'zhilian':''},'',cfg.engine.model_dump())
                db.record_delivery(704,status='unverified',reason_code='effect_error')
                db.connection.execute("INSERT INTO delivery_queue(job_id,status) VALUES(704,'failed')")
                with self.assertRaises(ValueError):preview(db,cfg,job_ids=[704])
                with self.assertRaises(ValueError):db.confirm_batch(token,config=cfg.engine.model_dump())
                self.assertEqual(db.get_batch(token)['status'],'pending')
                self.assertEqual(db.list_deliveries()[0]['status'],'unverified')
                # The MCP facade must reject the same ID before issuing another token.
                import os
                from job_agent.service.mcp_backend import preview as mcp_preview
                with patch.dict(os.environ,JOB_AGENT_HOME=tmp), patch('job_agent.service.mcp_backend.load_config',return_value=cfg):
                    # Facade uses the default filename in this isolated HOME.
                    with patch('job_agent.service.mcp_backend.SQLiteDeliveryStore',return_value=db):
                        with self.assertRaises(ValueError):mcp_preview(['704'])
