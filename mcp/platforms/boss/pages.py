"""页面投影与纯解析。所有选择器集中于此；不调用浏览器或数据库。"""
from platforms.signals import fields as signal_fields

from browser.base import BrowserError
from platforms.base import JobListing, ReplyEvent
from platforms.greeting import same_text

class PlatformStateError(RuntimeError):
    code = "receipt_unknown"


class JobUnavailable(PlatformStateError):
    code = "job_unavailable"


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
    if data.get("unavailable"):
        raise JobUnavailable("原站明确显示岗位不存在")


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

NEW_LABEL = "立即沟通"
JOB_HOSTS = {"www.zhipin.com"}
JOB_PATH = r"/job_detail/([^/]+)\.html"
CHAT_URL = "https://www.zhipin.com/web/geek/chat"
JOB_READY = ".job-banner .btn-startchat"
START = ".job-banner .btn-startchat"
GREETING_NEWLINE = 'shift_enter'  # #59: live keyboard handler + input/readback only.
CONTACT_NEWLINE = 'insert_text'  # Native textarea; no Enter key event.
INPUT = "#chat-input[contenteditable=true]"
SEND = "Enter"
LIST = '[role=listitem]'
MESSAGE_READY = ".message-item"

# Projections contain no cookies, tokens, securityId or recruiter names.
STATE_JS = '''() => { /* boss.state */
 const visible=e=>!!e && !!e.getClientRects().length && getComputedStyle(e).visibility!=='hidden';
 const alerts=[...document.querySelectorAll('[role=dialog], [role=alert], .dialog-wrap, .toast, .verify-wrap')].filter(visible).map(e=>e.innerText).join(' ');
 return {unavailable:document.title==='您访问的页面不存在-BOSS直聘' && (document.body?.innerText||'').includes('您访问的页面不存在'),
 buttons:[...document.querySelectorAll('.job-banner .btn-startchat')].filter(visible).map(e=>({text:e.innerText.trim()})),
 login_required:/passport|login/.test(location.pathname)||[...document.querySelectorAll('.login-dialog, .sign-form, input[type=password]')].some(visible),
 risk:/验证码|安全验证|操作频繁|访问异常|访问受限|异常行为|请完成验证|今日沟通.*上限/.test(alerts)};
}'''
LIST_JS = '''() => { /* boss.list */
 return {rows:[...document.querySelectorAll('[role=listitem]')].map(e=>{
 const s=e.__vue__?.$props?.source;if(!s)return null;
 return {encryptJobId:s.encryptJobId,jobId:s.jobId,uniqueId:s.uniqueId,brandName:s.brandName,jobName:s.title||s.jobName,
 lastMsgId:String(s.lastMsgId??''),unreadCount:s.unreadCount,lastIsSelf:s.lastIsSelf,lastTS:s.lastTS};}).filter(Boolean)};
}'''
MESSAGES_JS = '''() => { /* boss.messages */
 const s=document.querySelector('[role=listitem] .selected')?.closest('[role=listitem]')?.__vue__?.$props?.source;
 return {current_job_id:s?.encryptJobId,current_conversation_id:s?.uniqueId,rows:[...document.querySelectorAll('.message-item[data-mid]')].map(e=>({
 id:e.getAttribute('data-mid'),job_id:s?.encryptJobId,company:s?.brandName,job_title:s?.title||s?.jobName,
 direction:e.classList.contains('item-myself')?'outgoing':e.classList.contains('item-friend')?'incoming':'system',
 body:e.querySelector('.text-content')?.textContent||'',card:!!e.querySelector('.message-card-wrap'),
 receipt:e.querySelector('.message-status')?.textContent?.trim(),
 time:e.__vue__?.$props?.message?.time ?? (String(s?.lastMsgId)===e.getAttribute('data-mid')?s?.lastTS:null),
 conversation_id:s?.uniqueId}))};
}'''
INPUT_JS = "() => document.querySelector('#chat-input')?.innerText"


def conversation_summary(row):
    from platforms.base import ConversationSummary
    if (not row.get('uniqueId') or not row.get('encryptJobId')
            or type(row.get('lastMsgId')) not in (str, int) or str(row['lastMsgId']) == ''
            or type(row.get('unreadCount')) is not int or row['unreadCount'] < 0):
        raise PlatformStateError('BOSS 会话摘要缺少稳定身份或未读数')
    return ConversationSummary(str(row['uniqueId']), str(row['lastMsgId']), row['unreadCount'], epoch(row.get('lastTS')))


def is_chat_url(url):
    from urllib.parse import urlsplit
    try:
        u=urlsplit(url)
        return (u.scheme=='https' and u.hostname=='www.zhipin.com' and u.port in (None,443)
                and u.username is None and u.password is None and u.path=='/web/geek/chat')
    except (ValueError,TypeError):
        return False


