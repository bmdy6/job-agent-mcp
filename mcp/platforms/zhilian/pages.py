"""页面投影与纯解析。所有选择器集中于此；不调用浏览器或数据库。"""
from platforms.signals import fields as signal_fields

from browser.base import BrowserError
from platforms.base import JobListing, ReplyEvent
from platforms.greeting import same_text

class PlatformStateError(RuntimeError):
    code = "receipt_unknown"


class LoginRequired(PlatformStateError):
    code = "login_required"


class RiskControl(PlatformStateError):
    code = "risk_control"


def guard(data):
    if not isinstance(data, dict):
        raise PlatformStateError("页面结构不可读")
    if data.get("risk"):
        raise RiskControl("验证码或风控提示，停止平台")
    if data.get("login_required"):
        raise LoginRequired("登录失效")


def utf16_length(text):
    return len(text.encode("utf-16-le")) // 2


def applied(data):
    guard(data)
    labels = {b["text"].strip() for b in data.get("buttons", [])}
    if labels & {"继续沟通", "已投递", "已沟通"}:
        return True
    if labels == {NEW_LABEL}:
        return False
    raise PlatformStateError("未能明确判断是否已投")


def job_id(url):
    from urllib.parse import urlsplit
    import re
    u = urlsplit(url)
    if u.scheme != "https" or u.hostname not in JOB_HOSTS:
        raise ValueError("非平台岗位 URL")
    m = re.fullmatch(JOB_PATH, u.path)
    if not m:
        raise ValueError("岗位 URL 结构不匹配")
    return m[1]


def epoch(value):
    # Only numeric epoch values are accepted, never fabricate dates from HH:MM.
    import math
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    seconds = value / 1000 if value > 100000000000 else value
    return seconds if 946684800 <= seconds < 4102444800 else None

NEW_LABEL = "立即投递"
JOB_HOSTS = {"www.zhaopin.com", "jobs.zhaopin.com"}
JOB_PATH = r"/(?:jobdetail/)?([^/]+)\.html?"
CHAT_URL = "https://i.zhaopin.com/im"
JOB_READY = ".summary-planes__action button"
START = JOB_READY
INPUT = ".im-sender__input"
LIST = ".im-session-item"
MESSAGE_READY = ".im-message"
GREETING_NEWLINE = 'reject'  # platform_default; no custom composer input.
CONTINUE = ".deliver-greeting-modal__btn--primary"
SUCCESS_MODAL = ".deliver-greeting-modal__title"
STATE_JS = '''() => { /* zhilian.state */
 const visible=e=>!!e && !!e.getClientRects().length && getComputedStyle(e).visibility!=='hidden';
 const alerts=[...document.querySelectorAll('[role=dialog], [role=alert], .a-dialog, .a-message, .verify-wrap')].filter(visible).map(e=>e.innerText).join(' ');
 return {buttons:[...document.querySelectorAll('.summary-planes__action button')].filter(visible).map(e=>({text:e.innerText.trim()})),
 login_required:/login|passport/.test(location.hostname+location.pathname)||[...document.querySelectorAll('.login-modal, input[type=password]')].some(visible),
 risk:/验证码|安全验证|操作频繁|访问异常|访问受限|异常行为|请完成验证|投递.*上限/.test(alerts)};
}'''
APPLICATIONS_URL = "https://i.zhaopin.com/schedule"
APPLICATION_ROWS = ".ji-item"
APPLICATIONS_JS = r"""() => { /* zhilian.application_records */
 const visible=e=>e && !!e.getClientRects().length && getComputedStyle(e).visibility!=='hidden';
 return {origin:location.origin,path:location.pathname,rows:[...document.querySelectorAll('.ji-item')].filter(visible).map(e=>({
 url:e.querySelector('a.ji-item-info-jobName')?.href,
 title:e.querySelector('a.ji-item-info-jobName')?.innerText.trim(),
 company:e.querySelector('.ji-item-info-companyName')?.innerText.trim(),
 status:e.querySelector('.ji-item-status p')?.innerText.trim(),
 time:[...e.querySelectorAll('.ji-item-status-timer span')].map(s=>s.innerText.trim()).join(' ')
 }))};
}"""


