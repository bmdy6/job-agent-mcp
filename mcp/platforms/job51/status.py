"""Read-only application status fallback. Never treats QR/news metadata as HR text."""
from datetime import date, datetime, timezone
import math
import re
from urllib.parse import parse_qs, urlsplit
from platforms.base import ReplyEvent
from .pages import Unverified, LoginRequired, RiskControl, guard

APPLICATION_URL = 'https://i.51job.com/userset/my_apply.php?type=sh&lang=c&tagType='
STATUS_TAGS = {'cy': '已查阅', 'xq': '感兴趣', 'ms': '邀面试'}
STATUSES = frozenset((*STATUS_TAGS.values(), '不匹配', '不合适'))

# Fields/classes observed on 2026-11-25. Do not return hidden account/sign tokens,
# HR names, resumes, or global help text (which describes every possible status).
APPLICATION_JS = r'''() => { /* job51.applications */
 const visible=e=>!!e.getClientRects().length;
 const text=document.body?.innerText||'';
 const login=/passport|\/login/.test(location.href)||!![...document.querySelectorAll('iframe[src*="passport"],.login-dialog')].find(visible);
 const risk=/captcha|\/verify/.test(location.pathname)||[...document.querySelectorAll('[role="dialog"],.geetest_panel,.nc-container,[class*="captcha"],iframe[src*="captcha"]')].filter(visible).some(e=>/验证|访问异常|操作频繁/.test(e.innerText)||e.matches('.geetest_panel,.nc-container,[class*="captcha"],iframe'))||/^(安全验证|访问异常|访问过于频繁|请完成安全验证|人机验证)/.test(text.trim());
 const rows=[...document.querySelectorAll('.apox .e')].map(e=>{
   const a=e.querySelector('a.zhn'), c=e.querySelector('a.gs');
   const url=a?.href||'';
   return {id:url.match(/\/([0-9]+)\.html(?:\?|$)/)?.[1]||'',url,
    title:a?.textContent.trim()||'',company:c?.textContent.trim()||'',
    appliedAt:e.querySelector('.rq span')?.textContent.trim()||'',
    statuses:[...e.querySelectorAll('*')].filter(n=>visible(n)&&n.children.length===0&&!n.closest('.gtbox')&&/^(已查阅|感兴趣|邀面试|不匹配|不合适)$/.test(n.textContent.trim())).map(n=>n.textContent.trim())};
 });
 const tag=new URL(location.href).searchParams.get('tagType')||'';
 const emptyText=document.querySelector('.none.n7 p')?.textContent.trim()||'';
 const emptyByTag={'cy':'HR还未查看你的简历','xq':'暂无HR对你的简历表示感兴趣','ms':'暂无HR邀你参加面试'};
 const empty=tag ? emptyText===emptyByTag[tag] : /^(暂无申请记录|你还没有申请过职位|你还没有申请记录)/.test(emptyText);
 const p=document.querySelector('.p_in');
 const current=Number(p?.querySelector('li.on')?.textContent.trim());
 const total=Number(p?.querySelector('.td')?.textContent.match(/共\s*(\d+)\s*页/)?.[1]);
 const next=p?[...p.querySelectorAll('a[href]')].find(a=>a.textContent.trim()==='下一页')?.href:null;
 return {ready:location.hostname==='i.51job.com'&&location.pathname==='/userset/my_apply.php'&&!!document.querySelector('.mt_l .on a[href*="type=sh"]')&&['全部','已查阅','感兴趣','邀面试'].every(s=>[...document.querySelectorAll('a[href*="tagType="]')].some(a=>a.textContent.trim()===s)),
 loggedIn:!!document.querySelector('#accountid')?.value,login,risk,tag,
 page:empty?0:current,pages:empty?0:total,next:next||null,empty,rows};
}'''


