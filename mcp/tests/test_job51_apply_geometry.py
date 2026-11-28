"""Run the shipped preparation and driver geometry against captured layout data.

The DOM double models the observed header toggling .fix on scroll and an optional
sticky copy; it never connects to Ego or a recruiting site.
"""
import json
from pathlib import Path
import subprocess
import unittest
from job_agent.browser.ego import _SCRIPT
from job_agent.platforms.job51 import pages


FIXTURE = json.loads((Path(__file__).parent / 'fixtures/platforms/job51/sticky-apply.json').read_text())
GEOMETRY = _SCRIPT.split('await page.evaluate(async (selector) => {', 1)[1].split('},selector);', 1)[0]


class ApplyGeometryTest(unittest.TestCase):
    def run_layout(self, **options):
        script = r'''
const fixture=FIXTURE,options=OPTIONS,selector=SELECTOR;
let scrolled=true;
globalThis.innerWidth=fixture.viewport.width;
globalThis.innerHeight=fixture.viewport.height;
globalThis.setTimeout=fn=>{fn();return 0;};
const main={
 innerText:options.applied?'已投递':fixture.text,
 classList:{contains:c=>c==='disabled'&&!!options.disabled},
 hasAttribute:()=>false,getAttribute:()=>null,
 getClientRects:()=>options.hidden?[]:[1],
 scrollIntoView:()=>{if(!scrolled)main.horizontal=306;},
 getBoundingClientRect:()=>{
   const b=scrolled?fixture.fixed:fixture.normal;
   const x=b.x-(scrolled?0:(main.horizontal||0));
   return {...b,x,left:x,top:b.y,right:x+b.width,bottom:b.y+b.height};
 },contains:()=>false
};
const sticky={...main};
const second={...main};
globalThis.window={scrollTo:()=>{if(!options.stuck)scrolled=false;}};
globalThis.getComputedStyle=()=>({visibility:'visible'});
globalThis.document={
 querySelectorAll:css=>{
   const nodes=options.missing?[]:[...(options.stickyCopy?[sticky]:[]),main,...(options.duplicate?[second]:[])];
   if(css==='.apply-btn-new.big')return nodes;
   if(css!=='.tHeader:not(.fix) .apply-btn-new.big')throw Error('unexpected selector: '+css);
   return nodes.filter(e=>e!==sticky&&!scrolled);
 },
 elementFromPoint:()=>options.blocked?{}:main
};
const geometry=async selector=>{GEOMETRY};
const before=await geometry('.apply-btn-new.big');
const prepared=await (PREPARE)();
const after=prepared?await geometry(selector):null;
console.log(JSON.stringify({before,prepared,after,scrolled}));
'''
        for key, value in [('FIXTURE', json.dumps(FIXTURE)), ('OPTIONS', json.dumps(options)),
                           ('SELECTOR', json.dumps(pages.APPLY)), ('GEOMETRY', GEOMETRY),
                           ('PREPARE', pages.PREPARE_APPLY_JS)]:
            script = script.replace(key, value)
        result = subprocess.run(['node', '--input-type=module'], input=script, text=True,
                                capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_narrow_fixed_header_fails_driver_then_preparation_restores_full_button(self):
        data = self.run_layout()
        self.assertIsNone(data['before'])
        self.assertTrue(data['prepared'])
        self.assertFalse(data['scrolled'])
        self.assertEqual(data['after']['width'], 203)
        self.assertGreaterEqual(data['after']['x'], 0)
        self.assertLessEqual(data['after']['x'] + data['after']['width'], 900)

    def test_sticky_copy_first_does_not_replace_body_target(self):
        data = self.run_layout(stickyCopy=True)
        self.assertTrue(data['prepared'])
        self.assertEqual(data['after']['width'], 203)

    def test_ambiguous_missing_covered_disabled_and_stuck_layouts_fail_closed(self):
        for case in ('duplicate', 'missing', 'blocked', 'disabled', 'applied', 'hidden', 'stuck'):
            with self.subTest(case=case):
                data = self.run_layout(**{case: True})
                self.assertFalse(data['prepared'])
                self.assertIsNone(data['after'])
