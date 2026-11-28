"""Read only terminal vacancy state before waiting for an application control."""
from job_agent.browser.base import BrowserError, PageTimeout

class JobUnavailable(BrowserError):
    code = 'job_unavailable'


def wait_job_ready(page, selector, *, state_js=None):
    import json
    # Race readiness against terminal state inside the page; never wait on Apply alone.
    state = page.evaluate(r'''async () => { /* job.availability */
      const until=Date.now()+10000;
      do {
        const visible=e=>e.getClientRects().length && getComputedStyle(e).visibility!=='hidden';
        const projection=STATE_PROJECTION;
        if(projection?.unavailable)return {state:'unavailable',evidence:projection.unavailable_evidence};
        if(projection?.risk || projection?.login_required)return 'ready';
        const text=projection?'':document.body?.innerText||'';
        if(/(?:职位|岗位)(?:已)?(?:关闭|下线|下架|失效|不存在)|停止招聘|招聘已结束|该职位已结束/.test(text))return 'unavailable';
        if([...document.querySelectorAll(SELECTOR)].some(visible))return 'ready';
        await new Promise(r=>setTimeout(r,100));
      }while(Date.now()<until);
      return 'timeout';
    }'''.replace('SELECTOR',json.dumps(selector)).replace('STATE_PROJECTION', '('+state_js+')()' if state_js else 'null'))
    if isinstance(state,dict) and state.get('state')=='unavailable':
        evidence=state.get('evidence') or {}
        raise JobUnavailable('原站明确显示岗位关闭或停止招聘；'+str(evidence.get('selector','页面状态'))[:200]+'：'+str(evidence.get('text','明确不可用'))[:200])
    if state == 'unavailable':
        raise JobUnavailable('原站明确显示岗位关闭或停止招聘')
    if state == 'timeout':
        raise PageTimeout('job.availability: page_timeout')
    # Test/alternate Browser implementations may not project this helper.
    if state != 'ready':
        page.wait_for(selector)
