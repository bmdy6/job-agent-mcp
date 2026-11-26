"""Synchronous Ego Lite driver. No shell, persistent REPL, or action replay.

Ego owns durable spaces/pages; each command gets a fresh short-lived Node process.
Only named space resolution (foreground or heartbeat) may recreate a space.
Existing page handles fail closed and never replay actions on its replacement.
"""
from __future__ import annotations

import json
import logging
import math
import secrets
from collections import deque
from .input_config import InputSettings
from urllib.parse import urlsplit
from pathlib import Path
import subprocess
import threading
from typing import Any

from .base import BrowserError, PageTimeout, SpaceUnavailable, TabObservation, RiskControl

_PREFIX = '__JOB_AGENT_RESULT__'
_GESTURE_PREFIX = '__JOB_AGENT_GESTURE__'
log = logging.getLogger(__name__)

# All arguments are serialized as data. No platform selectors/parsers belong here.
_GEOMETRY = Path(__file__).with_name('humanize.js').read_text().replace('export ', '')

_SCRIPT = _GEOMETRY + r'''
const pointerTrace=[];
let trustedEvents=[];
let gesture=null;
let gestureStarted=null;
const reportGesture=()=>console.log("__JOB_AGENT_GESTURE__"+JSON.stringify({gesture}));
let inputStep="resolve";
async function run() {
  // Preflight the entire input before resolving a page or dispatching any input.
  const typing = request.method === 'fill'
    ? typingSteps(request.args[1], request.args[2]?.newline) : [];
  const resolve = request.op === "space" || request.op === "keepalive";
  const spaces = await listTaskSpaces();
  const matches = spaces.filter(s => resolve
    ? s.name === request.name : s.id === request.space_id);
  function unavailable() { throw {code: "space_unavailable"}; }
  if (matches.length > 1) unavailable();
  const metadata = matches[0];
  if (metadata && metadata.ownership !== "agent") unavailable();
  if (!metadata && !resolve) {
    if (request.op === "shutdown") return null;
    unavailable();
  }
  const task = resolve
    ? await taskSpace(request.name) : await taskSpace(request.space_id);
  if (task.ownership !== "agent") unavailable();
  if (resolve) {
    const tabs = await task.tabs();
    let anchor = tabs.find(t => t.label && (!request.anchor || t.label === request.anchor)
      && t.openedBy === "agent" && t.url === "about:blank");
    if (!anchor) {
      // Only a newly created space's initial Page can be navigated as an anchor.
      const initial = !metadata && tabs.find(t => t.label && t.openedBy === "agent");
      const page = initial ? task.page(initial.label) : await task.newPage();
      await page.goto("about:blank");
      anchor = {label: page.label};
    }
    if (request.op === "keepalive" && (await task.page(anchor.label).evaluate("1")) !== 1) {
      throw {code: "browser_error"};
    }
    return {space_id: task.spaceId, anchor: anchor.label};
  }
  if (request.op === "shutdown") {
    const tabs = await task.tabs();
    const anchor = tabs.find(t => t.label === request.anchor && t.openedBy === "agent"
      && t.url === "about:blank");
    if (!anchor) unavailable();
    // Never finish: it transfers the space to the user.
    return null;
  }
  if (request.op === "new_page") {
    const page = await task.newPage();
    return {label: page.label};
  }
  if (request.op === "tabs") {
    const tabs=await task.tabs();
    for(const tab of tabs) {
      if(!tab.label && tab.openedBy==='agent')tab.label=(await task.adopt(tab.page)).label;
    }
    return tabs.filter(t => t.label && t.label !== request.anchor
      && t.openedBy === "agent").map(t => ({label:t.label,url:t.url}));
  }
  const tabs = await task.tabs();
  const tab = tabs.find(t => t.label === request.label);
  if (!tab || tab.openedBy !== "agent" || request.label === request.anchor) unavailable();
  const page = task.page(request.label);
  const domain=host=>host.split(".").slice(-2).join(".");
  const assertOrigin=async()=>{
    if(!request.origin_host)return;
    const current=await page.evaluate(()=>location.hostname);
    if(domain(current)!==domain(request.origin_host))throw {code:"risk_control"};
  };
  if(!["goto","close"].includes(request.method))await assertOrigin();
  if(request.input && ["goto","keyboard.press","read_detail","click","hover","fill","click_projected"].includes(request.method)) {
    const pauseRandom=rng(request.seed ^ 12345);
    if(pauseRandom()<request.input.long_pause_probability)
      await new Promise(r=>setTimeout(r,1000*(request.input.long_pause_seconds[0]+pauseRandom()*(request.input.long_pause_seconds[1]-request.input.long_pause_seconds[0]))));
  }
  if (request.method === "read_detail") {
    const cfg=request.input,random=rng(request.seed);
    const total=cfg.detail_seconds[0]+random()*(cfg.detail_seconds[1]-cfg.detail_seconds[0]);
    const count=Math.floor(cfg.detail_scrolls[0]+random()*(cfg.detail_scrolls[1]-cfg.detail_scrolls[0]+1));
    const started=Date.now();
    for(let i=0;i<count;i++) {
      await new Promise(r=>setTimeout(r,Math.max(0,total*1000-(Date.now()-started))/(count-i+1)));
      await page.mouse.move(400,300);
      await page.mouse.wheel(0,100+Math.floor(random()*300));
    }
    await new Promise(r=>setTimeout(r,Math.max(0,total*1000-(Date.now()-started))));
    return {duration_ms:Date.now()-started,planned_ms:total*1000,scrolls:count};
  }
  if (["click", "hover", "fill", "click_projected"].includes(request.method)) {
    const readRisk=()=>page.evaluate(() => {
      const visible=e=>!!e.getClientRects().length && getComputedStyle(e).visibility!=="hidden";
      return /captcha|\/verify/.test(location.pathname) || [...document.querySelectorAll('[role=dialog],[role=alert],.verify-wrap,.geetest_panel,.nc-container,iframe[src*=captcha]')].filter(visible).some(e=>/验证码|安全验证|频繁|访问异常|访问受限/.test(e.innerText)||e.matches('.geetest_panel,.nc-container,iframe[src*=captcha]'));
    });
    if(await readRisk())throw {code:"risk_control"};
    await page.evaluate(() => {
      window.__jobAgentTrusted=[];
      if(!window.__jobAgentTrustListener) {
        window.__jobAgentTrustListener=e=>window.__jobAgentTrusted.push({type:e.type,isTrusted:e.isTrusted,at:Date.now()});
        for(const type of ['mousemove','mousedown','mouseup','keydown','input'])document.addEventListener(type,window.__jobAgentTrustListener,true);
      }
    });
    const selector=request.args[0];
    if(request.method!=="click_projected")await page.waitForSelector(selector, {timeout:5000});
    inputStep="target_geometry";
    const box = request.method==="click_projected" ? await page.evaluate(selector) : await page.evaluate(async (selector) => {
      const bits=selector.replace(/^(?:loc=css:|css=)/, "").split(/\s*>>\s*nth=/);
      const elements=[...document.querySelectorAll(bits[0])].filter(e=>e.getClientRects().length && getComputedStyle(e).visibility!=="hidden");
      const el=bits.length===2 ? elements.at(Number(bits[1])) : elements.length===1 ? elements[0] : null;
      if(!el) return null;
      el.scrollIntoView({block:"center",inline:"nearest",behavior:"instant"});
      // scrollIntoView can animate a sticky header. Wait for actual geometry to settle.
      let prior='',stable=0;
      for(let i=0;i<30 && stable<3;i++) {
        const r=el.getBoundingClientRect(),key=[r.x,r.y,r.width,r.height].join(',');
        stable=key===prior?stable+1:0;prior=key;
        await new Promise(r=>setTimeout(r,100));
      }
      const r=el.getBoundingClientRect();
      const x=Math.max(0,r.x),y=Math.max(0,r.y),right=Math.min(innerWidth,r.right),bottom=Math.min(innerHeight,r.bottom);
      if(right<=x || bottom<=y)return null;
      return {x,y,width:right-x,height:bottom-y,viewport:{width:innerWidth,height:innerHeight}};
    },selector);
    if(!box)throw {code:"browser_error"};
    const cfg=request.input, random=rng(request.seed ^ 0x9e3779b9);
    const plan=trajectory(box,box.viewport,request.previous,cfg,request.seed);
    const trace=pointerTrace;
    const started=Date.now();
    gestureStarted=started;
    gesture={planned_points:plan.points.length, duration_ms:0, move_ms:[],
      budget_ms:(plan.points.length+(request.method==='hover'?0:2))*(request.mouse_point_ms??cfg.mouse_point_ms)
        +cfg.gesture_margin_ms+cfg.duration_ms[1]+cfg.hesitation_ms[1]+(request.method==='hover'?0:cfg.hold_ms[1])};
    reportGesture();
    for(const p of plan.points) {
      if(p.delay_ms)await new Promise(r=>setTimeout(r,p.delay_ms));
      const before=Date.now();
      inputStep="mouse.move";
      await page.mouse.move(p.x,p.y);
      gesture.move_ms.push(Date.now()-before);
      gesture.duration_ms=Date.now()-started;
      trace.push({at:Date.now(),x:p.x,y:p.y});
      reportGesture();
    }
    trustedEvents=await page.evaluate(()=>window.__jobAgentTrusted||[]);
    if(request.method!=="hover") {
      // A late overlay/layout change must never turn a planned click into another action.
      inputStep="target_recheck";
      const valid=await page.evaluate(({point,selector,projected})=>{
        if(projected)return true; // Stable-identity projection is rechecked below.
        const bits=selector.replace(/^(?:loc=css:|css=)/, "").split(/\s*>>\s*nth=/);
        const nodes=[...document.querySelectorAll(bits[0])].filter(e=>e.getClientRects().length&&getComputedStyle(e).visibility!=="hidden");
        const el=bits.length===2?nodes.at(Number(bits[1])):nodes.length===1?nodes[0]:null;
        const hit=document.elementFromPoint(...point),r=el?.getBoundingClientRect();
        return !!r&&point[0]>=r.left&&point[0]<=r.right&&point[1]>=r.top&&point[1]<=r.bottom
          &&!!hit&&(el===hit||el.contains(hit));
      },{point:plan.end,selector,projected:request.method==="click_projected"});
      if(await readRisk())throw {code:"risk_control"};
      await assertOrigin();
      if(!valid)throw {code:"browser_error"};
      if(request.method==="click_projected") {
        const current=await page.evaluate(selector);
        if(!current||Math.abs(current.x-box.x)>1||Math.abs(current.y-box.y)>1)throw {code:"browser_error"};
      }
      inputStep="mouse.down";
      await page.mouse.down({button:"left"});
      await new Promise(r=>setTimeout(r,cfg.hold_ms[0]+random()*(cfg.hold_ms[1]-cfg.hold_ms[0])));
      inputStep="mouse.up";
      await page.mouse.up({button:"left"});
      inputStep="post_click_observation";
    }
    gesture.duration_ms=Date.now()-started;
    if(request.method==="fill") {
      await page.keyboard.press("ControlOrMeta+A");
      await page.keyboard.press("Backspace");
      for(const step of typing) {
        await new Promise(r=>setTimeout(r,cfg.typing_ms[0]+random()*(cfg.typing_ms[1]-cfg.typing_ms[0])));
        await page.keyboard[step.method](step.text);
      }
    }
    let trusted=trustedEvents;
    try { trusted=await page.evaluate(()=>window.__jobAgentTrusted||[]); }
    catch(error) {
      if(inputStep!=="post_click_observation")throw error;
      // Mouse-up completed; navigation/popup observation cannot replay the click.
      gesture.observation='unavailable';
    }
    trustedEvents=trusted;
    return {mouse:plan.end,trace,trusted,gesture};
  }
  if (request.method === "keyboard.press") return await page.keyboard.press(...request.args);
  const allowed = ["goto", "url", "evaluate", "snapshot", "click", "fill",
    "waitForSelector", "waitForURL", "screenshot", "close"];
  if (!allowed.includes(request.method)) throw {code: "browser_error"};
  const value=await page[request.method](...request.args);
  if(request.method==="goto")await assertOrigin();
  return value;
}
try {
  const value = await run();
  console.log("__JOB_AGENT_RESULT__" + JSON.stringify({ok: true, value: value ?? null}));
} catch (error) {
  if(gesture)gesture.duration_ms=Date.now()-gestureStarted;
  const text = String(error?.message ?? error);
  const raw = String(error?.code ?? "");
  let code = "browser_error";
  if (raw === "space_unavailable" || /TASK_SPACE.*(NAME_IN_USE|CONTROL|INACTIVE|NOT_FOUND|USER)/i.test(raw)
      || /control of this task space|user.owned|inactive|handed.*user/i.test(text)) code = "space_unavailable";
  else if (raw === "risk_control") code="risk_control";
  else if (/timeout|timed out/i.test(raw + " " + text)) code = "page_timeout";
  console.log("__JOB_AGENT_RESULT__" + JSON.stringify({ok: false, code,step:inputStep,trace:pointerTrace,trusted:trustedEvents,gesture}));
}
'''


