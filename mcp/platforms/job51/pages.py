"""51job selectors and pure parsing. Attachment rows verified in the site's loaded bundle.

The live dialog has not been submitted in this task. Never infer an attachment
receipt from the parent's applySuccess: it is also displayed on attachment failure.
"""
from platforms.signals import fields as signal_fields

GREETING_NEWLINE = 'reject'  # Desktop has no verified greeting editor.
APPLY = '.apply-btn-new.big'
DIALOG = '.attachment_resume_dialog'
ROWS = DIALOG + ' .attachment_item'
ATTACHMENT_RECEIPT = 'html[data-job-agent-attachment-status]'
SEND_ATTACHMENT = DIALOG + ' .el-dialog__footer button.el-button--primary'
CHAT_URL = 'https://we.51job.com/pc/my/myjob'

class Unverified(RuntimeError):
    code = 'receipt_unknown'

class AttachmentUnverified(Unverified):
    code = 'attachment'

class LoginRequired(Unverified):
    code = 'login_required'

class RiskControl(Unverified):
    code = 'risk_control'

# Inspect only booleans, never return account credentials from Vuex.
JOB_JS = r'''() => { /* job51.job */
 const visible=e=>!!e.getClientRects().length;
 const app=document.querySelector('#app')?.__vue__;
 const common=app?.$store?.state?.commonStore;
 const v=[...document.querySelectorAll('*')].map(e=>e.__vue__).find(v=>v?.$options?.name==='ApplyjobBtn');
 const dialogs=[...document.querySelectorAll('[role="dialog"],.geetest_panel,.nc-container')].filter(visible).map(e=>e.innerText).join('\n');
 return {ready:!!v?.detailData?.jobId && typeof v.detailData.isApply==='boolean' && !common?.isshowGlobalLoading,
 loggedIn:!!common?.accountId,jobId:v?.detailData?.jobId,isApply:v?.detailData?.isApply,
 controls:[...document.querySelectorAll('.apply-btn-new.big')].map(e=>({text:e.innerText.trim(),class:e.className})),
 login:/passport|login/.test(location.pathname),risk:/滑动验证|安全验证|操作频繁|访问异常|账号异常|验证码/.test(dialogs)};
}'''
# The account-personalized isApply flag can lag the first paint. A positive
# applied state blocks immediately; a negative requires stable loaded state.
JOB_JS = '''async () => { const read = %s;
 let last=null, previous='', stable=0;
 for(let i=0;i<48;i++) {
   last=read();
   if(last.risk || last.login || (last.ready && last.isApply===true))return last;
   const key=JSON.stringify(last);
   stable=last.ready && last.loggedIn && key===previous ? stable+1 : 0;
   if(stable>=12)return last;
   previous=key; await new Promise(r=>setTimeout(r,250));
 }
 return {...last,ready:false}; }''' % JOB_JS
READY = '.apply-btn-new.big'
# Used by Page.wait_for; ensures account + job async state has settled before checking False.
ATTACHMENT_JS = r'''() => { /* job51.attachment */
 const e=document.querySelector('.attachment_resume_dialog');
 // Element UI moves the visible dialog to body; its DOM owner becomes a
 // transition / ElDialog instance. Resolve the original component via $parent.
 let v=e?.__vue__||e?.parentElement?.__vue__;
 for(let i=0;v&&i<12;i++,v=v.$parent) {
   if(Array.isArray(v.cvLogIdList)&&Array.isArray(v.attachmentResumeList))break;
 }
 if(!Array.isArray(v?.cvLogIdList)||!Array.isArray(v?.attachmentResumeList))v=null;
 if(!v)return {receipt:window.__jobAgentAttachmentReceipt||null};
 return {visible:!!v.isVisible,cvLogIdList:v.cvLogIdList,selectedAttachmentResumeId:v.selectedAttachmentResumeId,
 rows:(v.attachmentResumeList||[]).map((r,index)=>({index,attachResumeId:r.attachResumeId,attachResumeName:r.attachResumeName,
 selected:!!e.querySelectorAll('.attachment_item')[index]?.querySelector('.radio.selected')})),
 receipt:window.__jobAgentAttachmentReceipt||null};
}'''
# Observe the actual component event, never manufacture apply/attachment success.
# One per temporary page. Public receipt only contains matching IDs + sendStatus.
OBSERVE_JS = r'''() => { /* job51.observe */
 const e=document.querySelector('.attachment_resume_dialog');
 // Element UI moves the visible dialog to body; its DOM owner becomes a
 // transition / ElDialog instance. Resolve the original component via $parent.
 let v=e?.__vue__||e?.parentElement?.__vue__;
 for(let i=0;v&&i<12;i++,v=v.$parent) {
   if(Array.isArray(v.cvLogIdList)&&Array.isArray(v.attachmentResumeList))break;
 }
 if(!Array.isArray(v?.cvLogIdList)||!Array.isArray(v?.attachmentResumeList))v=null;
 if(!v || typeof v.$on!=='function')return false;
 window.__jobAgentAttachmentReceipt=null;
 document.documentElement.removeAttribute('data-job-agent-attachment-status');
 v.$on('confirm', r=>{window.__jobAgentAttachmentReceipt={sendStatus:r.sendStatus,params:{cvLogIdList:r.params?.cvLogIdList,attachResumeId:r.params?.attachResumeId}};document.documentElement.setAttribute('data-job-agent-attachment-status',String(r.sendStatus));});
 return true;
}'''
# P0/P1 desktop evidence has a QR bridge, no chat body editor or stable IDs.
CHAT_JS = r'''() => { /* job51.chat */
 const visible=e=>!!e.getClientRects().length;
 const app=document.querySelector('#app')?.__vue__;
 const c=app?.$store?.state?.commonStore;
 return {loggedIn:!!c?.accountId,ready:false,reason:'桌面端仅有微信桥接提醒，未取得聊天正文、消息时间和消息ID',
 risk:[...document.querySelectorAll('[role="dialog"],.geetest_panel,.nc-container')].filter(visible).some(e=>/安全验证|操作频繁|验证码/.test(e.innerText))};
}'''