def fresh_application_record(data, job, attempted_at, checked_at):
    """Positive original-site application record only; not a greeting/attachment receipt."""
    import math
    from datetime import datetime
    from urllib.parse import urlsplit
    from zoneinfo import ZoneInfo
    import re
    guard(data)
    if (data.get('origin') != 'https://i.zhaopin.com' or data.get('path') != '/schedule'
            or not isinstance(data.get('rows'), list)):
        return None
    if (type(attempted_at) not in (int,float) or not math.isfinite(attempted_at)
            or not job.get('platform_job_id') or not job.get('company') or not job.get('title')):
        return None
    normalize=lambda value: ' '.join(str(value or '').split())
    matches=[]
    for row in data['rows']:
        if not isinstance(row,dict) or row.get('status') not in ('成功投递','投递成功'):
            continue
        try:
            link=urlsplit(row.get('url') or '')
            # The original site uses HTTP and HTTPS links in displayed records.
            # Compare only; never navigate to a record link.
            m=re.fullmatch(JOB_PATH,link.path)
            if link.scheme not in ('http','https') or link.hostname not in JOB_HOSTS or not m or m[1]!=job['platform_job_id']:
                continue
            if normalize(row.get('company'))!=normalize(job['company']) or normalize(row.get('title'))!=normalize(job['title']):
                continue
            stamp=datetime.strptime(row.get('time',''),'%Y-%m-%d %H:%M:%S').replace(tzinfo=ZoneInfo('Asia/Shanghai'))
        except (ValueError,TypeError):
            continue
        if math.floor(attempted_at)<=stamp.timestamp()<=math.ceil(checked_at):
            matches.append(stamp.isoformat())
    return matches[0] if len(matches)==1 else None


LIST_JS = '''() => { /* zhilian.list */
 return {rows:[...document.querySelectorAll('.im-session-item')].map(e=>{
 const s=e.__vue__?.$props?.session;return s?{sessionId:s.sessionId,jobNumber:s.jobNumber,
 unreadCount:s.unreadCount,sendTime:s.sendTime,text:s.text,lastSentenceType:s.lastSentenceType,senderType:s.senderType}:null;}).filter(Boolean)};
}'''
MESSAGES_JS = '''() => { /* zhilian.messages */
 return {messages:[...document.querySelectorAll('.im-message')].map(e=>{
 const p=e.__vue__?.$props,m=p?.msg,s=p?.session;if(!m||!s)return null;
 return {msg:{idServer:m.idServer,fromMe:m.fromMe,flow:m.flow,time:m.time,type:m.type,
 body:typeof m.body==='string'?m.body:null,content:typeof m.content==='string'?m.content:null,sendState:m.sendState},
 session:{sessionId:s.sessionId,jobNumber:s.jobNumber,companyName:s.companyName,jobTitle:s.jobTitle}};
 }).filter(Boolean)};
}'''


def session_selector(index):
    return f'{LIST} >> nth={index}'


def conversation_summary(row):
    import hashlib
    import json
    from platforms.base import ConversationSummary
    # The observed session model has no message ID. Hash only stable message
    # metadata, excluding read/online/relative-time UI state and personal names.
    if (not row.get('sessionId') or not row.get('jobNumber') or row.get('sendTime') is None
            or not isinstance(row.get('text'), str)
            or type(row.get('unreadCount')) is not int or row['unreadCount'] < 0):
        raise PlatformStateError('智联会话摘要不可读')
    content = [row.get(k) for k in ('sendTime', 'text', 'lastSentenceType', 'senderType')]
    fingerprint = 'summary:' + hashlib.sha256(json.dumps(content, ensure_ascii=False).encode()).hexdigest()
    return ConversationSummary(str(row['sessionId']), fingerprint, row['unreadCount'], epoch(row['sendTime']))


def events(data, since=None):
    guard(data)
    result, seen = [], set()
    for r in data.get('messages', []):
        m, s = r.get('msg', {}), r.get('session', {})
        mid, sid, jid = m.get('idServer'), s.get('sessionId'), s.get('jobNumber')
        stamp = epoch(m.get('time'))
        if not mid or not sid or not jid or stamp is None or (since is not None and stamp <= since):
            continue
        key = (sid, mid)
        if key in seen:
            continue
        seen.add(key)
        direction = 'system'
        if m.get('type') == 'text':
            if m.get('fromMe') is True and m.get('flow') == 'out':
                direction = 'outgoing'
            elif m.get('fromMe') is False and m.get('flow') == 'in':
                direction = 'incoming'
        body = m.get('body') or m.get('content') or ''
        if not isinstance(body, str):
            body = ''
        from urllib.parse import urlencode
        result.append(ReplyEvent('zhilian', f'{sid}:{mid}', jid, s.get('companyName') or '',
                     s.get('jobTitle') or '', direction, body, stamp,
                     CHAT_URL + '?' + urlencode({'sessionId': sid})))
    return result


def message_ids(data):
    guard(data)
    return {(r.get('session', {}).get('sessionId'), r.get('msg', {}).get('idServer'))
            for r in data.get('messages', [])}


def receipt(data, greeting, jid, before):
    guard(data)
    for r in data.get('messages', []):
        m, s = r.get('msg', {}), r.get('session', {})
        if ((s.get('sessionId'), m.get('idServer')) not in before and m.get('idServer')
                and s.get('jobNumber') == jid and m.get('type') == 'text'
                and m.get('fromMe') is True and m.get('flow') == 'out'
                and same_text(m.get('body') or m.get('content'), greeting) and m.get('sendState') == 'success'):
            return True
    return False


