"""Read-only evidence, with bounded fixed-code/selector receipts and no message bodies."""
from datetime import datetime
import time

SELECTORS = {
    'application_record': '.ji-item .ji-item-status p',
    'application_record_day': '.apox .e a.zhn + company/title + .rq span (application date only; attachment unknown)',
    'application_time_unavailable': '.ji-item: matching historical status record without original application time',
    'outgoing_message': '.message-item.item-myself[data-mid] .message-status',
    'explicit_not_applied': '.summary-planes__action button / .job-banner .btn-startchat',
    'no_matching_record': 'original-site records: no unique ID/company/title/time match',
    'detail_unavailable': 'original-site detail: no verified application state',
    'unsupported_platform': 'no verified original-site reader',
    'login_required': 'original-site login guard',
    'risk_control': 'original-site risk guard',
    'page_timeout': 'original-site bounded wait',
    'browser_error': 'original-site observation unavailable',
    'conversation_unavailable': 'existing conversation: stable row or matching message pane unavailable',
    'space_unavailable': 'Ego task space ownership guard',
    'task_space': 'Ego task space ownership guard',
    'paused': 'existing platform pause',
}


def evidence(status, code, *, receipt_at=None):
    return {'status': status, 'evidence': {'code':code, 'selector':SELECTORS[code], 'receipt_at':receipt_at}}


def application_outcome(job, data, *, checked_epoch):
    from job_agent.platforms.zhilian import pages
    stamp = pages.fresh_application_record(data, job, job['attempted_at'], min(checked_epoch, job['attempted_at']+600))
    if stamp:
        return evidence('sent_confirmed', 'application_record', receipt_at=datetime.fromisoformat(stamp).timestamp())
    identity = data.get('identity') or {}
    same = all(job.get(k) and identity.get(k)==job[k] for k in ('platform_job_id','company','title'))
    if same and pages.applied(data) is False:
        return evidence('not_sent', 'explicit_not_applied')
    norm = lambda s: ' '.join(str(s or '').split())
    for row in data.get('rows',[]):
        try:
            jid=pages.job_id((row.get('url') or '').replace('http:','https:',1))
        except ValueError:
            continue
        if (jid==job['platform_job_id'] and job['company'] and job['title']
                and norm(row.get('company'))==norm(job['company']) and norm(row.get('title'))==norm(job['title'])
                and row.get('status') not in {'成功投递','投递成功'}):
            return evidence('still_unknown','application_time_unavailable')
    return evidence('still_unknown', 'no_matching_record')


def job51_outcome(job, rows):
    """The official history has date precision, never an exact sending/file receipt."""
    from zoneinfo import ZoneInfo
    zone = ZoneInfo('Asia/Shanghai')
    attempted = datetime.fromtimestamp(job['attempted_at'], zone)
    norm = lambda value: ' '.join(str(value or '').split())
    matches = [r for r in rows if r.get('id')==job['platform_job_id']
               and job['company'] and job['title']
               and norm(r.get('company'))==norm(job['company'])
               and norm(r.get('title'))==norm(job['title'])
               and r.get('appliedAt')==attempted.date().isoformat()]
    if len(matches)==1:
        day = datetime.combine(attempted.date(), datetime.min.time(), tzinfo=zone)
        return evidence('sent_confirmed','application_record_day',receipt_at=day.timestamp())
    return evidence('still_unknown','no_matching_record')


def boss_outcome(job, data, *, checked_epoch):
    from job_agent.platforms.boss import pages
    pages.guard(data)
    norm = lambda s: ' '.join(str(s or '').split())
    same = (data.get('current_job_id')==job['platform_job_id'] and bool(data.get('current_conversation_id')))
    matches = []
    for r in data.get('rows', []) if same else []:
        stamp = pages.epoch(r.get('time'))
        if (r.get('id') and r.get('job_id')==job['platform_job_id'] and r.get('conversation_id')==data['current_conversation_id']
                and norm(r.get('company'))==norm(job['company']) and norm(r.get('job_title'))==norm(job['title'])
                and job['company'] and job['title'] and r.get('direction')=='outgoing'
                and r.get('receipt') in {'已读','送达','已送达'} and stamp is not None
                and job['attempted_at']-1<=stamp<=min(checked_epoch,job['attempted_at']+600)):
            matches.append(stamp)
    if matches:
        return evidence('sent_confirmed', 'outgoing_message', receipt_at=min(matches))
    return evidence('still_unknown', 'no_matching_record')


# Projection intentionally drops body, recruiter and chat URL before returning from the Page.
BOSS_MESSAGES_JS = '''() => { /* reconcile.boss_messages */
 const s=document.querySelector('[role=listitem] .selected')?.closest('[role=listitem]')?.__vue__?.$props?.source;
 return {current_job_id:s?.encryptJobId,current_conversation_id:s?.uniqueId,
 rows:[...document.querySelectorAll('.message-item[data-mid]')].map(e=>({id:e.getAttribute('data-mid'),
 job_id:s?.encryptJobId,company:s?.brandName,job_title:s?.jobName,conversation_id:s?.uniqueId,
 direction:e.classList.contains('item-myself')?'outgoing':e.classList.contains('item-friend')?'incoming':'system',
 receipt:e.querySelector('.message-status')?.textContent?.trim(),
 time:e.__vue__?.$props?.message?.time ?? (String(s?.lastMsgId)===e.getAttribute('data-mid')?s?.lastTS:null)}))};
}'''