class EgoBrowser:
    def __init__(self, executable: str = '~/.local/bin/ego-browser', *, timeout_s: float = 30,
                 keepalive_seconds: float = 300, on_event=None, human_input=None):
        if timeout_s <= 0:
            raise ValueError('timeout_s must be positive')
        self.executable = str(Path(executable).expanduser())
        self.timeout_s = timeout_s
        self.human_input = human_input or InputSettings()
        self._mouse_samples = deque(maxlen=self.human_input.mouse_sample_window)
        if (type(keepalive_seconds) not in (int, float)
                or not math.isfinite(keepalive_seconds) or keepalive_seconds < 0):
            raise ValueError('keepalive_seconds must be finite and nonnegative')
        self.keepalive_seconds, self.on_event = keepalive_seconds, on_event
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._blocked, self._retired, self._failed = set(), set(), set()
        self._spaces: dict[str, EgoSpace] = {}

    @property
    def mouse_point_ms(self):
        return (sum(self._mouse_samples)/len(self._mouse_samples) if self._mouse_samples
                else self.human_input.mouse_point_ms)

    def _observe_gesture(self, value, *, failed=False):
        gesture = value.get('gesture') if isinstance(value, dict) else None
        if not gesture:
            return
        samples = gesture.get('move_ms', [])
        self._mouse_samples.extend(samples)
        log.info('gesture%s planned_points=%s completed_points=%s duration_ms=%s point_avg_ms=%s rolling_point_ms=%s budget_ms=%s trusted=%s',
                 '_failed' if failed else '', gesture['planned_points'],len(samples),
                 gesture['duration_ms'],sum(samples)/len(samples) if samples else None,
                 self.mouse_point_ms,gesture.get('budget_ms'),value.get('trusted',[]))

    def _call(self, request: dict, *, timeout_s: float | None = None) -> Any:
        # Heartbeats and foreground commands share a connection/ownership view.
        with self._lock:
            return self._execute(request, timeout_s=timeout_s)

    def _execute(self, request: dict, *, timeout_s: float | None = None) -> Any:
        # Log only the internally controlled operation name, never URL, selector,
        # greeting, request arguments, or raw CLI output.
        operation = request.get('method') if request.get('op') == 'page' else request.get('op')
        if operation not in {'space','keepalive','shutdown','new_page','tabs','goto','url','evaluate','snapshot','click','fill',
                             'waitForSelector','waitForURL','screenshot','close','keyboard.press'}:
            operation = 'unknown'
        from collect.deadline import remaining, CollectionTimeout
        budget=remaining()
        command_timeout=max(self.timeout_s, timeout_s or 0)
        if budget is not None:
            command_timeout=min(command_timeout,budget)
        script = 'const request = ' + json.dumps(request, ensure_ascii=True) + ';\n' + _SCRIPT
        try:
            proc = subprocess.run([self.executable, 'nodejs'], input=script,
                                  text=True, capture_output=True,
                                  timeout=command_timeout)
        except subprocess.TimeoutExpired as error:
            if request.get('method') in {'click','hover','fill','click_projected'}:
                # Progress survives a killed Node process; it contains no selectors/body.
                progress=None
                for output in (error.stdout,error.stderr):
                    if isinstance(output,bytes):output=output.decode(errors='replace')
                    for line in (output or '').splitlines():
                        if line.startswith(_GESTURE_PREFIX):
                            try:progress=json.loads(line[len(_GESTURE_PREFIX):])
                            except ValueError:pass
                if progress:
                    self._observe_gesture(progress,failed=True)
                log.info('gesture_failed duration_ms=unknown isTrusted=unknown reason=transport_timeout')
            if budget is not None and command_timeout == budget:
                raise CollectionTimeout('Platform collection timed out') from None
            # Outcome may be unknown; never repeat a click/fill/navigation automatically.
            raise PageTimeout(f'Ego {operation} timed out; outcome unknown') from None
        except OSError:
            raise BrowserError('Could not start Ego CLI') from None
        if proc.returncode:
            raise BrowserError(f'Ego {operation} CLI failed; outcome unknown')
        try:
            # Ego CLI may route console.log to stderr (observed on macOS).
            lines = (proc.stdout + '\n' + proc.stderr).splitlines()
            line = next(s for s in reversed(lines) if s.startswith(_PREFIX))
            payload = json.loads(line[len(_PREFIX):])
            if not isinstance(payload, dict) or type(payload.get('ok')) is not bool:
                raise ValueError()
        except (StopIteration, ValueError, TypeError):
            raise BrowserError(f'Invalid Ego {operation} CLI response; outcome unknown') from None
        self._observe_gesture(payload.get('value') if payload['ok'] else payload, failed=not payload['ok'])
        if not payload['ok']:
            trace=payload.get('trace',[])
            if trace and not payload.get('gesture'):
                log.info('gesture_failed duration_ms=%s trusted=%s',trace[-1]['at']-trace[0]['at'],payload.get('trusted',[]))
            for point in trace:
                log.info('pointer at=%s x=%s y=%s',point['at'],point['x'],point['y'])
            cls = {'space_unavailable': SpaceUnavailable, 'page_timeout': PageTimeout,'risk_control':RiskControl}.get(payload.get('code'), BrowserError)
            step=payload.get('step','unknown')
            if step not in {'resolve','target_geometry','mouse.move','target_recheck','mouse.down','mouse.up','post_click_observation'}: step='unknown'
            raise cls(f'Ego {operation}.{step} failed: {cls.code}')
        return payload.get('value')

    def _notify(self, name, event):
        log.warning('Ego space event name=%s event=%s', name, event)
        if self.on_event is not None:
            try:
                self.on_event(name, event)
            except Exception:
                log.error('Ego space alert failed event=%s', event)

    def _resolve(self, name: str, op: str) -> EgoSpace:
        if not isinstance(name, str) or not name.strip():
            raise ValueError('Space name must not be empty')
        with self._lock:
            if name in self._blocked:
                raise SpaceUnavailable('Space requires explicit restart after user control')
            if op == 'keepalive' and name in self._retired:
                raise SpaceUnavailable('Space is shutting down')
            previous = self._spaces.get(name)
            rejected = False
            try:
                request = {'op': op, 'name': name}
                if previous:
                    request['anchor'] = previous._anchor
                value = self._call(request)
            except SpaceUnavailable:
                self._blocked.add(name)
                rejected = True
            if not rejected:
                if (not isinstance(value, dict) or type(value.get('space_id')) is not int
                        or not isinstance(value.get('anchor'), str) or not value['anchor']):
                    raise BrowserError('Invalid Ego space response')
                space = previous if previous and previous.space_id == value['space_id'] else EgoSpace(self, name, value['space_id'], value['anchor'])
                space._anchor = value['anchor']
                self._spaces[name] = space
                self._failed.discard(name)
        # Notifications can block on transport; never hold the browser lock.
        if rejected:
            self._notify(name, 'unavailable')
            raise SpaceUnavailable('Ego space unavailable')
        if previous and previous.space_id != space.space_id:
            self._notify(name, 'recreated')
        return space

    def space(self, name: str) -> EgoSpace:
        space = self._resolve(name, 'space')
        with self._lock:
            self._retired.discard(name)
            idle_worker = self._thread is None or not self._thread.is_alive()
            if idle_worker and self._stop.is_set():
                self._stop = threading.Event()
            if self.keepalive_seconds and idle_worker:
                self._thread = threading.Thread(target=self._keepalive_loop, name='ego-keepalive', daemon=True)
                self._thread.start()
        return space

    def _keepalive_loop(self):
        while not self._stop.wait(self.keepalive_seconds):
            self.keepalive_once()

    def keepalive_once(self):
        with self._lock:
            names = set(self._spaces) - self._blocked - self._retired
        for name in names:
            if self._stop.is_set():
                break
            try:
                self._resolve(name, 'keepalive')
            except SpaceUnavailable:
                pass  # _resolve marks the space blocked and emits one alert.
            except BrowserError:
                with self._lock:
                    first = name not in self._failed
                    self._failed.add(name)
                if first:
                    self._notify(name, 'keepalive_failed')

    def stop_keepalive(self):
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=self.timeout_s + 5)

    def shutdown(self, name: str) -> None:
        with self._lock:
            space = self._spaces.get(name)
            self._retired.add(name)
            last = not (set(self._spaces) - self._retired)
            blocked = name in self._blocked
        if last:
            self.stop_keepalive()
        if blocked:
            return  # User-controlled pages are no longer eligible for cleanup.
        if space is not None:
            for page in list(space._pages):
                page.close()
            self._call(space._request('shutdown'))
            with self._lock:
                self._spaces.pop(name, None)


