"""Read only terminal vacancy state before waiting for an application control."""
from browser.base import BrowserError, PageTimeout

class JobUnavailable(BrowserError):
    code = 'job_unavailable'


def wait_job_ready(page, selector):
    import json
    # Race readiness against terminal state inside the page; never wait on Apply alone.
    state = page.evaluate(r'''async () => { /* job.availability */
      const until=Date.now()+10000;
      do {
        const visible=e=>e.getClientRects().length && getComputedStyle(e).visibility!=='hidden';
        const text=document.body?.innerText||'';
        if(/(?:职位|岗位)(?:已)?(?:关闭|下线|下架|失效|不存在)|停止招聘|招聘已结束|该职位已结束/.test(text))return 'unavailable';
        if([...document.querySelectorAll(SELECTOR)].some(visible))return 'ready';
        await new Promise(r=>setTimeout(r,100));
      }while(Date.now()<until);
      return 'timeout';
    }'''.replace('SELECTOR',json.dumps(selector)))
    if state == 'unavailable':
        raise JobUnavailable('原站明确显示岗位关闭或停止招聘')
    if state == 'timeout':
        raise PageTimeout('job.availability: page_timeout')
    # Test/alternate Browser implementations may not project this helper.
    if state != 'ready':
        page.wait_for(selector)