# The reference also has an ordinary visible editable first-contact form.
# Use only its visible input and explicit, unambiguous submit control.
CONTACT_DIALOG = '.dialog-wrap.startchat-dialog'
CONTACT_INPUT = CONTACT_DIALOG+' textarea.input-area'
CONTACT_SUBMIT_SELECTORS = tuple(CONTACT_DIALOG+' '+s for s in (
    '.send-message', '[ka="dialog_confirm"]', '.btn-sure', '.btn-send', '.send-btn',
    'button', '[role="button"]'))
CONTACT_SUBMIT_LABELS = ('发送','确定','确认','发送消息','发送招呼语','开始沟通','立即沟通','确认发送')

import json as _json
CONTACT_STATE_JS = r"""() => { /* boss.contact_state */
 const visible=e=>!!e && !!e.getClientRects().length && getComputedStyle(e).visibility!=='hidden'
     && getComputedStyle(e).display!=='none' && getComputedStyle(e).pointerEvents!=='none';
 const enabled=e=>visible(e) && !e.disabled && e.getAttribute('aria-disabled')!=='true'
     && !e.classList.contains('disabled');
 const preset=[...document.querySelectorAll('.greet-boss-pop, .greet-pop')].filter(visible);
 const editable=[...document.querySelectorAll('.dialog-wrap.startchat-dialog')].filter(e=>visible(e));
 const other=[...document.querySelectorAll('.dialog-wrap, [role=dialog]')].filter(visible);
 if(preset.length) return {kind:'preset_greeting'};
 if(editable.length!==1) return {kind:other.length?'unknown_dialog':'none'};
 const dialog=editable[0];
 if(other.some(e=>e!==dialog && !dialog.contains(e) && !e.contains(dialog)))
     return {kind:'unknown_dialog'};
 const inputs=[...document.querySelectorAll('__INPUT__')];
 const input=inputs.length===1 && enabled(inputs[0]) && !inputs[0].readOnly ? inputs[0]:null;
 const selectors=__SELECTORS__, labels=__LABELS__;
 const label=e=>(e.innerText||e.textContent||'').trim();
 const buttons=[...new Set(selectors.flatMap(s=>[...document.querySelectorAll(s)]))]
     .filter(e=>dialog.contains(e) && visible(e) && labels.includes(label(e)));
 const button=buttons.length===1 && enabled(buttons[0])?buttons[0]:null;
 const selector=button?selectors.find(s=>{
     const found=document.querySelectorAll(s); return found.length===1 && found[0]===button;
 }):null;
 return {kind:'editable_contact',input_ready:!!input,value:input?.value??null,
     submit_selector:selector??null,submit_label:button?label(button):null};
}""".replace('__INPUT__',CONTACT_INPUT).replace('__SELECTORS__',_json.dumps(CONTACT_SUBMIT_SELECTORS)
    ).replace('__LABELS__',_json.dumps(CONTACT_SUBMIT_LABELS,ensure_ascii=False))


def contact_submit_selector(state, greeting):
    if (not isinstance(state,dict) or state.get('kind')!='editable_contact'
            or state.get('input_ready') is not True):
        raise PlatformStateError('首次沟通编辑框不可核验')
    if state.get('value')!=greeting:
        raise PlatformStateError('首次沟通招呼语全文读回不匹配')
    if (state.get('submit_label') not in CONTACT_SUBMIT_LABELS
            or state.get('submit_selector') not in CONTACT_SUBMIT_SELECTORS):
        raise PlatformStateError('首次沟通明确且唯一的提交控件不可核验')
    return state['submit_selector']


CHAT_READY_JS = r"""() => { /* boss.chat_ready */
 const visible=e=>!!e && !!e.getClientRects().length && getComputedStyle(e).visibility!=='hidden';
 const input=document.querySelector('#chat-input');
 const messages=[...document.querySelectorAll('.message-item')].filter(visible);
 return {root:!!document.querySelector('.chat-conversation'),
   editor:!!(visible(input) && input.getAttribute('contenteditable')==='true'),
   input_empty:!!input && !input.innerText.trim(),
   loading:[...document.querySelectorAll('.loading, .chat-loading, .loading-wrap')].some(visible),
   visible_messages:messages.length};
}"""


def first_message_ready(state, data, job_id, conversation_id):
    """Positive, exact and fully projected visible chat; not just an available input."""
    guard(data)
    if (not isinstance(state,dict) or not state.get('root') or not state.get('editor')
            or state.get('input_empty') is not True or state.get('loading') is not False
            or not conversation_id or data.get('current_job_id')!=job_id
            or not isinstance(data.get('rows'),list) or state.get('visible_messages')!=len(data['rows'])):
        return False
    return no_outgoing_message(data,job_id,conversation_id)


