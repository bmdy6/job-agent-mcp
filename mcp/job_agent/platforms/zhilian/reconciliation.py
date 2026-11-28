"""Read every visible application-state filter, preserving each event's meaning."""
import json
from job_agent.browser.base import PageTimeout
from job_agent.platforms.zhilian import pages

FILTERS = ('投递成功','被查看','有意向','邀面试','不合适')
NEXT_JS = """() => { /* zhilian.reconcile_next */
 const e=document.querySelector('.ivu-page-next');return !!e&&!e.classList.contains('ivu-page-disabled');
}"""


def filter_projection(label):
    return '''() => { /* zhilian.reconcile_filter */
      const label=LABEL;
      const matches=[...document.querySelectorAll('a')].filter(e=>e.innerText.trim()===label);
      if(matches.length!==1)return false;
      const e=matches[0];e.scrollIntoView({block:'center',behavior:'instant'});
      const r=e.getBoundingClientRect(),hit=document.elementFromPoint(r.x+r.width/2,r.y+r.height/2);
      if(r.width<=0||r.height<=0||!hit||!(e===hit||e.contains(hit)))return false;
      return {x:r.x,y:r.y,width:r.width,height:r.height,viewport:{width:innerWidth,height:innerHeight}};
    }'''.replace('LABEL',json.dumps(label,ensure_ascii=False))


def settle_script(label, before):
    # Active tab changes before asynchronous rows do. Require a new stable
    # record signature; merely observing the highlighted tab can read old data.
    return '''async () => { /* zhilian.reconcile_settle */
      const {label,before}=PARAMS,read=READ_PROJECTION,state=STATE_PROJECTION;
      let prior='',stable=0;
      for(let i=0;i<80;i++) {
        const guard=state();if(guard.risk||guard.login_required)return {...guard,settled:false};
        const active=[...document.querySelectorAll('li.on a')].some(e=>e.innerText.trim()===label);
        const data=read(),key=JSON.stringify(data.rows);
        const changed=key!==before || (key==='[]'&&before==='[]');
        stable=active&&changed&&key===prior?stable+1:0;prior=key;
        if(stable>=3)return {settled:true};
        await new Promise(r=>setTimeout(r,100));
      }
      return {settled:false};
    }'''.replace('READ_PROJECTION',pages.APPLICATIONS_JS).replace('STATE_PROJECTION',pages.STATE_JS).replace(
        'PARAMS',json.dumps({'label':label,'before':before},ensure_ascii=False))


def read_history(page):
    collected=[]
    data=page.evaluate(pages.APPLICATIONS_JS)
    for index,label in enumerate(FILTERS):
        if index:
            before=json.dumps(data.get('rows',[]),ensure_ascii=False,separators=(',',':'))
            page.click_projected(filter_projection(label))
            settled=page.evaluate(settle_script(label,before))
            pages.guard(settled)
            if not settled.get('settled'):
                raise PageTimeout('Application filter did not settle')
        for _ in range(100):
            pages.guard(page.evaluate(pages.STATE_JS))
            data=page.evaluate(pages.APPLICATIONS_JS)
            collected.extend(data.get('rows',[]))
            if not page.evaluate(NEXT_JS):break
            before=json.dumps(data.get('rows',[]),ensure_ascii=False,separators=(',',':'))
            page.click('.ivu-page-next:not(.ivu-page-disabled)')
            settled=page.evaluate(settle_script(label,before))
            pages.guard(settled)
            if not settled.get('settled'):
                raise PageTimeout('Application pagination did not settle')
        else:
            raise PageTimeout('Application pagination exceeded bound')
    data['rows']=list({(r.get('url'),r.get('status'),r.get('time')):r for r in collected}.values())
    return data