def guard(data):
    if not isinstance(data, dict):
        raise Unverified('51job 页面状态不可读')
    if data.get('risk'):
        raise RiskControl('验证码或风控提示，停止平台')
    if data.get('login') or not data.get('loggedIn'):
        raise LoginRequired('51job 登录失效或账号未就绪')


def applied(data, job_id):
    guard(data)
    if not data.get('ready') or str(data.get('jobId', '')) != str(job_id):
        raise Unverified('51job 岗位或登录异步状态未就绪')
    controls = data.get('controls', [])
    if data.get('isApply') is True and any(c.get('text') == '已投递' for c in controls):
        return True
    if data.get('isApply') is False and any(c.get('text') == '立即投递' and 'disabled' not in c.get('class', '').split() for c in controls):
        return False
    raise Unverified('51job 投递状态不明或互相矛盾')


def attachment_selection(data, name):
    if not isinstance(data, dict) or data.get('visible') is not True or not data.get('cvLogIdList'):
        raise AttachmentUnverified('已申请、附件未确认：缺少正常弹窗或 cvLogId')
    rows = data.get('rows', [])
    matches = [r for r in rows if r.get('attachResumeName') == name and r.get('attachResumeId')]
    if len(matches) != 1:
        raise AttachmentUnverified('已申请、附件未确认：附件名称未唯一命中')
    row = matches[0]
    if sum(r.get('attachResumeId') == row['attachResumeId'] for r in rows) != 1:
        raise AttachmentUnverified('已申请、附件未确认：附件 ID 不唯一')
    return row


def attachment_receipt(data, attachment_id, cv_logs):
    if not isinstance(data, dict):
        return False
    r = data.get('receipt') or {}
    p = r.get('params') or {}
    return (r.get('sendStatus') == 1 and str(p.get('attachResumeId')) == str(attachment_id)
            and p.get('cvLogIdList') == cv_logs)


def parse_messages(data, *, since=None):
    guard(data)
    # Deliberately fail rather than advertise an empty, successfully polled inbox.
    raise Unverified('51job 聊天未验证：需要已登录的正文入口、消息时间和稳定消息 ID')

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


def listing_signals(row):
    import re
    value = signal_fields(row)
    raw = value['hr_activity_raw']
    if value['hr_activity']=='unknown':
        if re.search(r'(?:今日|今天)(?:回复|查看|人才搜索)|\d+分钟前(?:回复|查看简历)|\d+小时前(?:回复|查看简历)',raw):
            value['hr_activity']='today'
        elif re.search(r'本周(?:回复|人才搜索|查看)',raw):
            value['hr_activity']='week'
    return value

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
        # Known official company microsite has an unsupported detail renderer.
        parts = urlsplit(row['url'])
        if (parts.scheme == 'https' and parts.hostname == 'mahle.51job.com'
                and not parts.username and not parts.password
                and parts.path == '/jobdetail.html'):
            continue
        url = clean_job_url(row['url'])
        if row['id'] in seen:
            continue
        seen.add(row['id'])
        result.append(JobListing(PLATFORM,row['id'],url,row['company'],row['title'],row.get('city',''),row.get('salary',''),
                                 {k:row[k] for k in ('experience','education') if row.get(k)}, **listing_signals(row)))
    if not result:
        raise CollectionError('搜索结果全部为不支持的企业专页')
    return result


def parse_detail(data, listing):
    collection_guard(data)
    if str(data.get('id','')) != listing.platform_job_id:
        raise CollectionError('详情岗位 ID 不匹配')
    jd = data.get('jd')
    if not isinstance(jd,str) or not jd.strip():
        raise CollectionError('JD 正文未取得')
    return jd

