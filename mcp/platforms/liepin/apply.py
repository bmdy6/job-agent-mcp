from platforms.availability import wait_job_ready
"""PlatformAdapter implementation; no database, driver or engine orchestration here."""
from . import pages
from platforms.greeting import prepare_greeting

class LiepinAdapter:
    def __init__(self, browser, settings):
        self.browser, self.settings = browser, settings
        self.page = None
        self._greeting = None
        self._before = set()
        self._attempted = False

    def open_job(self, job, task_space):
        if self.page is not None:
            raise pages.Unverified('先关闭上一个临时页')
        self._greeting, self._attempted, self._before = None, False, set()
        self.page = self.browser.space(task_space).new_page()
        self.page.goto(job.url, wait_ms=15000)
        wait_job_ready(self.page, pages.JOB_READY)
        self._guard()

    def _guard(self):
        if self.page is None:
            raise pages.Unverified('岗位未打开')
        pages.guard(self.page.evaluate(pages.GUARD_JS))

    def check_applied(self):
        self._guard()
        return pages.applied(self.page.evaluate(pages.JOB_JS))

    def send_greeting(self, greeting):
        greeting = prepare_greeting('liepin', greeting,
            getattr(self.settings, 'greeting_newline_separator', '；'))
        limit = min(800, getattr(self.settings, 'greeting_max_length', 800))
        if not greeting or pages.utf16_length(greeting) > limit:
            raise ValueError('猎聘招呼语超过上限或为空；不截断')
        if getattr(self.settings, 'attachment_name', ''):
            raise pages.Unverified('猎聘附件发送尚未取证，未开始沟通')
        if self._attempted or self.check_applied():
            raise pages.Unverified('禁止重复沟通')
        self._attempted = True  # First click may send the platform preset.
        self.page.click(pages.CHAT)
        self.page.wait_for(pages.EDITOR)
        self._guard()
        before = self.page.evaluate(pages.MESSAGES_JS)
        if not isinstance(before, dict) or not before.get('ready'):
            raise pages.Unverified('聊天未就绪，可能已发送平台预设')
        self._before = {m.get('msgId') for m in before.get('items', [])}
        self.page.fill(pages.EDITOR, greeting)
        if self.page.evaluate(pages.EDITOR_JS) != greeting:
            raise pages.Unverified('全文输入读回不一致')
        self.page.press('Enter')
        self._greeting = greeting

    def choose_attachment(self, attachment_name):
        if attachment_name:
            raise pages.Unverified('猎聘附件发送尚未取证')
        return True

    def confirm_greeting_receipt(self):
        """An exact outgoing text receipt proves contact, not application."""
        self._guard()
        data = self.page.evaluate(pages.MESSAGES_JS)
        return bool(self._greeting and isinstance(data, dict) and data.get('ready') and any(
            m.get('msgId') and m['msgId'] not in self._before and m.get('msg') == self._greeting
            and str(m.get('direction')) == '0' and m.get('extType') == 1
            for m in data.get('items', [])))

    def confirm_receipt(self):
        # No verified formal application or attachment receipt in this adapter.
        # Never allow an outgoing greeting to become a resume success.
        return False

    def close(self):
        if self.page is not None:
            self.page.close()
            self.page = None

PlatformAdapter = LiepinAdapter