class EgoSpace:
    def __init__(self, browser: EgoBrowser, name: str, space_id: int, anchor: str):
        self._browser, self.name, self.space_id, self._anchor = browser, name, space_id, anchor
        self._pages = []

    def _request(self, op: str) -> dict:
        return {'op': op, 'space_id': self.space_id, 'anchor': self._anchor}

    def tabs(self) -> list[TabObservation]:
        with self._browser._lock:
            if self.name in self._browser._blocked or self._browser._spaces.get(self.name) is not self:
                raise SpaceUnavailable('Task space is no longer the observed owned space')
            value=self._browser._call(self._request('tabs'))
        if not isinstance(value,list):
            raise BrowserError('Invalid Ego tabs response')
        seen=set()
        result=[]
        for item in value:
            if (not isinstance(item,dict) or not isinstance(item.get('label'),str)
                    or not item['label'] or item['label']==self._anchor or item['label'] in seen
                    or not isinstance(item.get('url'),str)):
                raise BrowserError('Invalid Ego tab observation')
            seen.add(item['label'])
            result.append(TabObservation(item['label'],item['url']))
        return result

    def inspect_page(self,label: str) -> EgoPage:
        with self._browser._lock:
            if label not in {t.label for t in self.tabs()}:
                raise SpaceUnavailable('Observed page no longer available')
            return EgoPage(self,label,inspection_only=True)

    def claim_page(self,page: EgoPage,before: set[str]) -> None:
        with self._browser._lock:
            if (page._space is not self or page.label in before or not page._inspection_only
                    or page._closed or page.label not in {t.label for t in self.tabs()}):
                raise SpaceUnavailable('Cannot claim unrelated or unavailable observed page')
            page._inspection_only=False
            self._pages.append(page)

    def new_page(self) -> EgoPage:
        with self._browser._lock:
            if self.name in self._browser._blocked:
                raise SpaceUnavailable('Space requires explicit restart after user control')
            # A collector may retain its Space between reads. New pages use the
            # replacement already resolved by the heartbeat; old pages keep IDs.
            current = self._browser._spaces.get(self.name)
            if current is not None and current is not self:
                return current.new_page()
            value = self._browser._call(self._request('new_page'))
            if (not isinstance(value, dict) or not isinstance(value.get('label'), str)
                    or not value['label'] or value['label'] == self._anchor):
                raise BrowserError('Invalid Ego page response')
            page = EgoPage(self, value['label'])
            self._pages.append(page)
            return page


