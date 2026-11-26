from platforms.availability import wait_job_ready
from . import pages

class Job51Adapter:
    def __init__(self, browser, settings):
        self.browser, self.settings = browser, settings
        self.page = None
        self._attempted = False
        self._attachment = None
        self._logs = None

    def open_job(self, job, task_space):
        if self.page is not None:
            raise pages.Unverified('先关闭上一个临时页')
        self._attempted, self._attachment, self._logs = False, None, None
        self.job = job
        self.page = self.browser.space(task_space).new_page()
        self.page.goto(job.url, wait_ms=15000)
        wait_job_ready(self.page, pages.READY)

    def check_applied(self):
        if self.page is None:
            raise pages.Unverified('岗位未打开')
        return pages.applied(self.page.evaluate(pages.JOB_JS), self.job.job_id)

    def send_greeting(self, greeting):
        limit = getattr(self.settings, 'greeting_max_length', 0)
        if len(greeting.encode('utf-16-le')) // 2 > limit:
            raise ValueError('51job 招呼语超过配置上限；不截断')
        if self._attempted or self.check_applied():
            raise pages.Unverified('禁止重复申请')
        # Register before any effect; if observation is unavailable, do not apply.
        if self.page.evaluate(pages.OBSERVE_JS) is not True:
            raise pages.Unverified('附件回执观察不可用，未申请')
        self._attempted = True
        self.page.click(pages.APPLY)
        # Desktop has no verified first-message editor. Application-only is allowed
        # by architecture; never click a QR/communicate action to test availability.

    def choose_attachment(self, attachment_name):
        if not self._attempted:
            raise pages.Unverified('尚未申请')
        stage = 'wait_rows'
        try:
            self.page.wait_for(pages.ROWS)
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
