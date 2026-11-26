from platforms.availability import wait_job_ready
"""One visible application effect; verify the official history independently of chat UI."""
from time import time
from browser.base import Browser, Page, PageTimeout
from engine.models import Job
from . import pages, receipt_reader


class ZhilianAdapter:
    platform = 'zhilian'
    greeting_mode = 'platform_default'

    def __init__(self, browser: Browser, settings):
        self.browser, self.settings = browser, settings
        self.page: Page | None = None
        self.space = None
        self.job = None
        self.identity = None
        self.greeting = None
        self.attempted_at = None
        self.attempted = False
        self.attachment_requested = False
        self.application_receipt_at = None
        self.result_pages = []

    def _setting(self, key, default):
        return self.settings.get(key, default) if isinstance(self.settings, dict) else getattr(self.settings, key, default)

    def open_job(self, job: Job, task_space: str) -> None:
        if self.page is not None:
            raise pages.PlatformStateError('先关闭上一个岗位页面')
        if job.platform != self.platform or pages.job_id(job.url) != job.job_id:
            raise ValueError('岗位平台或 ID 与 URL 不一致')
        self.job, self.greeting, self.identity = job, None, None
        self.attempted, self.attachment_requested = False, False
        self.attempted_at, self.application_receipt_at = None, None
        self.space = self.browser.space(task_space)
        self.page = self.space.new_page()
        try:
            self.page.goto(job.url, wait_ms=15000)
            self._guard()
            wait_job_ready(self.page, pages.JOB_READY)
            self._guard()
            if pages.job_id(self.page.url()) != job.job_id:
                raise pages.PlatformStateError('岗位跳转后 ID 不一致')
        except Exception:
            self.close()
            raise

    def bind_identity(self, identity):
        # Treat the immutable owned job identity as the receipt matching scope.
        if (identity.get('platform_job_id')!=self.job.job_id
                or identity.get('platform')!=self.platform
                or not identity.get('company') or not identity.get('title')):
            raise pages.PlatformStateError('原站回执核验缺少完整岗位身份；未提交')
        self.identity={k:identity[k] for k in ('platform_job_id','company','title')}

    def _guard(self):
        if self.page is None:
            raise pages.PlatformStateError('没有打开岗位')
        pages.guard(self.page.evaluate(pages.STATE_JS))

    def check_applied(self) -> bool:
        self._guard()
        if pages.job_id(self.page.url()) != self.job.job_id:
            raise pages.PlatformStateError('页面已不是目标岗位')
        return pages.applied(self.page.evaluate(pages.STATE_JS))

    def send_greeting(self, greeting: str) -> None:
        if self.attempted:
            raise pages.PlatformStateError('同一会话禁止重复提交')
        maximum = min(500, self._setting('greeting_max_length', 500))
        if not greeting.strip() or pages.utf16_length(greeting) > maximum:
            raise ValueError('招呼语为空或超长；拒绝截断')
        if self._setting('greeting_mode', self.greeting_mode) != self.greeting_mode:
            raise ValueError('招呼方式与平台架构不一致')
        if self._setting('attachment_name', ''):
            raise pages.PlatformStateError('附件流程尚待授权取证；不提交申请')
        if self.identity is None:
            raise pages.PlatformStateError('没有冻结岗位身份；未提交')
        if self.check_applied():
            raise pages.PlatformStateError('已沟通，禁止重复申请')
        self.greeting, self.attempted, self.attempted_at = greeting, True, time()
        before={t.label for t in self.space.tabs()} if hasattr(self.space,'tabs') else set()
        try:
            self.page.click(pages.START)
        except PageTimeout as exc:
            # The effect acknowledgement itself is unknown. No action replay.
            raise PageTimeout(f'zhilian.submit_application: {exc.code}') from None
        if before:
            from urllib.parse import urlsplit
            for tab in self.space.tabs():
                if tab.label in before: continue
                if urlsplit(tab.url).hostname not in pages.JOB_HOSTS | {'i.zhaopin.com'}:
                    raise pages.PlatformStateError('zhilian.post_click: 新页面域名无法核验；不重放申请')
                observed=self.space.inspect_page(tab.label)
                pages.guard(observed.evaluate(pages.STATE_JS))
                self.space.claim_page(observed,before)
                self.result_pages.append(observed)
        # The success dialog is not an application receipt. It is also not a
        # prerequisite to read the official application history after a known click.
        # Never click "continue chat" to turn a successful application into a timeout.
        try:
            self.page.wait_for(pages.SUCCESS_MODAL)
        except PageTimeout:
            pass
        # A submit may open a separate result tab. Receipts are read independently;
        # never click Continue/Apply again or require the original page to survive.

    def choose_attachment(self, attachment_name: str) -> bool:
        self.attachment_requested = bool(attachment_name)
        return not self.attachment_requested

    def confirm_receipt(self) -> bool:
        if not self.attempted or self.greeting is None or self.attachment_requested:
            return False
        self._guard()
        evidence={**self.identity,'attempted_at':self.attempted_at,'greeting':self.greeting}
        try:
            self.application_receipt_at=receipt_reader.read_application(self.space,evidence)
        except PageTimeout:
            return False
        if not self.application_receipt_at:
            return False
        try:
            return receipt_reader.read_greeting(self.space,evidence)
        except PageTimeout:
            # Preserve the positive application evidence; full greeting remains unknown.
            return False

    def close(self) -> None:
        for page in self.result_pages:
            page.close()
        self.result_pages=[]
        if self.page is not None:
            page = self.page
            page.close()
            self.page = None
