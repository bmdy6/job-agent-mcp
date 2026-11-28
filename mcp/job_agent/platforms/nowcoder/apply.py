"""One visible application; positive original-site record required, never replay."""
from time import time, monotonic, sleep
from . import pages, flow

class NowcoderAdapter:
    platform = 'nowcoder'

    def __init__(self,browser,settings):
        self.browser,self.settings=browser,settings
        self.page=None
        self.submitted=False
        self.opened_popup=False
        self.selected=None
        self.resume_recorder=None

    def bind_resume_recorder(self,recorder):
        self.resume_recorder=recorder

    def _setting(self,key,default=''):
        return self.settings.get(key,default) if isinstance(self.settings,dict) else getattr(self.settings,key,default)

    def open_job(self,job,task_space):
        if self.page is not None:raise ValueError('先关闭上一个岗位')
        if job.platform!=self.platform or pages.clean_job_url(job.url).rsplit('/',1)[-1]!=job.job_id:
            raise ValueError('牛客岗位身份不匹配')
        self.job=job
        self.submitted=self.opened_popup=False
        self.selected=None
        self.page=self.browser.space(task_space).new_page()
        self.page.goto(pages.clean_job_url(job.url),wait_ms=15000)
        self.page.wait_for('.job-detail-word')
        self._state()

    def _state(self):
        state=self.page.evaluate(pages.GUARD_JS)
        pages.guard(state)
        if state.get('logged_in') is not True:raise pages.LoginRequired('牛客登录未确认')
        if pages.clean_job_url(self.page.url()).rsplit('/',1)[-1]!=self.job.job_id:
            raise pages.Unverified('牛客页面岗位变化')
        d=self.page.evaluate(flow.JOB_JS)
        if not isinstance(d,dict) or d.get('count')!=1 or str(d.get('id'))!=self.job.job_id:
            raise pages.Unverified('牛客岗位组件身份不明确')
        return d

    def check_applied(self):
        d=self._state()
        if type(d.get('applied')) is not bool:raise pages.Unverified('牛客已投状态不明确')
        return d['applied']

    def send_greeting(self,greeting):
        if self.submitted or self.opened_popup:raise pages.Unverified('禁止重复申请')
        if greeting or self._setting('greeting_mode')!='none':
            raise flow.NotStarted('牛客只支持简历申请，须配置空招呼语与 greeting_mode:none')
        if self._setting('attachment_name'):
            raise flow.NotStarted('牛客附件唯一选择尚未实站验收；不提交')
        d=self._state()
        if d.get('official') is not False or d.get('button')!='立即申请' or d.get('applied') is not False:
            raise flow.NotStarted('官网跳转、关闭或已申请岗位不开始投递')
        self.selected=flow.resume(self.page.evaluate(flow.RESUMES_JS))
        self.opened_popup=True
        self.page.click(flow.ENTRY)
        self.page.wait_for('.delivery-resume-popup')
        self._state()
        state=self.page.evaluate(flow.POPUP_JS)
        if isinstance(state,dict) and state.get('count')==1 and str(state.get('job_id'))==self.job.job_id:
            selection=flow.resume_selection(self.selected,state)
            # Persist before any refusal or submit click, so crashes cannot lose the selection.
            if self.resume_recorder is not None:self.resume_recorder(selection)
            if not selection['matches_expected']:
                raise pages.Unverified('申请弹窗实际简历与预期默认在线简历不一致；未提交，不重投')
        if (not isinstance(state,dict) or state.get('count')!=1 or state.get('ready') is not True
            or str(state.get('job_id'))!=self.job.job_id or state.get('forms') is not False
            or str(state.get('resume_id'))!=str(self.selected['id']) or state.get('resume_done') is not True
            or state.get('submit_count')!=1):
            # The known entry only opens the popup; no form submit has occurred.
            raise flow.NotStarted('申请弹窗有问卷/志愿/简历不匹配或未就绪，未提交')
        self.attempted_at=time()
        self.submitted=True  # Set BEFORE dispatch: a timeout can never authorize replay.
        self.page.click(flow.SUBMIT)

    def choose_attachment(self,attachment_name):
        return False

    def confirm_receipt(self):
        if not self.submitted:return False
        self._state()
        deadline=monotonic()+10
        while True:
            d=self.page.evaluate(flow.POPUP_JS)
            if isinstance(d,dict) and d.get('success') is True:break
            self._state()
            if monotonic()>=deadline:return False
            sleep(.25)
        if (not isinstance(d,dict) or d.get('success') is not True or d.get('ack') is not True
            or d.get('successful_resume_id')!=self.selected['id']):return False
        rows,info=flow.record_rows(self.page.evaluate(flow.records_js()))
        # New applications must appear on the newest page; no retry or inferred success.
        return flow.receipt(rows,self.job.job_id,d.get('deliver_id'),self.attempted_at,time())

    def close(self):
        if self.page is not None:
            self.page.close()
            self.page=None

PlatformAdapter=NowcoderAdapter
