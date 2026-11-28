"""Content-free, bounded observations for historical BOSS reconciliation only."""
from job_agent.platforms.boss import pages


def select_existing_session(row):
    """The shared projection scrolls; Vue can unmount its row during that scroll.

    Re-resolve after recycling and wait for stable hit-tested geometry. This is
    only a projection, never an input dispatch or action retry.
    """
    project = pages.select_session(row)
    return '''async () => { /* boss.reconcile_select_session */
      const project=PROJECT;let prior='',stable=0;
      for(let i=0;i<30;i++) {
        const box=project();
        const key=box?JSON.stringify(box):'';
        stable=box&&key===prior?stable+1:0;prior=key;
        if(stable>=3)return box;
        await new Promise(r=>setTimeout(r,100));
      }
      return false;
    }'''.replace('PROJECT', project)


# Capture only rows intersecting the actual scroll viewport. The virtual list
# mounts offscreen rows too: storing their current scroll position overwrote
# useful positions with bottom-of-list positions in the original reader.
SCAN_JS = '''async () => { /* boss.reconcile_scan */
 const state=STATE_PROJECTION;
 const container=document.querySelector('.user-list-content');
 if(!container)return {entries:[],complete:false};
 const entries=new Map();container.scrollTop=0;
 let complete=false;
 for(let step=0;step<200;step++) {
   let prior='',stable=0;
   for(let poll=0;poll<30;poll++) {
     const guard=state();if(guard.risk||guard.login_required)return {...guard,entries:[],complete:false};
     const nodes=[...document.querySelectorAll('[role=listitem]')];
     const key=container.scrollTop+':'+nodes.map(e=>e.__vue__?.$props?.source?.uniqueId||'').join('|');
     stable=key===prior?stable+1:0;prior=key;
     if(stable>=3)break;
     await new Promise(r=>setTimeout(r,50));
   }
   if(stable<3)break;
   const viewport=container.getBoundingClientRect();
   for(const e of document.querySelectorAll('[role=listitem]')) {
     const r=e.getBoundingClientRect(),s=e.__vue__?.$props?.source;
     if(!s?.encryptJobId||!s?.uniqueId||r.bottom<=Math.max(0,viewport.top)||r.top>=Math.min(innerHeight,viewport.bottom))continue;
     const row={encryptJobId:s.encryptJobId,uniqueId:s.uniqueId,brandName:s.brandName,
       jobName:s.jobName,lastMsgId:String(s.lastMsgId??'')};
     entries.set(JSON.stringify([s.encryptJobId,s.uniqueId]),{row,position:container.scrollTop});
   }
   const old=container.scrollTop;
   if(old+container.clientHeight>=container.scrollHeight-1){complete=true;break;}
   container.scrollTop=Math.min(container.scrollHeight-container.clientHeight,old+container.clientHeight*.8);
   if(container.scrollTop<=old+1)break;
 }
 return {entries:[...entries.values()],complete};
}'''


def scan_script():
    return SCAN_JS.replace('STATE_PROJECTION', pages.STATE_JS)
