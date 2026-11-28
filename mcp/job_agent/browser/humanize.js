// Pure input plans and seeded geometry. No DOM, I/O or global random state.
export function typingSteps(text, newline = 'reject') {
  if (!['reject', 'shift_enter', 'insert_text'].includes(newline)
      || /[\r\u2028\u2029]/.test(text)
      || (newline === 'reject' && text.includes('\n'))) {
    throw new Error('Explicit safe newline rule required');
  }
  return [...text].map(char => char === '\n'
    ? {method: newline === 'shift_enter' ? 'press' : 'insertText',
       text: newline === 'shift_enter' ? 'Shift+Enter' : '\n'}
    : {method: 'type', text: char});
}

export function rng(seed) {
  let n = seed >>> 0;
  return () => { n += 0x6D2B79F5; let t = Math.imul(n ^ n >>> 15, 1 | n);
    t ^= t + Math.imul(t ^ t >>> 7, 61 | t); return ((t ^ t >>> 14) >>> 0) / 4294967296; };
}
export function trajectory(box, viewport, previous, cfg, seed) {
  const random=rng(seed), between=(a,b)=>a+(b-a)*random();
  const clamp=(v,max)=>Math.max(0,Math.min(max-0.001,v));
  const start=previous ?? [between(0,viewport.width-1),between(0,viewport.height-1)];
  const w=box.width*cfg.shrink, h=box.height*cfg.shrink;
  const end=[between(box.x+(box.width-w)/2,box.x+(box.width+w)/2),
             between(box.y+(box.height-h)/2,box.y+(box.height+h)/2)].map(Math.round);
  // Avoid rounding outside very small/fractional boxes.
  end[0]=Math.max(box.x+(box.width-w)/2,Math.min(box.x+(box.width+w)/2,end[0]));
  end[1]=Math.max(box.y+(box.height-h)/2,Math.min(box.y+(box.height+h)/2,end[1]));
  if(end[0]===box.x+box.width/2 && end[1]===box.y+box.height/2 && w>2)end[0]+=1;
  const distance=Math.hypot(end[0]-start[0],end[1]-start[1]);
  const bend=Math.min(distance*.3,100);
  const c1=[start[0]+(end[0]-start[0])*.35+between(-bend,bend),start[1]+(end[1]-start[1])*.35+between(-bend,bend)];
  const c2=[end[0]+between(-bend,bend),end[1]+between(-bend,bend)];
  const intermediate=Math.floor(between(cfg.steps[0],cfg.steps[1]+1));
  const count=intermediate+1;
  const low=Math.max(cfg.duration_ms[0],count*cfg.step_ms[0]);
  const high=Math.min(cfg.duration_ms[1],count*cfg.step_ms[1]);
  if(low>high)throw new Error('Incompatible trajectory timing');
  const duration=between(low,high);
  const overshoot=random()<cfg.overshoot_probability ? between(...cfg.overshoot_px):0;
  const hesitation=random()<(cfg.hesitation_probability??0);
  const points=[{x:start[0],y:start[1],delay_ms:0,kind:'start'}];
  // Sparse samples of the same eased Bezier. Special moves replace samples.
  let remaining=duration;
  for(let i=1;i<=count;i++) {
    const raw=i/count, t=1-Math.pow(1-raw,2), u=1-t;
    const noise=i===count?0:cfg.noise_px*Math.sin(Math.PI*raw);
    const delay= i===count ? remaining : between(
      Math.max(cfg.step_ms[0],remaining-(count-i)*cfg.step_ms[1]),
      Math.min(cfg.step_ms[1],remaining-(count-i)*cfg.step_ms[0]));
    remaining-=delay;
    points.push({x:clamp(u**3*start[0]+3*u*u*t*c1[0]+3*u*t*t*c2[0]+t**3*end[0]+between(-noise,noise),viewport.width),
      y:clamp(u**3*start[1]+3*u*u*t*c1[1]+3*u*t*t*c2[1]+t**3*end[1]+between(-noise,noise),viewport.height),delay_ms:delay,kind:'curve'});
  }
  if(hesitation) {
    const at=Math.max(2,Math.floor(intermediate*.5));
    const fraction=between(...cfg.hesitation_backtrack),previous=points[at-1];
    points[at]={x:previous.x-(previous.x-start[0])*fraction,
      y:previous.y-(previous.y-start[1])*fraction,
      delay_ms:points[at].delay_ms+between(...cfg.hesitation_ms),kind:'hesitation'};
  }
  if(overshoot) {
    points[intermediate]={x:clamp(end[0]+overshoot*(end[0]-start[0])/(distance||1),viewport.width),
      y:clamp(end[1]+overshoot*(end[1]-start[1])/(distance||1),viewport.height),
      delay_ms:points[intermediate].delay_ms,kind:'overshoot'};
  }
  points[points.length-1].x=end[0];points[points.length-1].y=end[1];
  points[points.length-1].kind='end';
  return {points, end};
}