def select_existing_session(row):
    """Select an existing session by exact stable IDs; never a numeric row of a changing list."""
    import json
    if not row.get('sessionId') or not row.get('jobNumber'):
        raise PlatformStateError('会话缺少稳定身份')
    return """() => { /* zhilian.select_existing_session */
      const matches=[...document.querySelectorAll('.im-session-item')].filter(e=>{
        const s=e.__vue__?.$props?.session;
        return e.getClientRects().length && s?.sessionId===%s && s?.jobNumber===%s;
      });
      if(matches.length!==1)return false;
      matches[0].click();return true;
    }""" % (json.dumps(row['sessionId']),json.dumps(row['jobNumber']))


def select_session(row):
    """Resolve monitor mouse geometry by stable session/job IDs."""
    import json
    if not row.get('sessionId') or not row.get('jobNumber'):
        raise PlatformStateError('会话缺少稳定身份')
    return '''() => { /* zhilian.select_session */
      const matches=[...document.querySelectorAll('.im-session-item')].filter(e=>{
        const s=e.__vue__?.$props?.session;
        return s?.sessionId===%s && s?.jobNumber===%s;
      });
      if(matches.length!==1)return false;
      const e=matches[0];e.scrollIntoView({block:'center',behavior:'instant'});
      const r=e.getBoundingClientRect();
      if(r.x<0||r.y<0||r.right>innerWidth||r.bottom>innerHeight||r.width<=0||r.height<=0)return false;
      const hit=document.elementFromPoint(r.x+r.width/2,r.y+r.height/2);
      if(!hit||!(e===hit||e.contains(hit)))return false;
      return {x:r.x,y:r.y,width:r.width,height:r.height,viewport:{width:innerWidth,height:innerHeight}};
    }''' % (json.dumps(row['sessionId']), json.dumps(row['jobNumber']))


def fresh_greeting_receipt(data, job, session_id, checked_at):
    """An application record alone is never evidence of this exact greeting."""
    import math
    guard(data)
    attempted_at=job.get('attempted_at')
    if (not session_id or not job.get('greeting') or type(attempted_at) not in (int,float)
            or not math.isfinite(attempted_at) or not isinstance(data.get('messages'),list)):
        return False
    for row in data['messages']:
        if not isinstance(row,dict): continue
        m,s=row.get('msg',{}),row.get('session',{})
        stamp=epoch(m.get('time'))
        if s.get('sessionId')!=session_id or stamp is None or not math.floor(attempted_at)<=stamp<=math.ceil(checked_at):
            continue
        if receipt({'messages':[row]},job['greeting'],job['platform_job_id'],set()):
            return True
    return False


# ---- 只读采集（#15 P3）----

class CollectionError(BrowserError):
    code = 'collection_structure'


class CollectionLoginRequired(CollectionError):
    code = 'login_required'


class CollectionRiskControl(CollectionError):
    code = 'risk_control'


def collection_guard(data):
    if not isinstance(data, dict):
        raise CollectionError('采集页面结构不可读')
    if data.get('risk'):
        raise CollectionRiskControl('验证码或风控，停止平台')
    if data.get('login_required'):
        raise CollectionLoginRequired('登录失效，停止平台')


def search_url(query, city_codes=None):
    codes = {**CITY_CODES, **(city_codes or {})}
    from urllib.parse import urlencode
    if type(query.page) is not int or query.page < 1 or not query.keyword.strip():
        raise ValueError('关键词和页码不合法')
    city = query.city.removesuffix('市')
    if city not in codes:
        raise ValueError('未配置的平台城市: ' + city)
    return SEARCH_BASE + urlencode({CITY_PARAM: codes[city], KEYWORD_PARAM: query.keyword})


def search_rows(data):
    collection_guard(data)
    if data.get('ready') is not True or not isinstance(data.get('rows'), list):
        raise CollectionError('列表尚未加载或结构变化')
    if not data['rows'] and data.get('has_more') is not False:
        raise CollectionError('空列表没有到底证据')
    result, seen = [], set()
    for row in data['rows']:
        if not isinstance(row, dict):
            raise CollectionError('岗位结构变化')
        try:
            jid = job_id(row.get('url', ''))
        except (ValueError, TypeError):
            raise CollectionError('缺少稳定岗位链接') from None
        if jid != row.get('id') or not row.get('title') or not row.get('company'):
            raise CollectionError('缺少岗位标识、名称或公司')
        if jid in seen:
            continue
        seen.add(jid)
        salary = salary_text(row.get('salary', ''), row.get('salary_plain', ''))
        result.append(JobListing(PLATFORM, jid, JOB_ORIGIN + JOB_CANONICAL.format(jid=jid),
            row['company'], row['title'], row.get('city', ''), salary,
            {key: row[key] for key in ('experience', 'education', 'company_size') if row.get(key)}, **signal_fields(row)))
    return result


