"""Liepin DOM projections and pure parsers. React fields are private, fail closed on drift."""
from platforms.signals import fields as signal_fields

import math
from platforms.base import ReplyEvent

RECORD_URL = 'https://c.liepin.com/job/record/apply'
JOB_READY = '.apply-box'
ENTRY = '.im-ui-basic-entry'
CONTACTS = '#im-basic-contact .im-ui-contact-list-item'
GREETING_NEWLINE = 'reject'  # Not live-verified; prepare_greeting uses configured separator.
EDITOR = 'textarea.im-ui-textarea'
CHAT = '.apply-box a[data-selector="chat-chat"]'
MESSAGE = '.im-ui-message-item-wrapper'

class Unverified(RuntimeError):
    code = 'receipt_unknown'

class LoginRequired(Unverified):
    code = 'login_required'

class RiskControl(Unverified):
    code = 'risk_control'

# Only visible challenge UI counts. Ordinary anti-fraud advice is not a challenge.
GUARD_JS = r'''() => { /* liepin.guard */
 const visible=e=>!!e.getClientRects().length;
 const dialogs=[...document.querySelectorAll('[role="dialog"],.geetest_panel,.nc-container')].filter(visible).map(e=>e.innerText).join('\n');
 return {url:location.href, login:!![...document.querySelectorAll('iframe[src*="passport"],.login-dialog')].find(visible),
 risk:/滑动验证|安全验证|操作频繁|访问异常|账号异常|验证码/.test(dialogs)};
}'''
JOB_JS = r'''() => { /* liepin.job */
 return {controls:[...document.querySelectorAll('.apply-box [data-selector]')].map(e=>({text:e.innerText.trim(),attributes:{'data-selector':e.getAttribute('data-selector'),'data-jobid':e.getAttribute('data-jobid')}}))};
}'''
RECORDS_JS = r'''() => { /* liepin.records */
 return [...document.querySelectorAll('a[href*="/job/"],a[href*="/lptjob/"]')].map(e=>({url:e.href,text:e.closest('li')?.innerText||''}));
}'''
CONTACTS_JS = r'''() => { /* liepin.contacts */
 const contact=e=>{let f=e[Object.keys(e).find(k=>/^__react(Fiber|InternalInstance)/.test(k))];for(let i=0;f&&i<15;i++,f=f.return)if(f.memoizedProps?.contact)return f.memoizedProps.contact;return null};
 return [...document.querySelectorAll('#im-basic-contact .im-ui-contact-list-item')].map((e,index)=>{
 const c=contact(e);return {index,id:c?.id,lastMsgId:c?.lastMsgId,
 lastTime:c?.lastMsgTime,unreadCount:c?.unreadCount};});
}'''
MESSAGES_JS = r'''() => { /* liepin.messages */
 const props=(e,key)=>{let f=e?.[Object.keys(e).find(k=>/^__react(Fiber|InternalInstance)/.test(k))];for(let i=0;f&&i<20;i++,f=f.return)if(f.memoizedProps?.[key])return f.memoizedProps[key];return null};
 const root=document.querySelector('.im-ui-message-list-wrapper');
 const c=props(root,'contact');
 const items=[...document.querySelectorAll('.im-ui-message-item-wrapper')].map(e=>props(e,'message')).filter(Boolean).map(m=>({msgId:m.msgId,msgTime:m.msgTime,msg:m.msg,direction:m.direction,msgType:m.msgType,extType:m.extType,bsData:m.bsData,bizData:m.extType===221?m.bizData:{},revokeFlag:m.revokeFlag}));
 return {ready:!!root,loading:props(root,'chatLoading')===true,contact:{id:c?.id,company:c?.company},items};
}'''
EDITOR_JS = r'''() => { /* liepin.editor */ return document.querySelector('textarea.im-ui-textarea')?.value; }'''


def conversation_summary(row):
    from platforms.base import ConversationSummary
    if (not row.get('id') or row.get('lastMsgId') is None or str(row['lastMsgId']) == ''
            or type(row.get('unreadCount')) is not int or row['unreadCount'] < 0):
        raise Unverified('猎聘联系人消息摘要不可读；不回退为逐个打开')
    stamp = row.get('lastTime')
    stamp = stamp / 1000 if type(stamp) in (int, float) and math.isfinite(stamp) and stamp > 0 else None
    return ConversationSummary(str(row['id']), str(row['lastMsgId']), row['unreadCount'], stamp)


