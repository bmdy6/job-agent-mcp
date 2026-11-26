from dataclasses import dataclass
from enum import Enum
from typing import Literal

Platform = Literal['boss', 'zhilian', 'liepin', '51job', 'nowcoder', 'maimai']
PLATFORMS = ('boss', 'zhilian', 'liepin', '51job', 'nowcoder', 'maimai')


class Status(str, Enum):
    PENDING = 'pending'
    SENT = 'sent'
    FAILED = 'failed'
    UNVERIFIED = 'unverified'
    SKIPPED = 'skipped'


class Reason(str, Enum):
    PLATFORM_BUSY = 'platform_busy'
    NONE = ''  # No failure/defer reason.
    DISABLED = 'disabled'  # Platform disabled; keep pending.
    DAILY_LIMIT = 'daily_limit'  # Daily attempt budget exhausted; keep pending.
    COOLDOWN = 'cooldown'
    JOB_UNAVAILABLE = 'job_unavailable'

    HOURLY_LIMIT = 'hourly_limit'
    OUTSIDE_HOURS = 'outside_hours'
    ACCOUNT_TYPE = 'account_type'
    SESSION_UNKNOWN = 'session_unknown'
    ALREADY_APPLIED = 'already_applied'  # Site positively identified an earlier application.
    LOGIN_REQUIRED = 'login_required'  # Preflight could not confirm login.
    RISK_CONTROL = 'risk_control'
    TASK_SPACE = 'task_space'  # Named task space could not be obtained/verified.
    ATTACHMENT = 'attachment'  # Exact unique attachment name could not be verified before sending.
    PRECHECK_ERROR = 'precheck_error'  # Exception before any possible sending action.
    NOT_IMPLEMENTED = 'not_implemented'  # Adapter awaits P0 evidence.
    RESUME_FLOW_UNVERIFIED = 'resume_flow_unverified'  # No validated formal resume workflow.
    RESUME_ATTACHMENT_REQUIRED = 'resume_attachment_required'  # Exact configured attachment is required.
    INTERRUPTED = 'interrupted'  # Persisted before first effect; process may have died during effect.
    RECEIPT_UNKNOWN = 'receipt_unknown'  # No positive final receipt; do not retry.
    APPLICATION_CONFIRMED = 'application_confirmed_greeting_unknown'  # Original application proven; full greeting not yet verified.
    EFFECT_ERROR = 'effect_error'  # Exception after the effect boundary; outcome uncertain.
    NOT_STARTED = 'not_started'  # Adapter refused before any click; nothing was sent.


@dataclass(frozen=True)
class Job:
    platform: Platform
    job_id: str
    url: str

    def __post_init__(self):
        if self.platform not in PLATFORMS or not self.job_id.strip() or not self.url.strip():
            raise ValueError('Job requires a supported platform, stable site job_id and URL')


@dataclass(frozen=True)
class Record:
    job: Job
    status: Status = Status.PENDING
    reason: Reason = Reason.NONE
    attempted_at: float | None = None
    error_detail: str = ''