class EgoPage:
    def __init__(self, space: EgoSpace, label: str, *, inspection_only=False):
        self._space, self.label, self._closed = space, label, False
        self._inspection_only=inspection_only
        self._mouse = None
        self._origin_host = None

    def _call(self, method: str, *args, timeout_s=None):
        if self._inspection_only and method not in {'url','evaluate','snapshot','waitForSelector','waitForURL'}:
            raise BrowserError('Observed page must be verified and claimed before acting')
        if self._closed:
            raise BrowserError('Page is closed')
        browser = self._space._browser
        with browser._lock:
            if self._space.name in browser._blocked:
                raise SpaceUnavailable('Space requires explicit restart after user control')
            request = self._space._request('page')
            request.update(label=self.label, method=method, args=list(args),origin_host=self._origin_host)
            if method in {'click','hover','fill','click_projected','read_detail','goto','keyboard.press'}:
                cfg=browser.human_input.model_dump()
                request.update(input=cfg,seed=secrets.randbits(32),previous=self._mouse)
                # Page wait/transport allowance is independent of intentional input.
                duration=cfg['long_pause_seconds'][1]
                if method in {'click','hover','fill','click_projected'}:
                    # The first fast move must not shrink the next rate-limited budget.
                    point_ms=max(cfg['mouse_point_ms'],browser.mouse_point_ms)
                    request['mouse_point_ms']=point_ms
                    points=cfg['steps'][1]+2  # start + intermediate + final
                    duration+=(points*point_ms+cfg['gesture_margin_ms']+cfg['duration_ms'][1]+cfg['hesitation_ms'][1])/1000
                    if method!='hover':duration+=(2*point_ms+cfg['hold_ms'][1])/1000
                    if method=='fill':duration+=len(args[1])*cfg['typing_ms'][1]/1000
                if method=='read_detail':
                    # Detail scroll move/wheel calls also have native input latency.
                    duration+=cfg['detail_seconds'][1]+2*cfg['detail_scrolls'][1]*max(cfg['mouse_point_ms'],browser.mouse_point_ms)/1000
                timeout_s=max(browser.timeout_s,timeout_s or 0)+duration
            try:
                value=browser._call(request, timeout_s=timeout_s)
            except Exception:
                if method in {'click','hover','fill','click_projected','read_detail','goto','keyboard.press'}:
                    self._mouse=None  # Unknown final pointer position; never replay.
                raise
            if method in {'click','hover','fill','click_projected'} and isinstance(value,dict):
                self._mouse=value.get('mouse')
                trace=value.get('trace',[])
                for point in value.get('trace',[]):
                    log.info('pointer at=%s x=%s y=%s',point['at'],point['x'],point['y'])
                return None
            return value

    def goto(self, url: str, *, wait_ms: int = 3000) -> None:
        if wait_ms <= 0:
            raise ValueError('wait_ms must be positive')
        self._origin_host=urlsplit(url).hostname
        self._call('goto', url, {'timeout': wait_ms, 'waitUntil': 'domcontentloaded'}, timeout_s=wait_ms / 1000 + 5)

    def wait_for_url(self, url: str, *, timeout_ms: int = 10000) -> None:
        self._call('waitForURL', url, {'timeout': timeout_ms}, timeout_s=timeout_ms / 1000 + 5)

    def url(self) -> str:
        return self._call('url')

    def evaluate(self, js: str) -> Any:
        # 平台 pages.py 的脚本写成函数字面量（"() => {...}"）；ego 的 evaluate 只求值表达式，
        # 直接传函数会得到函数本身（序列化为 {}）。这里统一包成立即调用。
        return self._call('evaluate', as_expression(js))

    def snapshot(self) -> str:
        return self._call('snapshot')

    def click(self, selector: str) -> None:
        self._call('click', selector)

    def click_projected(self, projection: str) -> None:
        self._call('click_projected',as_expression(projection))

    def read_detail(self):
        value=self._call('read_detail')
        log.info('detail_read %s',value)

    def hover(self, selector: str) -> None:
        self._call('hover', selector)

    def fill(self, selector: str, text: str, *, newline: str = 'reject') -> None:
        if (newline not in {'reject', 'shift_enter', 'insert_text'}
                or any(c in text for c in '\r\u2028\u2029')
                or ('\n' in text and newline == 'reject')):
            raise BrowserError('Explicit safe newline rule required')
        if newline == 'reject':
            self._call('fill', selector, text)
        else:
            self._call('fill', selector, text, {'newline': newline})

    def press(self, key: str) -> None:
        self._call('keyboard.press', key)

    def wait_for(self, selector: str, *, timeout_ms: int = 10000) -> None:
        if timeout_ms <= 0:
            raise ValueError('timeout_ms must be positive')
        self._call('waitForSelector', selector, {'timeout': timeout_ms}, timeout_s=timeout_ms / 1000 + 5)

    def screenshot(self, path: str) -> None:
        self._call('screenshot', {'path': path})

    def close(self) -> None:
        if not self._closed:
            self._call('close')
            self._closed = True


_FUNCTION_LITERAL = __import__('re').compile(r'^\s*(?:async\s+)?(?:function\b|\([^()]*\)\s*=>|[A-Za-z_$][\w$]*\s*=>)')


def as_expression(js: str) -> str:
    """函数字面量包成立即调用；普通表达式原样返回。"""
    return f'({js})()' if _FUNCTION_LITERAL.match(js) else js