def select_contact(contact_id):
    import json
    if not contact_id:
        raise Unverified('猎聘联系人缺少稳定 ID')
    return '''() => { /* liepin.select_contact */
      const rows=[...document.querySelectorAll('#im-basic-contact .im-ui-contact-list-item')].filter(e=>{
        let f=e[Object.keys(e).find(k=>/^__react(Fiber|InternalInstance)/.test(k))];
        for(let i=0;f&&i<15;i++,f=f.return)if(f.memoizedProps?.contact)
          return String(f.memoizedProps.contact.id)===%s;
        return false;
      });
      if(rows.length!==1)return false;
      const e=rows[0];e.scrollIntoView({block:'center',behavior:'instant'});
      const r=e.getBoundingClientRect();
      if(r.x<0||r.y<0||r.right>innerWidth||r.bottom>innerHeight||r.width<=0||r.height<=0)return false;
      const hit=document.elementFromPoint(r.x+r.width/2,r.y+r.height/2);
      if(!hit||!(e===hit||e.contains(hit)))return false;
      return {x:r.x,y:r.y,width:r.width,height:r.height,viewport:{width:innerWidth,height:innerHeight}};
    }''' % json.dumps(str(contact_id))

def guard(data):
    if not isinstance(data, dict):
        raise Unverified('页面状态不可读')
    if data.get('risk'):
        raise RiskControl('验证码或风控提示，停止平台')
    if data.get('login') or 'passport.' in data.get('url', ''):
        raise LoginRequired('登录失效')


def applied(data):
    if not isinstance(data, dict):
        raise Unverified('猎聘投递状态不可读')
    controls = data.get('controls', [])
    if not isinstance(controls, list) or any(not isinstance(c, dict) for c in controls):
        raise Unverified('猎聘投递控件不可读')
    texts = {c.get('text') for c in controls if isinstance(c.get('text'), str)}
    if '已投递' in texts and '投简历' in texts:
        raise Unverified('猎聘正式投递状态相互矛盾')
    if '已投递' in texts:
        # Positive precheck only; not proof of a specific attachment or current attempt.
        return True
    if '继续聊' in texts:
        # Existing communication blocks a repeat action; it is not an application receipt.
        raise Unverified('猎聘已有沟通，正式申请状态未确认')
    if '投简历' in texts and '聊一聊' in texts:
        return False
    # A chat-only entry does not establish whether a resume was submitted.
    raise Unverified('猎聘正式申请状态未确认')


def utf16_length(text):
    return len(text.encode('utf-16-le')) // 2


def parse_messages(data, *, since=None, job_id='', job_title=''):
    """Only exact job mappings produce events. Never map a contact ID/company to a job."""
    if not isinstance(data, dict) or not data.get('ready'):
        raise Unverified('猎聘聊天正文未就绪')
    items = data.get('items', [])
    recommendation = any(m.get('bizData', {}).get('sourceFlag') == 3 for m in items)
    def is_system(m):
        ai = m.get('bsData')
        if isinstance(ai, str):
            import json
            try:
                ai = json.loads(ai)
            except (ValueError, TypeError):
                ai = {}
        return (recommendation or m.get('extType') != 1 or m.get('msgType') != 'txt'
                or bool(m.get('revokeFlag')) or (isinstance(ai, dict) and bool(ai.get('aiMark'))))
    ids = {str(m.get('bizData', {}).get('communicateJobId')) for m in items
           if m.get('bizData', {}).get('communicateJobId')}
    if not job_id:
        if len(ids) != 1:
            if any(str(m.get('direction')) == '1' and not is_system(m) for m in items):
                raise Unverified('猎聘收件缺少唯一岗位映射')
            return []
        job_id = ids.pop()
    result = []
    for m in items:
        mid, body, ts = m.get('msgId'), m.get('msg'), m.get('msgTime')
        if not mid or not isinstance(body, str) or not body or not isinstance(ts, (int, float)) or not math.isfinite(ts) or ts <= 0:
            continue
        ts /= 1000
        if since is not None and ts <= since:
            continue
        system = is_system(m)
        direction = 'system' if system else {'1':'incoming','0':'outgoing'}.get(str(m.get('direction')))
        if direction is None:
            continue
        result.append(ReplyEvent('liepin', str(mid), str(job_id), data.get('contact', {}).get('company', ''),
                                 job_title, direction, body, ts, RECORD_URL))
    return result