class OriginalSiteReader:
    def __init__(self, space):
        self.space = space
        self.page = None
        self.platform = None
        self.cached = None
        self.failure = None

    def close(self):
        if self.page is not None:
            page, self.page = self.page, None
            page.close()
        self.platform = None
        self.cached = None
        self.failure = None

    def records(self, platform):
        if self.platform == platform:
            if self.failure is not None:
                raise self.failure
            return self.page
        self.close()
        from job_agent.platforms.boss import pages as boss
        from job_agent.platforms.zhilian import pages as zhilian
        pages = boss if platform=='boss' else zhilian
        self.platform = platform
        self.page = self.space.new_page()
        self.page.goto(pages.CHAT_URL if platform=='boss' else pages.APPLICATIONS_URL, wait_ms=15000)
        pages.guard(self.page.evaluate(pages.STATE_JS))
        self.page.wait_for(pages.LIST if platform=='boss' else pages.APPLICATION_ROWS)
        if platform == 'zhilian':
            from job_agent.platforms.zhilian.reconciliation import read_history
            self.cached = read_history(self.page)
        else:
            from job_agent.platforms.boss.reconciliation import scan_script
            listing = self.page.evaluate(scan_script())
            pages.guard(listing)
            self.cached = {(e['row']['encryptJobId'],e['row']['uniqueId']):(e['row'],e['position'])
                           for e in listing.get('entries',[])}
        return self.page

    def read(self, job):
        if job['platform']=='51job':
            if self.platform!='51job':
                self.close()
                self.platform = '51job'
                from job_agent.platforms.job51.status import read_applications
                try:
                    self.cached = read_applications(self.space)
                except Exception as error:
                    self.failure = error
                    raise
            if self.failure is not None:
                raise self.failure
            return job51_outcome(job, self.cached)
        if job['platform'] not in {'boss','zhilian'}:
            return evidence('still_unknown','unsupported_platform')
        platform = job['platform']
        try:
            page = self.records(platform)
        except Exception as error:
            self.failure = error
            raise
        if platform=='zhilian':
            from job_agent.platforms.zhilian import pages
            pages.guard(page.evaluate(pages.STATE_JS))
            result = application_outcome(job, self.cached, checked_epoch=time.time())
            # Missing or paginated rows prove nothing; the exact detail may still supply a negative receipt.
            return result if result['status']=='sent_confirmed' or result['evidence']['code']=='application_time_unavailable' else self.detail(job)
        from job_agent.platforms.boss import pages
        pages.guard(page.evaluate(pages.STATE_JS))
        rows = [v for (jid,_),v in self.cached.items() if jid==job['platform_job_id']]
        if len(rows)==1:
            row, position = rows[0]
            page.evaluate('''async () => {const e=document.querySelector('.user-list-content');
              if(e)e.scrollTop=POSITION;await new Promise(r=>setTimeout(r,150));}'''.replace('POSITION', str(position)))
            # The shared selector projects geometry; only click_projected performs
            # the ordinary selection of this existing conversation.
            from job_agent.platforms.boss.reconciliation import select_existing_session
            from job_agent.browser.base import BrowserError
            try:
                page.click_projected(select_existing_session(row))
                page.wait_for(pages.session_ready(row), timeout_ms=3000)
            except (BrowserError, pages.PlatformStateError) as error:
                if error.code in {'risk_control','login_required','space_unavailable','task_space'}:
                    raise
                result = self.detail(job)
                return result if result['status']!='still_unknown' else evidence('still_unknown','conversation_unavailable')
            pages.guard(page.evaluate(pages.STATE_JS))
            result = boss_outcome(job, page.evaluate(BOSS_MESSAGES_JS), checked_epoch=time.time())
            if result['status']=='sent_confirmed':
                return result
        return self.detail(job)

    def detail(self, job):
        from job_agent.platforms.boss import pages as boss
        from job_agent.platforms.zhilian import pages as zhilian
        pages = boss if job['platform']=='boss' else zhilian
        page = self.space.new_page()
        try:
            page.goto(job['url'], wait_ms=15000)
            state = page.evaluate(pages.STATE_JS)
            pages.guard(state)
            if pages.job_id(page.url())!=job['platform_job_id']:
                return evidence('still_unknown', 'detail_unavailable')
            page.wait_for(pages.JOB_READY)
            state = page.evaluate(pages.STATE_JS)
            identity = page.evaluate(pages.JOB_IDENTITY_JS if job['platform']=='boss' else '''() => ({
              title:document.querySelector('.summary-planes__title')?.innerText?.trim(),
              company:document.querySelector('.company-summary__name')?.innerText?.trim()})''')
            same = (isinstance(identity,dict) and identity.get('company')==job['company']
                    and identity.get('title')==job['title'] and job['company'] and job['title'])
            if same and pages.applied(state) is False:
                return evidence('not_sent', 'explicit_not_applied')
            return evidence('still_unknown', 'no_matching_record')
        except Exception as error:
            code = getattr(error,'code','browser_error')
            if code in {'login_required','risk_control','space_unavailable','task_space'}:
                raise
            return evidence('still_unknown', 'detail_unavailable')
        finally:
            page.close()
