"""Exercise the shipped Node gesture script with an offline Ego API double."""
import json
import subprocess
import unittest
from unittest.mock import patch
from browser.ego import EgoBrowser,_SCRIPT
from browser.input_config import InputSettings
from test_ego_browser import result


class GestureTest(unittest.TestCase):
    def test_full_move_down_up_and_each_character_in_one_node_call(self):
        setup=r'''
const actions=[];
const request={op:"page",space_id:1,label:"p2",anchor:"p1",method:"fill",args:["#input","你好ab"],previous:[10,20],seed:42,
 input:CONFIG};
globalThis.setTimeout=(fn)=>{fn();return 0;};
const page={waitForSelector:async s=>actions.push(["wait",s]),
 evaluate:async (js,args)=>{
   if(typeof js==="function"&&typeof args==="string")return {x:100,y:100,width:80,height:30,viewport:{width:800,height:600}};
   if(typeof args==="object")return true;
   return false;
 },mouse:{move:async (x,y)=>actions.push(["move",x,y]),down:async()=>actions.push(["down"]),up:async()=>actions.push(["up"])},
 keyboard:{press:async k=>actions.push(["key",k]),type:async c=>actions.push(["type",c])}};
globalThis.listTaskSpaces=async()=>[{id:1,name:"test",ownership:"agent"}];
globalThis.taskSpace=async()=>({ownership:"agent",tabs:async()=>[{label:"p2",openedBy:"agent"}],page:()=>page});
'''.replace('CONFIG',json.dumps(InputSettings().model_dump()))
        p=subprocess.run(['node','--input-type=module'],input=setup+_SCRIPT+'\nconsole.log("ACTIONS"+JSON.stringify(actions));',text=True,capture_output=True,timeout=5)
        self.assertEqual(p.returncode,0,p.stderr)
        payload=json.loads(next(s.removeprefix('__JOB_AGENT_RESULT__') for s in p.stdout.splitlines() if s.startswith('__JOB_AGENT_RESULT__')))
        self.assertTrue(payload['ok'],payload)
        actions=json.loads(next(s.removeprefix('ACTIONS') for s in p.stdout.splitlines() if s.startswith('ACTIONS')))
        moves=[a for a in actions if a[0]=='move']
        self.assertTrue(5 <= len(moves) <= 8)
        self.assertEqual(moves[0],['move',10,20])
        self.assertEqual([a for a in actions if a[0] in {'down','up'}],[['down'],['up']])
        self.assertEqual([a[1] for a in actions if a[0]=='type'],list('你好ab'))
        self.assertEqual(len(payload['value']['trace']),len(moves))
        self.assertNotIn('dispatchEvent',_SCRIPT)
        self.assertNotIn('target.click()',_SCRIPT)

    def test_previous_position_reused_and_sensitive_text_never_logged(self):
        with patch('browser.ego.subprocess.run') as run:
            run.return_value=result({'space_id':1,'anchor':'p1'})
            b=EgoBrowser(keepalive_seconds=0);space=b.space('test')
            run.return_value=result({'label':'p2'});page=space.new_page()
            run.return_value=result({'mouse':[123,234],'trace':[{'at':1,'x':123,'y':234}]})
            before=run.call_count
            with self.assertLogs('browser.ego',level='DEBUG') as logged:
                page.fill('#input','PRIVATE_BODY')
                page.hover('#input')
            self.assertEqual(run.call_count-before,2)
            self.assertNotIn('PRIVATE_BODY',' '.join(logged.output))
            request=json.loads(run.call_args.kwargs['input'].split('const request = ',1)[1].split(';\n',1)[0])
            self.assertEqual(request['previous'],[123,234])

    def test_risk_prevents_all_pointer_and_keyboard_actions(self):
        # The driver decodes risk separately, for safety persistence in callers.
        from browser.base import RiskControl
        with patch('browser.ego.subprocess.run',return_value=result(code='risk_control')):
            with self.assertRaises(RiskControl):EgoBrowser(keepalive_seconds=0)._call({'op':'space','name':'test'})