def messages_for(contact_id):
    """Wait for the selected contact, never consume the preceding conversation."""
    import json
    if not contact_id:
        raise Unverified('猎聘联系人缺少稳定 ID')
    return """async () => { const read = %s;
      for(let i=0;i<40;i++) {const d=read();
        if(d.ready && !d.loading && String(d.contact.id)===%s)return d;
        await new Promise(r=>setTimeout(r,250));
      } return {ready:false}; }""" % (MESSAGES_JS, json.dumps(str(contact_id)))

# ---- Read-only collection (#15) ----
from urllib.parse import urlencode, urlsplit, urlunsplit
from browser.base import BrowserError
from platforms.base import JobListing, SearchQuery

class CollectionError(BrowserError):
    code = 'page_structure'

class CollectionLoginRequired(CollectionError):
    code = 'login_required'

class CollectionRiskControl(CollectionError):
    code = 'risk_control'


def collection_guard(data):
    if not isinstance(data, dict):
        raise CollectionError('采集页面不可读')
    if data.get('risk'):
        raise CollectionRiskControl('验证码或风控，停止平台')
    if data.get('login') or data.get('loggedIn') is False:
        raise CollectionLoginRequired('登录失效，停止平台')
    if data.get('loggedIn') is not True or data.get('ready') is not True:
        raise CollectionError('采集页面未就绪或结构改变')


def search_url(query, city_codes=None):
    codes = {**CITY_CODES, **(city_codes or {})}
    city = query.city.strip().removesuffix('市')
    if city not in codes:
        raise ValueError('未支持的城市: ' + city)
    if type(query.page) is not int or query.page < 1 or not query.keyword.strip():
        raise ValueError('关键词不能为空，页码从 1 开始')
    return SEARCH_URL + '?' + urlencode(SEARCH_PARAMS(query, codes[city]))


def clean_job_url(url):
    parts = urlsplit(url)
    if parts.scheme != 'https' or parts.hostname not in JOB_HOSTS or parts.username or parts.password:
        raise CollectionError('岗位链接不属于当前平台')
    import re
    if not re.fullmatch(JOB_PATH, parts.path):
        raise CollectionError('岗位链接路径不可识别')
    return urlunsplit((parts.scheme, parts.netloc, parts.path, '', ''))


def parse_search(data, query):
    collection_guard(data)
    if data.get('page') != query.page:
        raise CollectionError('搜索页码未匹配，停止以免重复采集')
    items = data.get('items')
    if not isinstance(items, list):
        raise CollectionError('岗位列表不可读')
    if not items:
        if data.get('empty') is True:
            return []
        raise CollectionError('缺少明确空列表标志')
    result, seen = [], set()
    for row in items:
        if not isinstance(row, dict) or not all(isinstance(row.get(k),str) and row[k].strip() for k in ('id','url','title','company')):
            raise CollectionError('岗位缺少稳定 ID、链接、标题或公司')
        if not row['id'].isdigit():
            raise CollectionError('岗位 ID 不可识别')
        url = clean_job_url(row['url'])
        if row['id'] in seen:
            continue
        seen.add(row['id'])
        result.append(JobListing(PLATFORM,row['id'],url,row['company'],row['title'],row.get('city',''),row.get('salary',''),
                                 {k:row[k] for k in ('experience','education') if row.get(k)}, **signal_fields(row)))
    return result


def parse_detail(data, listing):
    collection_guard(data)
    if str(data.get('id','')) != listing.platform_job_id:
        raise CollectionError('详情岗位 ID 不匹配')
    jd = data.get('jd')
    if not isinstance(jd,str) or not jd.strip():
        raise CollectionError('JD 正文未取得')
    return jd

