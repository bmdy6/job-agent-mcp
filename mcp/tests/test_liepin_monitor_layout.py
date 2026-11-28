import json
from pathlib import Path
import subprocess
import unittest
from job_agent.platforms.liepin import pages

FIXTURE=Path(__file__).parent/'fixtures/platforms/liepin/monitor-drawer.json'


class DrawerProjectionTests(unittest.TestCase):
    def project(self, *, blocked=False, duplicate=False, missing=False, moving=False):
        fixture=json.loads(FIXTURE.read_text())
        setup='const fixture='+json.dumps(fixture)+';\n'
        setup+='const flags='+json.dumps(dict(blocked=blocked,duplicate=duplicate,missing=missing,moving=moving))+';\n'
        setup+='''let tick=0,scrolled=0;
global.setTimeout=(f)=>{tick++;queueMicrotask(f)};
global.innerWidth=fixture.viewport.width;global.innerHeight=fixture.viewport.height;
const target={__reactFiberFixture:{memoizedProps:{contact:fixture.contact}},
 scrollIntoView:()=>{scrolled++},contains:()=>false,
 getBoundingClientRect:()=>{const r=fixture.rects[Math.min(tick,fixture.rects.length-1)];
 const x=r.x+(flags.moving?tick%2*5:0);return {...r,x,right:x+r.width,bottom:r.y+r.height}}};
global.document={querySelectorAll:()=>flags.missing?[]:flags.duplicate?[target,target]:[target],
 elementFromPoint:()=>flags.blocked?{}:target};
'''
        script=setup+'\nconst result=await ('+pages.select_contact('contact-fixture')+')();console.log(JSON.stringify({result,tick,scrolled}));'
        return json.loads(subprocess.check_output(['node','--input-type=module'],input=script,text=True,timeout=5))

    def test_waits_until_drawer_is_visible_and_stable(self):
        data=self.project()
        self.assertIsInstance(data['result'],dict)
        self.assertEqual(data['result']['x'],973)
        self.assertGreaterEqual(data['tick'],4)

    def test_never_clicks_an_obscured_moving_missing_or_duplicate_contact(self):
        for flags in [dict(blocked=True),dict(moving=True),dict(missing=True),dict(duplicate=True)]:
            with self.subTest(flags=flags):
                self.assertIs(self.project(**flags)['result'],False)

    def test_messages_wait_for_mount_instead_of_throwing_on_null_root(self):
        script='''let tick=0;
global.setTimeout=f=>{tick++;queueMicrotask(f)};
const root={__reactFiberFixture:{memoizedProps:{contact:{id:'contact-fixture'},chatLoading:false}}};
global.document={querySelector:()=>tick<2?null:root,querySelectorAll:()=>[]};
const result=await (''' + pages.messages_for('contact-fixture') + ''')();
console.log(JSON.stringify({result,tick}));'''
        data=json.loads(subprocess.check_output(['node','--input-type=module'],input=script,text=True,timeout=5))
        self.assertTrue(data['result']['ready'])
        self.assertGreaterEqual(data['tick'],2)
