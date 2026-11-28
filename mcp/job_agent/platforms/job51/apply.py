from job_agent.platforms.availability import wait_job_ready
from job_agent.platforms.job51 import pages
from job_agent.platforms.job51 import status as application_status
from job_agent.browser.base import PageTimeout
import time

class Job51Adapter:
    def __init__(self, browser, settings):
        self.browser, self.settings = browser, settings
        self.page = None
        self._attempted = False
        self._attachment = None
        self._logs = None
        self._attempted_at = None
        self.application_receipt = None

    def open_job(self, job, task_space):
        if self.page is not None:
            raise pages.Unverified('先关闭上一个临时页')
        self._attempted, self._attachment, self._logs = False, None, None
        self._attempted_at, self.application_receipt = None, None
        self.job = job
        self.space = self.browser.space(task_space)
        self.page = self.space.new_page()
        self.page.goto(job.url, wait_ms=15000)
        wait_job_ready(self.page, pages.READY)

    def check_applied(self):
        if self.page is None:
            raise pages.Unverified('岗位未打开')
        return pages.applied(self.page.evaluate(pages.JOB_JS), self.job.job_id)

    def send_greeting(self, greeting):
        if getattr(self.settings, 'greeting_mode', 'none') != 'none':
            raise pages.NotStarted('51job 桌面招呼语编辑器与发送回执未验证；未申请')
        limit = getattr(self.settings, 'greeting_max_length', 0)
        if len(greeting.encode('utf-16-le')) // 2 > limit:
            raise ValueError('51job 招呼语超过配置上限；不截断')
        if self._attempted or self.check_applied():
            raise pages.Unverified('禁止重复申请')
        # Register before any effect; if observation is unavailable, do not apply.
        if self.page.evaluate(pages.OBSERVE_JS) is not True:
            raise pages.Unverified('附件回执观察不可用，未申请')
        for _ in range(2):
            try:
                prepared = self.page.evaluate(pages.PREPARE_APPLY_JS)
            except Exception as exc:
                if getattr(exc, 'code', '') in {'risk_control', 'unexpected_redirect', 'login_required', 'task_space', 'space_unavailable'}:
                    raise
                raise pages.NotStarted(f'申请按钮准备失败，未点击：{exc}') from exc
            if prepared is not True:
                raise pages.NotStarted('正文申请按钮不唯一、不可见或被遮挡，未点击')
            # Scrolling/layout settling must not bypass a newly applied state.
            if self.check_applied():
                raise pages.Unverified('禁止重复申请')
            self._attempted = True
            self._attempted_at = time.time()
            try:
                self.page.click(pages.APPLY)
                return
            except Exception as exc:
                if getattr(exc, 'code', '') in {'risk_control', 'unexpected_redirect', 'login_required', 'task_space', 'space_unavailable'}:
                    raise
                if not any(step in str(exc) for step in pages.PRE_CLICK_STEPS):
                    raise
                # Only a proven pre-mousedown failure permits one fresh preparation.
                # Preserve uncertainty for mouse/input errors; never select nth=0.
                self._attempted = False
                self._attempted_at = None
                last = exc
        raise pages.NotStarted(f'申请按钮未能定位，未点击：{last}') from last
        # Desktop has no verified first-message editor. Application-only is allowed
        # by architecture; never click a QR/communicate action to test availability.

    def choose_attachment(self, attachment_name):
        if not self._attempted:
            raise pages.Unverified('尚未申请')
        stage = 'wait_rows'
        try:
            try:
                self.page.wait_for(pages.ROWS)
            except PageTimeout:
                # A successful application may not offer a file dialog. Read a
                # separate history page; never click Apply/Continue to recover.
                stage = 'read_application'
                self.application_receipt = application_status.read_application_receipt(
                    self.space, self.job.job_id, self._attempted_at)
                if self.application_receipt:
                    raise pages.AttachmentUnverified(
                        '我的申请已确认、附件未确认：原站仅提供申请简历名称，未提供指定附件回执（不重投）')
                raise pages.AttachmentUnverified(
                    '申请与附件未确认：未找到同岗位同日申请记录（不重投）')
            stage = 'read_rows'
            data = self.page.evaluate(pages.ATTACHMENT_JS)
            row = pages.attachment_selection(data, attachment_name)
            logs = list(data['cvLogIdList'])
            stage = 'select_row'
            self.page.click(f'{pages.ROWS} >> nth={int(row["index"])}')
            stage = 'read_selection'
            selected = self.page.evaluate(pages.ATTACHMENT_JS)
            readback = pages.attachment_selection(selected, attachment_name)
            if (readback['attachResumeId'] != row['attachResumeId'] or not readback.get('selected')
                    or selected.get('selectedAttachmentResumeId') != row['attachResumeId']
                    or selected.get('cvLogIdList') != logs):
                raise pages.AttachmentUnverified('已申请、附件未确认：选中态读回不一致')
            self._attachment, self._logs = row['attachResumeId'], logs
            stage = 'send_attachment'
            self.page.click(pages.SEND_ATTACHMENT)
            stage = 'wait_receipt'
            self.page.wait_for(pages.ATTACHMENT_RECEIPT)
            # Event receipt must say sendStatus=1, correct cvLogId and attachment ID.
            receipt_data = self.page.evaluate(pages.ATTACHMENT_JS)
            if not pages.attachment_receipt(receipt_data, self._attachment, logs):
                status = (receipt_data.get('receipt') or {}).get('sendStatus') if isinstance(receipt_data, dict) else None
                label = '0' if status == 0 else 'unknown'
                raise pages.AttachmentUnverified(f'已申请、附件未确认：未取得附件成功回执 sendStatus={label}')
            return True
        except pages.AttachmentUnverified:
            raise
        except Exception as exc:
            if getattr(exc, 'code', '') in {'risk_control', 'unexpected_redirect', 'login_required', 'task_space', 'space_unavailable'}:
                raise  # Preserve safety codes so the engine pauses/stops correctly.
            raise pages.AttachmentUnverified(
                f'已申请、附件未确认：附件选择或读回失败 stage={stage} error_type={type(exc).__name__}') from exc

    def confirm_receipt(self):
        return bool(self._attachment and pages.attachment_receipt(
            self.page.evaluate(pages.ATTACHMENT_JS), self._attachment, self._logs))

    def close(self):
        if self.page is not None:
            self.page.close()
            self.page = None

PlatformAdapter = Job51Adapter