PLATFORM = 'liepin'
CITY_CODES = {'上海': '020', '苏州': '060080', '杭州': '070020', '南京': '060020', '合肥': '080020'}
SEARCH_URL = 'https://www.liepin.com/zhaopin/'
SEARCH_PARAMS = lambda q,c: dict(key=q.keyword,dqs=c,currentPage=q.page-1)
JOB_HOSTS = {'www.liepin.com','wow.liepin.com'}
JOB_PATH = r'/(?:(?:job|a)/\d+\.shtml|lptjob/\d+)'

SEARCH_JS = r'''(async () => { /* collect.liepin.search */ const read=()=>{
 const visible=e=>!!e.getClientRects().length;
 const text=document.body?.innerText||'';
 const challenge=[...document.querySelectorAll('[role="dialog"],.geetest_panel,.nc-container,[class*="captcha"],iframe[src*="captcha"]')].filter(visible);
 const risk=challenge.some(e=>/验证|访问异常|操作频繁/.test(e.innerText)||e.matches('.geetest_panel,.nc-container,[class*="captcha"],iframe')) || /^(安全验证|访问异常|访问过于频繁|请完成安全验证|人机验证)/.test(text.trim()) || /captcha|\/verify/.test(location.pathname);
 const login=/passport|\/login/.test(location.href)||!![...document.querySelectorAll('iframe[src*="passport"],.login-dialog')].find(visible);

 const loggedIn=!!document.querySelector('.header-quick-menu-username');
 const anchors=[...document.querySelectorAll('a[data-nick="job-detail-job-info"]')];
 const items=anchors.map(a=>{const box=a.closest('.jobDetailBox');const lines=a.innerText.split('\n').map(x=>x.trim()).filter(Boolean);const end=lines.indexOf('】');
 const cid=new URLSearchParams(a.getAttribute('data-tlg-scm')||'').get('cid')||'';
 return {id:a.getAttribute('data-jobid')||cid.split('_')[1]||'',url:a.href.split('?')[0],title:a.querySelector('.jobTitleBox .ellipsis-1')?.innerText||'',company:box?.querySelector('[data-nick="job-detail-company-info"] .companyName')?.innerText||'',city:lines[2]||'',salary:lines[end+1]||'',hr_activity_raw:box?.querySelector('.recruiter-container .active-time')?.innerText?.trim()||'',experience:lines[end+2]||'',education:lines[lines.length-1]||''}});
 const empty=/没有找到|暂无满足|未找到相关|暂无相关职位/.test(text) && !anchors.length;
 const page=Number(document.querySelector('.ant-pagination-item-active')?.title)|| (empty?Number(new URLSearchParams(location.search).get('currentPage')||0)+1:0);
 return {ready:items.length>0||empty,loggedIn,login,risk,page,empty,items};
}; let d; for(let i=0;i<40;i++){d=read(); if(d.risk||d.login||(d.ready&&d.loggedIn))return d; await new Promise(r=>setTimeout(r,250));} return d; })()'''
DETAIL_JS = r'''(async () => { /* collect.liepin.detail */ const read=()=>{
 const visible=e=>!!e.getClientRects().length;
 const text=document.body?.innerText||'';
 const challenge=[...document.querySelectorAll('[role="dialog"],.geetest_panel,.nc-container,[class*="captcha"],iframe[src*="captcha"]')].filter(visible);
 const risk=challenge.some(e=>/验证|访问异常|操作频繁/.test(e.innerText)||e.matches('.geetest_panel,.nc-container,[class*="captcha"],iframe')) || /^(安全验证|访问异常|访问过于频繁|请完成安全验证|人机验证)/.test(text.trim()) || /captcha|\/verify/.test(location.pathname);
 const login=/passport|\/login/.test(location.href)||!![...document.querySelectorAll('iframe[src*="passport"],.login-dialog')].find(visible);

 const e=document.querySelector('.job-intro-container [data-selector="job-intro-content"]')||document.querySelector('.job-intro-container');
 return {ready:!!e,loggedIn:!!document.querySelector('.header-quick-menu-username'),login,risk,id:document.querySelector('.apply-box [data-jobid]')?.getAttribute('data-jobid'),jd:e?.innerText};
}; let d; for(let i=0;i<40;i++){d=read(); if(d.risk||d.login||(d.ready&&d.loggedIn))return d; await new Promise(r=>setTimeout(r,250));} return d; })()'''