PLATFORM = '51job'
CITY_CODES = {'上海': '020000', '苏州': '070300', '杭州': '080200', '南京': '070200'}
SEARCH_URL = 'https://we.51job.com/pc/search'
SEARCH_PARAMS = lambda q,c: dict(keyword=q.keyword,jobArea=c,pageNum=q.page)
JOB_HOSTS = {'jobs.51job.com'}
JOB_PATH = r'/[^/]+/\d+\.html'

SEARCH_JS = r'''(async () => { /* collect.job51.search */ const read=()=>{
 const visible=e=>!!e.getClientRects().length;
 const text=document.body?.innerText||'';
 const challenge=[...document.querySelectorAll('[role="dialog"],.geetest_panel,.nc-container,[class*="captcha"],iframe[src*="captcha"]')].filter(visible);
 const risk=challenge.some(e=>/验证|访问异常|操作频繁/.test(e.innerText)||e.matches('.geetest_panel,.nc-container,[class*="captcha"],iframe')) || /^(安全验证|访问异常|访问过于频繁|请完成安全验证|人机验证)/.test(text.trim()) || /captcha|\/verify/.test(location.pathname);
 const login=/passport|\/login/.test(location.href)||!![...document.querySelectorAll('iframe[src*="passport"],.login-dialog')].find(visible);

 const v=[...document.querySelectorAll('*')].map(e=>e.__vue__).find(v=>v?.$options?.name==='PCSearch');
 const loggedIn=!!document.querySelector('#app')?.__vue__?.$store?.state?.commonStore?.accountId;
 return {ready:!!v&&!v.isLoading&&Array.isArray(v.joblist),loggedIn,login,risk,page:Number(v?.searchParams?.pageNum),empty:v?.isEmpty===true,items:(v?.joblist||[]).map(j=>({id:String(j.jobId||''),url:(j.jobHref||'').split('?')[0],title:j.jobName,company:j.fullCompanyName||j.companyName,city:j.jobAreaString,salary:j.provideSalaryString,hr_activity_raw:j.hrIsOnline===true?'在线':(j.onlineHrLabelList||j.hrLabels||[]).filter(x=>typeof x==='string').join('；'),experience:j.workYearString,education:j.degreeString}))};
}; let d; for(let i=0;i<40;i++){d=read(); if(d.risk||d.login||(d.ready&&d.loggedIn))return d; await new Promise(r=>setTimeout(r,250));} return d; })()'''
DETAIL_JS = r'''(async () => { /* collect.job51.detail */ const read=()=>{
 const visible=e=>!!e.getClientRects().length;
 const text=document.body?.innerText||'';
 const challenge=[...document.querySelectorAll('[role="dialog"],.geetest_panel,.nc-container,[class*="captcha"],iframe[src*="captcha"]')].filter(visible);
 const risk=challenge.some(e=>/验证|访问异常|操作频繁/.test(e.innerText)||e.matches('.geetest_panel,.nc-container,[class*="captcha"],iframe')) || /^(安全验证|访问异常|访问过于频繁|请完成安全验证|人机验证)/.test(text.trim()) || /captcha|\/verify/.test(location.pathname);
 const login=/passport|\/login/.test(location.href)||!![...document.querySelectorAll('iframe[src*="passport"],.login-dialog')].find(visible);

 const e=document.querySelector('.job_msg > div');
 const v=[...document.querySelectorAll('*')].map(e=>e.__vue__).find(v=>v?.$options?.name==='ApplyjobBtn');
 return {ready:!!e&&!!v?.detailData?.jobId,loggedIn:!!document.querySelector('#app')?.__vue__?.$store?.state?.commonStore?.accountId,login,risk,id:String(v?.detailData?.jobId||''),jd:e?.innerText};
}; let d; for(let i=0;i<40;i++){d=read(); if(d.risk||d.login||(d.ready&&d.loggedIn))return d; await new Promise(r=>setTimeout(r,250));} return d; })()'''

def pagination_js(page):
    """Use the site's observed read-only pagination handler; URL pageNum is ignored."""
    import json
    return '''(async () => { /* collect.job51.pagination */
      const state = await %s;
      if(state.risk || state.login || !state.loggedIn || !state.ready)return state;
      const target = %s;
      const v=[...document.querySelectorAll('*')].map(e=>e.__vue__).find(v=>v?.$options?.name==='PCSearch');
      if(!v || typeof v.getJobList!=='function')return {...state,ready:false};
      if(!Number.isFinite(Number(v.total)) || Number(v.total)<0 || (!state.items.length && !state.empty))return {...state,ready:false};
      if(target>Math.ceil(Number(v.total)/20))return {...state,page:target,items:[],empty:true};
      v.getJobList(target);
      return await %s;
    })()''' % (SEARCH_JS,json.dumps(page),SEARCH_JS)
