"""No browser imports. P0 must establish effect/receipt semantics before implementation."""
from typing import Protocol
from .models import Job


class PlatformAdapter(Protocol):
    # One adapter session owns only its per-job temporary tab. open_job must be read-only.
    def open_job(self, job: Job, task_space: str) -> None: ...
    # True = positively applied, False = positively not applied; ambiguity must raise.
    def check_applied(self) -> bool: ...
    def send_greeting(self, greeting: str) -> None: ...
    # True only after exact unique attachment selection is read back; may have effects.
    def choose_attachment(self, attachment_name: str) -> bool: ...
    # True only with a positive final receipt covering greeting AND attachment.
    def confirm_receipt(self) -> bool: ...
    # Close ONLY the session's temporary tab, even after partial open failures.
    def close(self) -> None: ...


class PendingP0Adapter:
    def open_job(self, job: Job, task_space: str) -> None:
        raise NotImplementedError('等待 P0 取证：打开岗位')

    def check_applied(self) -> bool:
        raise NotImplementedError('等待 P0 取证：确认已投状态')

    def send_greeting(self, greeting: str) -> None:
        raise NotImplementedError('等待 P0 取证：发送招呼语及申请边界')

    def choose_attachment(self, attachment_name: str) -> bool:
        raise NotImplementedError('等待 P0 取证：附件唯一选择和读回')

    def confirm_receipt(self) -> bool:
        raise NotImplementedError('等待 P0 取证：最终回执')

    def close(self) -> None:
        raise NotImplementedError('等待 P0 取证：仅关闭临时标签页')


class BossAdapter(PendingP0Adapter):
    """BOSS：等待 P0 取证。"""


class ZhilianAdapter(PendingP0Adapter):
    """智联：等待 P0 取证。"""


class LiepinAdapter(PendingP0Adapter):
    """猎聘：等待 P0 取证。"""


class Job51Adapter(PendingP0Adapter):
    """51job：等待 P0 取证。"""


class FakeAdapter:
    """Pure in-memory simulation; deliberately no arbitrary callbacks or browser handles."""
    def __init__(self, *, applied=False, receipt=True, attachment=True):
        self.applied = applied
        self.receipt = receipt
        self.attachment = attachment
        self.calls = []
        self.greetings = []

    def open_job(self, job, task_space):
        self.calls.append('open_job')

    def check_applied(self):
        self.calls.append('check_applied')
        return self.applied

    def send_greeting(self, greeting):
        self.calls.append('send_greeting')
        self.greetings.append(greeting)

    def choose_attachment(self, attachment_name):
        self.calls.append('choose_attachment')
        return self.attachment

    def confirm_receipt(self):
        self.calls.append('confirm_receipt')
        return self.receipt

    def close(self):
        self.calls.append('close')
