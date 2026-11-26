"""Read-only authentication projection, with no cookies, IDs or account names."""
import logging
import json
import time
from dataclasses import asdict, dataclass
from browser.base import SpaceUnavailable

URLS={
    'boss':'https://www.zhipin.com/web/geek/resume',
    'zhilian':'https://i.zhaopin.com/im',
    'liepin':'https://c.liepin.com/',
    '51job':'https://www.51job.com/resume/center',
}
HOSTS={'boss':{'www.zhipin.com'},'zhilian':{'i.zhaopin.com'},
       'liepin':{'c.liepin.com'},'51job':{'www.51job.com','we.51job.com'}}

# Known signed-in UI/state only; absence of a login modal is never evidence of login.
SESSION_JS=r'''(async () => { /* session.preflight */
 const read=()=>{
  const visible=e=>!!e && !!e.getClientRects().length && getComputedStyle(e).visibility!=='hidden';
  const text=document.body?.innerText||'';
  const challenges=[...document.querySelectorAll('[role=dialog],[role=alert],.geetest_panel,.nc-container,[class*=captcha],iframe[src*=captcha],.verify-wrap')].filter(visible);
  const risk=challenges.some(e=>/验证码|安全验证|人机验证|频繁|异常|访问受限/.test(e.innerText)||e.matches('.geetest_panel,.nc-container,iframe[src*=captcha]')) || /\/verify|captcha/.test(location.pathname);
  const login=/passport|login|signin/.test(location.hostname+location.pathname) || [...document.querySelectorAll('input[type=password],.login-dialog,.sign-form')].some(visible);
  const platform=PLATFORM;
  let logged_in=false,account_type='unknown';
  if(platform==='boss')logged_in=[...document.querySelectorAll('.nav-figure,.user-name')].some(e=>visible(e)&&!!e.textContent.trim()) || /退出登录/.test(text);
  if(platform==='zhilian')logged_in=[...document.querySelectorAll('.user-name,.userName,.user__name,.zp-user-name,.user-info__name,.c-login__top__name')].some(e=>visible(e)&&!!e.textContent.trim()&&!/登录|注册/.test(e.textContent)) || /退出登录|退出账号/.test(text);
  if(platform==='liepin') {
    logged_in=[...document.querySelectorAll('.header-quick-menu-username')].some(visible) || /退出登录/.test(text);
    // Switch labels refer to the destination account type, never the current one.
    if([...document.querySelectorAll('[class^=student-v]')].some(e=>/当前为学生版/.test(e.textContent)))account_type='student';
    else if(/切换.{0,3}(职场版|社会招聘)|我是职场人/.test(text))account_type='student';
    else if(/切换.{0,3}(学生版|校园版)|我是学生/.test(text))account_type='professional';
    else if(/学生版|校园版/.test(text))account_type='student';
    else if(/职场版/.test(text))account_type='professional';
  }
  if(platform==='51job')logged_in=!!document.querySelector('#app')?.__vue__?.$store?.state?.commonStore?.accountId || /退出登录/.test(text);
  return {logged_in:logged_in&&!login,login,risk,account_type,expected_host:HOSTS.includes(location.hostname)};
 };
 let d;
 for(let i=0;i<32;i++) {d=read();if(d.risk||d.login||(d.logged_in&&(PLATFORM!=='liepin'||d.account_type!=='unknown')))return d;await new Promise(r=>setTimeout(r,250));}
 return d;
})()'''


@dataclass(frozen=True)
class SessionResult:
    platform: str
    ok: bool
    reason: str
    logged_in: bool = False
    account_type: str = 'unknown'

    def as_dict(self): return asdict(self)


def check_session(browser,platform,space_name='job-agent 采集',expected_account_type='any'):
    if platform not in URLS:
        raise ValueError('Unknown platform')
    page=None
    try:
        page=browser.space(space_name).new_page()
        page.goto(URLS[platform],wait_ms=15000)
        data=page.evaluate(SESSION_JS.replace('PLATFORM',json.dumps(platform)).replace('HOSTS',json.dumps(sorted(HOSTS[platform]))))
        if not isinstance(data,dict):
            return SessionResult(platform,False,'session_unknown')
        logged=data.get('logged_in') is True
        account=data.get('account_type') if data.get('account_type') in {'student','professional'} else 'unknown'
        reason=('risk_control' if data.get('risk') else 'login_required' if data.get('login') or not logged
                else 'unexpected_redirect' if data.get('expected_host') is not True
                else 'account_type' if expected_account_type!='any' and account!=expected_account_type else '')
        logging.getLogger(__name__).info('session platform=%s logged_in=%s account_type=%s expected=%s reason=%s', platform,logged,account,expected_account_type,reason)
        return SessionResult(platform,not reason,reason,logged,account)
    except SpaceUnavailable:
        raise
    except Exception as exc:
        from collect.deadline import CollectionTimeout
        if isinstance(exc,CollectionTimeout):raise
        return SessionResult(platform,False,'risk_control' if getattr(exc,'code','')=='risk_control' else 'session_unknown')
    finally:
        if page is not None:
            page.close()