def no_outgoing_message(data,job_id,conversation_id):
    """Re-check just before the send; a late default/pending message blocks it."""
    guard(data)
    if (not conversation_id or data.get('current_job_id')!=job_id
            or not isinstance(data.get('rows'),list)):
        return False
    return all(isinstance(r,dict) and r.get('id') and r.get('job_id')==job_id
               and r.get('conversation_id')==conversation_id
               and r.get('direction') in {'incoming','system'} for r in data['rows'])


def session_selector(index):
    return f'{LIST} >> nth={index}'


def events(data, since=None):
    guard(data)
    result, seen = [], set()
    for r in data.get('rows', []):
        stamp = epoch(r.get('time'))
        mid, jid = str(r.get('id') or ''), r.get('job_id')
        if not mid or not jid or stamp is None or (since is not None and stamp <= since):
            continue
        key = (jid, r.get('conversation_id'), mid)
        if key in seen:
            continue
        seen.add(key)
        direction = 'system' if r.get('card') or not r.get('body') else r.get('direction', 'system')
        result.append(ReplyEvent('boss', f"{r.get('conversation_id') or jid}:{mid}", jid,
                     r.get('company') or '', r.get('job_title') or '', direction,
                     r.get('body') or '', stamp, CHAT_URL))
    return result


def receipt(data, greeting, jid, before):
    guard(data)
    return any(r.get('job_id') == jid and r.get('id') not in before and r.get('id')
               and r.get('direction') == 'outgoing' and not r.get('card')
               and same_text(r.get('body'), greeting) and r.get('receipt') in ('送达', '已读')
               for r in data.get('rows', []))


def fresh_greeting_receipt(data, job_id, conversation_id, greeting, attempted_at, checked_at):
    """Read-only audit of a known existing conversation; old or ambiguous text is not proof."""
    import math
    guard(data)
    if (not greeting or not conversation_id or type(attempted_at) not in (int,float)
            or not math.isfinite(attempted_at)):
        return False
    for row in data.get('rows', []):
        stamp=epoch(row.get('time'))
        if (row.get('job_id')==job_id and row.get('conversation_id')==conversation_id
                and row.get('id') and row.get('direction')=='outgoing' and not row.get('card')
                and same_text(row.get('body'), greeting) and row.get('receipt') in ('送达','已读')
                and stamp is not None and math.floor(attempted_at)<=stamp<=math.ceil(checked_at)):
            return True
    return False


def message_ids(data):
    guard(data)
    return {r.get('id') for r in data.get('rows', []) if r.get('id')}


def session_ready(row):
    import re
    mid = str(row.get('lastMsgId') or '')
    if not re.fullmatch(r'[A-Za-z0-9_-]+', mid):
        raise PlatformStateError('列表缺少稳定消息 ID')
    return f'.message-item[data-mid="{mid}"]'


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

PLATFORM = 'boss'
CITY_CODES = {'上海': '101020100', '苏州': '101190400', '杭州': '101210100', '南京': '101190100'}
SEARCH_BASE = 'https://www.zhipin.com/web/geek/jobs?'
CITY_PARAM, KEYWORD_PARAM = 'city', 'query'
JOB_ORIGIN = 'https://www.zhipin.com'
JOB_CANONICAL = '/job_detail/{jid}.html'
SEARCH_CARD = '.job-card-wrap'
PAGE_SIZE = 15

def salary_text(raw, plain=''):
    import re
    value = plain or raw
    if not isinstance(value, str) or re.search(r'[\ue000-\uf8ff\U000f0000-\U0010ffff]', value):
        raise CollectionError('薪资字体无法解码，停止采集')
    return value

SEARCH_JS = r'''(() => { /* boss.collect.search */
 const visible=e=>!!e && !!e.getClientRects().length && getComputedStyle(e).visibility!=='hidden';
 const alerts=[...document.querySelectorAll('[role=dialog], [role=alert], .dialog-wrap, .a-dialog, .verify-wrap, .geetest_panel, .nc-container')].filter(visible).map(e=>e.innerText).join(' ');
 const text=document.body?.innerText||'';
 const challenge=[...document.querySelectorAll('.geetest_panel, .nc-container, iframe[src*=captcha], iframe[src*=verify]')].some(visible);
 const risk=challenge||/captcha|verify|security-check|\/403/.test(location.pathname)||/验证码|安全验证|操作频繁|访问异常|访问受限|异常行为|请完成验证/.test(alerts+(text.length<500?text:''));
 const login_required=/passport|login/.test(location.hostname+location.pathname)||[...document.querySelectorAll('.login-dialog, .sign-form, .login-modal, input[type=password]')].some(visible);

 const cards=[...document.querySelectorAll('.job-card-wrap')];
 const parent=cards[0]?.__vue__?.$parent||document.querySelector('.page-jobs')?.__vue__;
 const rows=cards.map(e=>{const d=e.__vue__?.$props?.data||{};
 const url=e.querySelector('.job-name')?.href||'';
 return {id:d.encryptJobId,url,title:e.querySelector('.job-name')?.textContent.trim(),
 company:d.brandName,city:d.cityName,salary:e.querySelector('.job-salary')?.textContent.trim()||'',
 salary_plain:d.salaryDesc||'',hr_activity_raw:d.bossOnline===true?'在线':e.querySelector('.boss-online, .active-time')?.textContent?.trim()||'',experience:d.jobExperience,education:d.jobDegree,company_size:d.brandScaleName};});
 return {risk,login_required,ready:!!parent && !parent.loading && !parent.moreLoading && typeof parent.hasMore==='boolean',has_more:parent?.hasMore,rows};
})()'''

