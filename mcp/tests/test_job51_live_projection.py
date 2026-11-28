"""Reproduce Element UI relocating a visible dialog under body."""
import json
from pathlib import Path
import subprocess
import unittest
from job_agent.platforms.job51 import pages

class DialogProjection(unittest.TestCase):
    def test_component_remains_resolvable_after_dialog_moves(self):
        fixture=json.loads((Path(__file__).parent/'fixtures/platforms/job51/relocated-dialog.json').read_text())
        script='''const component = %s;
component.$on=(event,listener)=>{component.observed=event==='confirm'};
const wrapper={__vue__:{$options:{name:'transition'},$parent:{$options:{name:'ElDialog'},$parent:component}}};
const element={parentElement:wrapper,querySelectorAll:()=>[{querySelector:()=>null}]};
global.document={querySelector:()=>element,documentElement:{removeAttribute:()=>{}}};
global.window={};
const projection=(%s)(); const observable=(%s)();
console.log(JSON.stringify({projection,observable,observed:component.observed}));
''' % (json.dumps(fixture),pages.ATTACHMENT_JS,pages.OBSERVE_JS)
        data=json.loads(subprocess.check_output(['node','-e',script],text=True))
        self.assertTrue(data['projection'].get('visible'))
        self.assertEqual(data['projection']['cvLogIdList'],['cv-fixture'])
        self.assertTrue(data['observable'])
        self.assertTrue(data['observed'])
