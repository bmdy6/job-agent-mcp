"""Public website DOM verified 2026-11-26. App-only capabilities stay unavailable."""
import re
from urllib.parse import urlencode, urlsplit
from job_agent.browser.base import BrowserError, RiskControl
from job_agent.platforms.base import JobListing

GREETING_NEWLINE='reject'  # No verified web editor.

class Unverified(BrowserError):
    code='receipt_unknown'

GUARD_JS=r'''() => { /* maimai.guard */
 const visible=e=>!!e.getClientRects().length;
 const challenges=[...document.querySelectorAll('[role=dialog],[role=alert],.geetest_panel,[class*=captcha],iframe[src*=captcha]')].filter(visible);
 return {url:location.href,risk:challenges.some(e=>/验证码|安全验证|操作频繁|账号异常|访问异常/.test(e.innerText)||e.matches('.geetest_panel,[class*=captcha],iframe[src*=captcha]'))};
}'''
SEARCH_JS=r'''() => { /* maimai.search */
 const cards=[...document.querySelectorAll('a[data-testid="website-job-card"]')];
 const txt=(e,s)=>e.querySelector(s)?.innerText.trim()||'';
 return {ready:cards.length>0||!!document.querySelector('[aria-label="招聘职位"]'),
 has_next:[...document.querySelectorAll('a')].some(e=>e.innerText.trim()==='下一页'),items:cards.map(e=>{
 const meta=[...e.querySelectorAll('[class*="__jobMetaItem"]')].map(x=>x.innerText.trim());
 return {url:e.href,title:txt(e,'h2'),salary:txt(e,'[class*="__jobSalary"]'),
 company:txt(e,'[class*="__companyName"]'),city:meta[0]||'',experience:meta[1]||'',education:meta[2]||''};})};
}'''
DETAIL_JS=r'''() => { /* maimai.detail */
 const heading=[...document.querySelectorAll('h1,h2,h3')].find(e=>e.innerText.trim()==='职位描述');
 return {url:location.href,jd:heading?.parentElement.innerText||''};
}'''

def guard(data):
    if not isinstance(data,dict):raise Unverified('脉脉页面状态不可读')
    if data.get('risk'):raise RiskControl('脉脉要求验证或操作受限')
    if 'url' in data and urlsplit(data['url']).hostname!='maimai.cn':
        raise Unverified('脉脉跳转到未核验域名')


def clean_job_url(url):
    p=urlsplit(url)
    if p.scheme!='https' or p.netloc!='maimai.cn' or not re.fullmatch(r'/n/job/geo/[A-Za-z0-9_-]+',p.path):
        raise ValueError('脉脉岗位 URL 不受支持')
    return 'https://maimai.cn'+p.path


def search_url(query):
    if type(query.page) is not int or query.page<1:raise ValueError('页码必须为正整数')
    return 'https://maimai.cn/n/platform/job-list?'+urlencode({'city':query.city,'page':query.page})


def parse_search(data,query):
    guard(data)
    if data.get('ready') is not True or not isinstance(data.get('items'),list) or type(data.get('has_next')) is not bool:
        raise Unverified('脉脉岗位目录未就绪或结构已变')
    result=[]
    for row in data['items']:
        if not isinstance(row,dict):raise Unverified('脉脉岗位卡结构已变')
        try:url=clean_job_url(row.get('url',''))
        except ValueError:continue
        if not all(isinstance(row.get(k),str) and row[k] for k in ('title','company','city')):continue
        if query.keyword.casefold() not in row['title'].casefold() or row['city']!=query.city:continue
        result.append(JobListing('maimai',url.rsplit('/',1)[-1],url,row['company'],row['title'],
          city=row['city'],salary=row.get('salary',''),experience=row.get('experience',''),education=row.get('education','')))
    if not result and data['has_next']:
        # Collector interprets [] as exhaustion. Do not silently truncate a directory
        # after local keyword filtering; server-side keyword search is not verified.
        raise Unverified('脉脉目录本页无匹配但还有下一页：不能声明搜索结束')
    return result


def parse_detail(data,listing):
    guard(data)
    try:url=clean_job_url(data.get('url',''))
    except ValueError:raise Unverified('脉脉详情身份不可读') from None
    if url.rsplit('/',1)[-1]!=listing.platform_job_id or not isinstance(data.get('jd'),str) or not data['jd'].strip():
        raise Unverified('脉脉详情身份或 JD 不可读')
    return data['jd']
