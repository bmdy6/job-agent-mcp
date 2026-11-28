"""Observed 2026-11-27 desktop settings. No message composer or HR send is used."""
URL = 'https://i.zhaopin.com/im/greeting/setting'
NAV = '.greeting-left-nav__item >> nth=1'
ADD = '.greeting-panel__add-btn'
INPUT = '.phrase-modal textarea'
SAVE = '.phrase-modal__btn--ok'
READ = r'''() => { /* zhilian.default_greeting */
 const v=document.querySelector('.greeting-panel')?.__vue__;
 const rows=Object.values(v?.contentList||{}).flat();
 return {ready:v?.initialized===true && Array.isArray(rows) && rows.length>0,
 rows:rows.map(r=>({id:r.id,text:r.content,selected:r.defaultStatus===true})),
 origin:location.origin,path:location.pathname,
 login:/login|passport/.test(location.href),
 risk:[...document.querySelectorAll('[role=dialog],[role=alert],.verify-wrap')].filter(e=>e.getClientRects().length).some(e=>/验证码|安全验证|操作频繁/.test(e.innerText))};
}'''
EDITOR = "() => document.querySelector('.phrase-modal textarea')?.value"

def read(page):
    from job_agent.platforms.zhilian.pages import PlatformStateError
    data=page.evaluate(READ)
    if (not isinstance(data,dict) or not data.get('ready') or data.get('login') or data.get('risk')
            or data.get('origin')!='https://i.zhaopin.com' or data.get('path')!='/im/greeting/setting'):
        raise PlatformStateError('默认招呼语设置未就绪或登录/风控异常')
    rows=data.get('rows',[])
    if any(not isinstance(r.get('text'),str) or type(r.get('id')) is not int for r in rows):
        raise PlatformStateError('默认招呼语列表结构改变')
    selected=[r for r in rows if r.get('selected')]
    if len(selected)!=1:
        raise PlatformStateError('默认招呼语不唯一')
    return selected[0]['text'],rows

def synchronize(page, text, confirm=False):
    current,rows=read(page)
    result={'current':current,'desired':text,'different':current!=text,'changed':False,'verified':current==text}
    if current==text or not confirm: return result
    if type(confirm) is not bool: raise ValueError('confirm must be boolean')
    if not text.strip(): raise ValueError('招呼语不能为空')
    matches=[r for r in rows if r['text']==text]
    if not matches:
        if len(text.encode('utf-16-le'))//2>200:
            raise ValueError('智联设置编辑器上限200 UTF-16单位；拒绝截断')
        page.click(ADD)
        page.fill(INPUT,text,newline='insert_text')
        if page.evaluate(EDITOR)!=text: raise ValueError('编辑器回读不一致，未保存')
        page.click(SAVE)  # never replay an uncertain save
        page.goto(URL,wait_ms=15000)
        page.wait_for('.greeting-left-nav__item')
        page.click(NAV)
        page.wait_for('.greeting-list__item')
        _,rows=read(page)
        matches=[r for r in rows if r['text']==text]
    if len(matches)!=1: raise ValueError('目标招呼语未唯一命中；不重复保存')
    page.click('#greeting-item-'+str(matches[0]['id'])+' .greeting-list__default')
    page.goto(URL,wait_ms=15000)
    page.wait_for('.greeting-left-nav__item')
    page.click(NAV)
    page.wait_for('.greeting-list__item')
    actual,_=read(page)
    if actual!=text: raise ValueError('站内默认招呼语写后回读失败；不自动重试')
    return {**result,'changed':True,'verified':True}
