"""Virtual-list fixtures reproduce missing/recycled rows without personal data."""
import json
import subprocess
import unittest
from job_agent.platforms.boss import pages


class VirtualListTests(unittest.TestCase):
    def test_messages_use_job_name_instead_of_recruiter_title(self):
        from job_agent.platforms.reconciliation import BOSS_MESSAGES_JS
        setup='''const source={encryptJobId:'job',uniqueId:'session',jobName:'工程师',title:'招聘者',brandName:'公司'};
        const selected={closest:()=>({__vue__:{$props:{source}}})};
        const item={getAttribute:()=> 'message',classList:{contains:s=>s==='item-myself'},querySelector:()=>({textContent:'送达'}),
            __vue__:{$props:{message:{time:1795675283743}}}};
        global.document={querySelector:()=>selected,querySelectorAll:()=>[item]};'''
        script=setup+'\nconsole.log(JSON.stringify(('+BOSS_MESSAGES_JS+')()));'
        data=json.loads(subprocess.check_output(['node'],input=script,text=True,timeout=5))
        self.assertEqual(data['rows'][0]['job_title'],'工程师')

    def test_reconciliation_projection_waits_for_recycled_row_and_stable_geometry(self):
        from job_agent.platforms.boss.reconciliation import select_existing_session
        setup='''let tick=0,scrolled=false;
        global.setTimeout=f=>{tick++;queueMicrotask(f)};
        global.innerWidth=800;global.innerHeight=600;
        const target={scrollIntoView:()=>{scrolled=true},contains:()=>false,
          getBoundingClientRect:()=>({x:10,y:scrolled?100:1500,right:200,bottom:scrolled?150:1550,width:190,height:50})};
        const row={__vue__:{$props:{source:{encryptJobId:'job',uniqueId:'session'}}},querySelector:()=>target};
        global.document={querySelectorAll:()=>scrolled&&tick<2?[]:[row],elementFromPoint:()=>target};
        '''
        script=setup+'\nconst result=await ('+select_existing_session({'encryptJobId':'job','uniqueId':'session'})+')();console.log(JSON.stringify({result,tick}));'
        result=json.loads(subprocess.check_output(['node','--input-type=module'],input=script,text=True,timeout=5))
        self.assertEqual(result['result']['y'],100)
        self.assertGreaterEqual(result['tick'],4)

    def test_reconciliation_projection_never_selects_obscured_or_duplicate_rows(self):
        from job_agent.platforms.boss.reconciliation import select_existing_session
        for duplicate in (False,True):
            setup='''global.setTimeout=f=>queueMicrotask(f);global.innerWidth=800;global.innerHeight=600;
            const target={getBoundingClientRect:()=>({x:10,y:100,right:200,bottom:150,width:190,height:50}),contains:()=>false};
            const row={__vue__:{$props:{source:{encryptJobId:'job',uniqueId:'session'}}},querySelector:()=>target};
            global.document={querySelectorAll:()=>ROWS,elementFromPoint:()=>({})};'''.replace('ROWS','[row,row]' if duplicate else '[row]')
            script=setup+'\nconsole.log(JSON.stringify(await ('+select_existing_session({'encryptJobId':'job','uniqueId':'session'})+')()));'
            self.assertIs(json.loads(subprocess.check_output(['node','--input-type=module'],input=script,text=True,timeout=5)),False)

    def test_scan_does_not_overwrite_visible_positions_with_offscreen_mounted_rows(self):
        from job_agent.platforms.boss.reconciliation import SCAN_JS
        setup='''global.setTimeout=f=>queueMicrotask(f);global.innerHeight=200;global.innerWidth=800;
        const container={scrollTop:0,scrollHeight:300,clientHeight:100,getBoundingClientRect:()=>({top:0,bottom:100})};
        const rows=[0,1,2].map(i=>({__vue__:{$props:{source:{encryptJobId:'job'+i,uniqueId:'session'+i,title:'Role',brandName:'Company'}}},
           getBoundingClientRect:()=>({top:i*100-container.scrollTop,bottom:(i+1)*100-container.scrollTop})}));
        global.document={querySelector:()=>container,querySelectorAll:s=>s==='[role=listitem]'?rows:[]};
        const state=()=>({});
        '''
        script=setup+'\nconst result=await ('+SCAN_JS.replace('STATE_PROJECTION','() => ({})')+')();console.log(JSON.stringify(result));'
        data=json.loads(subprocess.check_output(['node','--input-type=module'],input=script,text=True,timeout=5))
        self.assertEqual(len(data['entries']),3)
        positions={e['row']['encryptJobId']:e['position'] for e in data['entries']}
        self.assertEqual(positions['job0'],80)
        self.assertEqual(positions['job2'],200)
        self.assertTrue(data['complete'])