DETAIL_JS = r'''(() => { /* boss.collect.detail */
 const visible=e=>!!e && !!e.getClientRects().length && getComputedStyle(e).visibility!=='hidden';
 const alerts=[...document.querySelectorAll('[role=dialog], [role=alert], .dialog-wrap, .a-dialog, .verify-wrap, .geetest_panel, .nc-container')].filter(visible).map(e=>e.innerText).join(' ');
 const text=document.body?.innerText||'';
 const challenge=[...document.querySelectorAll('.geetest_panel, .nc-container, iframe[src*=captcha], iframe[src*=verify]')].some(visible);
 const risk=challenge||/captcha|verify|security-check|\/403/.test(location.pathname)||/验证码|安全验证|操作频繁|访问异常|访问受限|异常行为|请完成验证/.test(alerts+(text.length<500?text:''));
 const login_required=/passport|login/.test(location.hostname+location.pathname)||[...document.querySelectorAll('.login-dialog, .sign-form, .login-modal, input[type=password]')].some(visible);
const match=location.pathname.match(/\/job_detail\/([^/]+)\.html/);
 return {risk,login_required,id:match?.[1],jd:document.querySelector('.job-sec-text')?.innerText||''};})()'''

SCROLL_JS = r'''(() => { /* collect.scroll */ const rows=document.querySelectorAll('.job-card-wrap'); rows[rows.length-1]?.scrollIntoView({block:'end'}); return null; })()'''

JOB_IDENTITY_JS = """() => { /* boss.identity */
 return {title:document.querySelector('.job-banner h1')?.innerText.trim(),
 company:[...document.querySelectorAll('.company-info a')].map(e=>e.innerText.trim()).find(Boolean)};
}"""
CHAT_IDENTITY_JS = """() => { /* boss.chat_identity */
 const s=document.querySelector('[role=listitem] .selected')?.closest('[role=listitem]')?.__vue__?.$props?.source;
 return {job_id:s?.encryptJobId,conversation_id:s?.uniqueId,company:s?.brandName,title:s?.jobName,
 header_title:document.querySelector('.position-name')?.innerText.trim()};
}"""

def matching_chat(target, current, jid):
    return (isinstance(target, dict) and isinstance(current, dict)
            and bool(target.get('company')) and bool(target.get('title'))
            and current.get('job_id') == jid and current.get('company') == target['company']
            and current.get('title') == target['title'] and current.get('header_title') == target['title'])


def select_session(row):
    """Select an existing conversation by stable ID, avoiding virtual-list row reuse.

    Scroll the stable row into view, then resolve it again in case Vue recycled
    the virtual-list element. The driver rechecks geometry before trusted input.
    """
    import json
    if not row.get('uniqueId') or not row.get('encryptJobId'):
        raise PlatformStateError('会话缺少稳定 ID')
    return """() => { /* boss.select_session */
      const find=()=>[...document.querySelectorAll('[role=listitem]')].filter(e=>{
        const s=e.__vue__?.$props?.source;
        return s?.uniqueId===%s && s?.encryptJobId===%s;
      });
      let matches=find();
      if(matches.length!==1)return false;
      let target=matches[0].querySelector('.friend-content');
      if(!target)return false;
      const initial=target.getBoundingClientRect();
      if(initial.y<0||initial.bottom>innerHeight) {
        target.scrollIntoView({block:'center',inline:'nearest',behavior:'instant'});
        matches=find();
        if(matches.length!==1)return false;
        target=matches[0].querySelector('.friend-content');
        if(!target)return false;
      }
      const r=target.getBoundingClientRect();
      if(r.x<0||r.y<0||r.right>innerWidth||r.bottom>innerHeight||r.width<=0||r.height<=0)return null;
      const hit=document.elementFromPoint(r.x+r.width/2,r.y+r.height/2);
      if(!hit||!(target===hit||target.contains(hit)))return null;
      return {x:r.x,y:r.y,width:r.width,height:r.height,viewport:{width:innerWidth,height:innerHeight}};
    }""" % (json.dumps(row['uniqueId']), json.dumps(row['encryptJobId']))
