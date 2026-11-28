"""Nowcoder projections verified 2026-11-26. No application or community actions."""
import re
from urllib.parse import urlsplit, urlencode
from job_agent.browser.base import BrowserError, RiskControl
from job_agent.platforms.base import JobListing

class Unverified(BrowserError):
    code = 'receipt_unknown'

class LoginRequired(BrowserError):
    code = 'login_required'

GUARD_JS = r'''() => { /* nowcoder.guard */
 const visible=e=>!!e.getClientRects().length;
 const text=[...document.querySelectorAll('[role=dialog],[role=alert],.el-message,.geetest_panel,[class*=captcha],iframe[src*=captcha]')].filter(visible).map(e=>e.innerText).join('\n');
 return {url:location.href,risk:/验证码|安全验证|操作频繁|账号异常|访问异常/.test(text)||[...document.querySelectorAll('.geetest_panel,[class*=captcha],iframe[src*=captcha]')].some(visible),
 login:!![...document.querySelectorAll('input[type=password],.login-dialog')].find(visible),
 logged_in:!!document.querySelector('a[href*="/users/notice/privateChat"]')};
}'''
SEARCH_JS = r'''() => { /* nowcoder.search */
 const v=document.querySelector('.school-job-box')?.__vue__;
 const txt=(e,s)=>e.querySelector(s)?.innerText.trim()||'';
 return {ready:!!v&&v.isLoading===false,items:[...document.querySelectorAll('a.job-message-boxs[href*="/jobs/detail/"]')].map(e=>{
 const id=Number(new URL(e.href).pathname.split('/').pop());const r=v?.list?.find(x=>x.id===id);
 return {url:e.href,title:txt(e,'.job-name'),salary:txt(e,'.job-salary'),company:txt(e.parentElement,'.company-name'),
 city:r?.jobCity||'',experience:r?.graduationYear||'',education:[...e.querySelectorAll('.job-info-item')].map(x=>x.innerText.trim()).find(x=>/本科|硕士|博士|大专|学历不限/.test(x))||'',
 activity:[...e.querySelectorAll('.job-info-item')].map(x=>x.innerText.trim()).find(x=>/HR/.test(x))||''};})};
}'''
DETAIL_JS = r'''() => { /* nowcoder.detail */
 return {url:location.href,jd:document.querySelector('.job-detail-word')?.innerText||''};
}'''

def guard(data):
    if not isinstance(data,dict):raise Unverified('牛客页面状态不可读')
    if data.get('risk'):raise RiskControl('牛客风控提示，停止平台')
    if data.get('login'):raise LoginRequired('牛客登录失效')
    if 'url' in data and urlsplit(data['url']).hostname != 'www.nowcoder.com':
        raise Unverified('牛客跳转到了未核验域名')


def clean_job_url(url):
    p=urlsplit(url)
    if (p.scheme!='https' or p.netloc!='www.nowcoder.com'
            or not re.fullmatch(r'/jobs/detail/[1-9]\d*',p.path)):
        raise ValueError('牛客岗位 URL 不受支持')
    return 'https://www.nowcoder.com'+p.path


def search_url(query):
    if query.page != 1:
        raise Unverified('牛客翻页尚未取证；不重复采集第一页')
    return 'https://www.nowcoder.com/jobs/school/jobs?'+urlencode({'city':query.city})


def parse_search(data,query):
    guard(data)
    if data.get('ready') is not True or not isinstance(data.get('items'),list):
        raise Unverified('牛客列表未就绪或结构已变')
    result=[]
    for row in data['items']:
        if not isinstance(row,dict):raise Unverified('牛客列表行结构已变')
        try:url=clean_job_url(row.get('url',''))
        except ValueError:continue
        if not all(isinstance(row.get(k),str) and row[k] for k in ('title','company','city')):continue
        if query.keyword.casefold() not in row['title'].casefold() or query.city not in row['city'].split('/'):continue
        graduation=row.get('experience','')
        result.append(JobListing('nowcoder',url.rsplit('/',1)[-1],url,row['company'],row['title'],
                                 city=row['city'],salary=row.get('salary',''),experience=graduation,
                                 education=row.get('education',''),hr_activity_raw=row.get('activity',''),
                                 extra={'graduation_year':graduation}))
    return result


def parse_detail(data,listing):
    guard(data)
    try:url=clean_job_url(data.get('url',''))
    except ValueError:raise Unverified('牛客详情身份不可读') from None
    if url.rsplit('/',1)[-1]!=listing.platform_job_id or not isinstance(data.get('jd'),str) or not data['jd'].strip():
        raise Unverified('牛客详情身份或 JD 不可读')
    return data['jd']

GREETING_NEWLINE = 'reject'  # No send-capable editor has been verified.