def detail_text(data, expected_id):
    collection_guard(data)
    if data.get('id') != expected_id or not isinstance(data.get('jd'), str) or not data['jd'].strip():
        raise CollectionError('详情岗位不匹配或 JD 尚未加载')
    return data['jd'].strip()

PLATFORM = 'zhilian'
CITY_CODES = {'上海': '538', '苏州': '639', '杭州': '653', '南京': '635'}
SEARCH_BASE = 'https://www.zhaopin.com/jobs/?'
CITY_PARAM, KEYWORD_PARAM = 'jl', 'kw'
JOB_ORIGIN = 'https://www.zhaopin.com'
JOB_CANONICAL = '/jobdetail/{jid}.htm'
SEARCH_CARD = '.job-card'
PAGE_SIZE = 20

def salary_text(raw, plain=''):
    import re
    value = plain or raw
    if not isinstance(value, str) or re.search(r'[\ue000-\uf8ff\U000f0000-\U0010ffff]', value):
        raise CollectionError('薪资字体无法解码，停止采集')
    return value

SEARCH_JS = r'''(() => { /* zhilian.collect.search */
 const visible=e=>!!e && !!e.getClientRects().length && getComputedStyle(e).visibility!=='hidden';
 const alerts=[...document.querySelectorAll('[role=dialog], [role=alert], .dialog-wrap, .a-dialog, .verify-wrap, .geetest_panel, .nc-container')].filter(visible).map(e=>e.innerText).join(' ');
 const text=document.body?.innerText||'';
 const challenge=[...document.querySelectorAll('.geetest_panel, .nc-container, iframe[src*=captcha], iframe[src*=verify]')].some(visible);
 const risk=challenge||/captcha|verify|security-check|\/403/.test(location.pathname)||/验证码|安全验证|操作频繁|访问异常|访问受限|异常行为|请完成验证/.test(alerts+(text.length<500?text:''));
 const login_required=/passport|login/.test(location.hostname+location.pathname)||[...document.querySelectorAll('.login-dialog, .sign-form, .login-modal, input[type=password]')].some(visible);

 const cards=[...document.querySelectorAll('.job-card')];
 const panel=document.querySelector('.job-list-panel')?.__vue__;
 const rows=cards.map(e=>{const d=e.__vue__?.$props?.job||{};
 return {id:d.number,url:d.positionURL||d.positionUrl,title:d.name,company:d.companyName,
 city:e.querySelector('.job-card__location span')?.textContent.trim()||'',
 salary:e.querySelector('.job-card__salary')?.textContent.trim()||'',hr_activity_raw:d.staffCard?.lastOnlineTimeText||d.staffCard?.hrOnlineState||'',experience:d.workingExp,education:d.education,company_size:d.companySize};});
 return {risk,login_required,ready:!!panel && !panel.loading && !panel.loadingMore && typeof panel.hasMore==='boolean',has_more:panel?.hasMore,rows};
})()'''

DETAIL_JS = r'''(() => { /* zhilian.collect.detail */
 const visible=e=>!!e && !!e.getClientRects().length && getComputedStyle(e).visibility!=='hidden';
 const alerts=[...document.querySelectorAll('[role=dialog], [role=alert], .dialog-wrap, .a-dialog, .verify-wrap, .geetest_panel, .nc-container')].filter(visible).map(e=>e.innerText).join(' ');
 const text=document.body?.innerText||'';
 const challenge=[...document.querySelectorAll('.geetest_panel, .nc-container, iframe[src*=captcha], iframe[src*=verify]')].some(visible);
 const risk=challenge||/captcha|verify|security-check|\/403/.test(location.pathname)||/验证码|安全验证|操作频繁|访问异常|访问受限|异常行为|请完成验证/.test(alerts+(text.length<500?text:''));
 const login_required=/passport|login/.test(location.hostname+location.pathname)||[...document.querySelectorAll('.login-dialog, .sign-form, .login-modal, input[type=password]')].some(visible);
const match=location.pathname.match(/\/(?:jobdetail\/)?([^/]+)\.html?/);
 return {risk,login_required,id:match?.[1],jd:document.querySelector('.job-description__content, .describtion__detail-content, .describtion-card__detail-content')?.innerText||''};})()'''

SCROLL_JS = r'''(() => { /* collect.scroll */ const rows=document.querySelectorAll('.job-card'); rows[rows.length-1]?.scrollIntoView({block:'end'}); return null; })()'''