def parse_application_page(data, tag):
    guard(data)
    if data.get('ready') is not True or data.get('tag') != tag or tag not in ('', *STATUS_TAGS):
        raise Unverified('51job 申请筛选页面未就绪或筛选不匹配')
    rows = data.get('rows')
    if not isinstance(rows, list):
        raise Unverified('51job 申请列表结构不可读')
    if not rows:
        if data.get('empty') is True and data.get('page') == data.get('pages') == 0:
            return [], None
        raise Unverified('51job 缺少明确的无数据标志')
    for row in rows:
        if (not isinstance(row, dict) or not all(isinstance(row.get(k), str) and row[k].strip()
                for k in ('id', 'title', 'company', 'appliedAt', 'url'))):
            raise Unverified('51job 申请缺少岗位、公司或申请日期')
        parts = urlsplit(row['url'])
        if (not row['id'].isdigit() or parts.scheme != 'https' or parts.hostname != 'jobs.51job.com'
                or parts.username or parts.password or not re.fullmatch(r'/all/'+re.escape(row['id'])+r'\.html',parts.path)):
            raise Unverified('51job 申请岗位身份不匹配')
        try:
            date.fromisoformat(row['appliedAt'])
        except ValueError:
            raise Unverified('51job 申请日期不可识别') from None
        if not isinstance(row.get('statuses'),list) or any(s not in STATUSES for s in row['statuses']):
            raise Unverified('51job 申请状态不可识别')
    current, total = data.get('page'), data.get('pages')
    if type(current) is not int or type(total) is not int or not 1 <= current <= total <= 50:
        raise Unverified('51job 申请分页不可识别或超出上限')
    next_url = data.get('next')
    if current == total:
        if next_url:
            raise Unverified('51job 申请末页仍有下一页')
    else:
        parts = urlsplit(next_url or '')
        if (parts.scheme != 'https' or parts.hostname != 'i.51job.com' or parts.username or parts.password
                or parts.path != '/userset/my_apply.php' or parse_qs(parts.query).get('tagType',[''])[0] != tag):
            raise Unverified('51job 申请下一页不可识别')
    return rows, next_url


def read_applications(space, *, tag=''):
    """Read one complete official filter, validating every page before following it."""
    if tag not in ('', *STATUS_TAGS):
        raise ValueError('Unknown application filter')
    page = space.new_page()
    try:
        return read_application_pages(page, tag=tag)
    finally:
        page.close()


def read_application_pages(page, *, tag=''):
    """Reuse an owned temporary page; the caller owns cleanup."""
    if tag not in ('', *STATUS_TAGS):
        raise ValueError('Unknown application filter')
    url, rows, seen_urls, last_page = APPLICATION_URL + tag, [], set(), 0
    expected_pages = None
    while url:
        if url in seen_urls:
            raise Unverified('51job 申请分页重复')
        seen_urls.add(url)
        page.goto(url, wait_ms=10000)
        data = page.evaluate(APPLICATION_JS)
        current_rows, url = parse_application_page(data, tag)
        if expected_pages is not None and data['pages'] != expected_pages:
            raise Unverified('51job 申请分页在读取期间变化，下轮重新读取')
        expected_pages = data['pages']
        if current_rows and data['page'] != last_page + 1:
            raise Unverified('51job 申请分页不连续')
        last_page = data['page']
        rows.extend(current_rows)
    if len({r['id'] for r in rows}) != len(rows):
        raise Unverified('51job 申请岗位重复，列表可能发生变化')
    return rows


def conversation_summaries(snapshots):
    """Status-only equivalent: one stable application/date/status-set summary."""
    from platforms.base import ConversationSummary
    labels = {r['id']: set(r['statuses']) for r in snapshots['']}
    for tag, label in STATUS_TAGS.items():
        for row in snapshots[tag]:
            labels[row['id']].add(label)
    return [ConversationSummary(r['id'] + ':' + r['appliedAt'],
                'status:' + '|'.join(sorted(labels[r['id']])), 0)
            for r in snapshots['']]


def application_events(snapshots, *, observed_at, since=None):
    """Emit one observation per (job, application date, status), never a fake message.

    The site provides no status time/ID. occurred_at is explicitly FIRST OBSERVED
    time, not HR action time; the repository preserves it on duplicate reads.
    """
    if not isinstance(observed_at,(int,float)) or not math.isfinite(observed_at):
        raise Unverified('51job 状态观测时间无效')
    all_rows = {r['id']:r for r in snapshots['']}
    found = {(r['id'],s) for r in all_rows.values() for s in r['statuses']}
    for tag, label in STATUS_TAGS.items():
        for row in snapshots[tag]:
            base = all_rows.get(row['id'])
            if base is None or any(base[k] != row[k] for k in ('company','title','appliedAt')):
                raise Unverified('51job 状态与全部申请不匹配，下轮重新读取')
            found.add((row['id'],label))
    result = []
    for job_id,label in sorted(found):
        row=all_rows[job_id]
        stamp=datetime.fromtimestamp(observed_at,timezone.utc).isoformat()
        body=f'51job 状态变化，没有正文\n申请状态：{label}\n首次观测时间：{stamp}\n原站未提供状态发生时间；首次抓取可能为已有状态。'
        if since is None or observed_at > since:
            result.append(ReplyEvent('51job',f'51job-status:{job_id}:{row["appliedAt"]}:{label}',job_id,
                row['company'],row['title'],'incoming',body,observed_at,APPLICATION_URL,
                {'id_origin':'application_status'}))
    return result
