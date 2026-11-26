"""Synchronous, resumable offline queue; no MCP or browser integration in P1 skeleton."""
import random
import time
from datetime import datetime
from typing import Callable, Iterable
from zoneinfo import ZoneInfo
from .adapters import FakeAdapter, PlatformAdapter
from .config import EngineSettings
from .models import Job, Record, Status, Reason
from .resume_contract import resume_block_reason
from .preflight import FakePreflight, Preflight
from .spaces import FakeTaskSpaces, TaskSpaces
from .store import DeliveryStore
from .rate import RateDeferred,RateLimiter
from platforms.greeting import prepare_greeting


class DeliveryEngine:
    def __init__(self, store: DeliveryStore, settings: EngineSettings,
                 adapters: dict[str, PlatformAdapter], preflight: Preflight, spaces: TaskSpaces,
                 *, clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep, safety=None, seed=None):
        self.store, self.settings, self.adapters = store, settings, adapters
        self.preflight, self.spaces, self.clock, self.sleep = preflight, spaces, clock, sleep
        self.random=random.Random(seed)
        self.safety=safety

    def enqueue(self, jobs: Iterable[Job]) -> int:
        with self.store.exclusive():
            return sum(self.store.add(job) for job in jobs)

    def run(self, *, batch_token: str | None = None, should_stop: Callable[[], bool] = lambda: False) -> list[Record]:
        cfg = EngineSettings.model_validate(self.settings.model_dump())
        live = not cfg.dry_run
        if live:
            if not cfg.live_send_enabled or self.store.dry_run or not batch_token:
                raise NotImplementedError('Live delivery requires opt-in and a confirmed batch')
            from platforms.registry import ADAPTERS
            from platforms.preflight import AdapterPreflight
            if (type(self.preflight) is not AdapterPreflight or type(self.spaces) is not TaskSpaces
                    or any(type(a) is not ADAPTERS.get(p) for p, a in self.adapters.items())):
                raise ValueError('Live delivery requires registered concrete dependencies')
            if self.store.get_batch(batch_token)['status'] != 'confirmed':
                raise ValueError('Live delivery requires a confirmed batch')
        elif (not self.store.dry_run or type(self.preflight) is not FakePreflight
                or type(self.spaces) is not FakeTaskSpaces
                or any(type(a) is not FakeAdapter for a in self.adapters.values())):
            raise ValueError('Dry-run accepts only built-in offline fake dependencies')
        with self.store.exclusive():
            greetings = {p: prepare_greeting(p, s.greeting, s.greeting_newline_separator)
                         for p, s in cfg.platforms.items()}
            self.store.prepare_run(cfg.model_dump(),batch_token=batch_token,greetings=greetings)
            checked={}
            self._inactive_platforms=set()
            if live:
                from store.automation import AutomationRepository
                limiter=RateLimiter(AutomationRepository(self.store),clock=self.clock,sleep=self.sleep,should_stop=should_stop)
            for record in self._rounds(should_stop,batch_token):
                if should_stop():
                    break
                job = record.job
                if batch_token and job not in (self.store.execution_jobs(batch_token) if hasattr(self.store, 'execution_jobs') else self.store.batch_jobs(batch_token)):
                    continue
                if record.status != Status.PENDING:
                    continue
                if any(r.reason == Reason.TASK_SPACE and (not batch_token or r.job in self.store.batch_jobs(batch_token)) for r in self.store.records()):
                    break
                if job.platform in self.store.paused_platforms():
                    self.store.update(job, Status.PENDING, Reason.RISK_CONTROL)
                    continue
                pcfg = cfg.platforms[job.platform]
                if not pcfg.enabled:
                    self._inactive_platforms.add(job.platform)
                    self.store.update(job, Status.PENDING, Reason.DISABLED)
                    continue
                resume_hold = resume_block_reason(job.platform, pcfg) if live else None
                if resume_hold:
                    # No browser, attempt timestamp or external effect for a capability hold.
                    self.store.update(job, Status.PENDING, resume_hold)
                    continue
                if live:
                    if self.safety is None: raise ValueError('Live delivery requires session safety preflight')
                    if job.platform not in checked:
                        try:
                            self.safety.require(job.platform)
                            checked[job.platform]=''
                        except RateDeferred as error:
                            checked[job.platform]=error.code
                    if checked[job.platform]:
                        self._inactive_platforms.add(job.platform)
                        code=checked[job.platform]
                        self.store.update(job,Status.PENDING,Reason(code) if code in Reason._value2member_map_ else Reason.SESSION_UNKNOWN)
                        continue
                rows = [r for r in self.store.records() if r.job.platform == job.platform]
                if any((r.status == Status.UNVERIFIED or r.reason == Reason.RISK_CONTROL or (r.reason == Reason.LOGIN_REQUIRED and r.status != Status.PENDING))
                       and (not batch_token or r.job in self.store.batch_jobs(batch_token)) for r in rows):
                    continue
                attempted = self.store.attempt_times(job.platform)
                # Persisted interval survives restarts. Polling keeps cooperative stop responsive.
                deadline = 0 if live else max(attempted, default=0) + pcfg.interval_seconds
                if self.clock() < deadline:
                    self._retry.append(record)
                    continue
                if should_stop():
                    break
                day = lambda ts: datetime.fromtimestamp(ts, ZoneInfo(cfg.timezone)).date()
                if not live and sum(day(at) == day(self.clock()) for at in attempted) >= pcfg.daily_limit:
                    self.store.update(job, Status.PENDING, Reason.DAILY_LIMIT)
                    continue
                if live:
                    try:
                        limiter.acquire(job.platform,'delivery',pcfg.rate_policy(),history=attempted,block=False)
                    except RateDeferred as error:
                        if error.code=='stopped':break
                        if error.code=='cooldown':
                            self._retry.append(record)
                            continue
                        self.store.update(job,Status.PENDING,Reason(error.code))
                        continue
                self._deliver(job, pcfg, cfg.task_space_name)
            return self.store.records()

    def _rounds(self, should_stop, batch_token=None):
        allowed=set(self.store.execution_jobs(batch_token)) if batch_token else None
        pending = [r for r in self.store.records() if r.status==Status.PENDING and (allowed is None or r.job in allowed)]
        while pending and not should_stop():
            groups = {}
            for record in pending:
                groups.setdefault(record.job.platform, []).append(record)
            platforms = list(groups)
            self.random.shuffle(platforms)
            self._retry = []
            for platform in platforms:
                yield groups[platform].pop(0)
            pending = [r for r in self._retry + [r for rows in groups.values() for r in rows]
                       if r.job.platform not in getattr(self,'_inactive_platforms',set())]
            if pending:
                # Only idle after all other platforms have had their turn.
                self.sleep(.25)

    def _deliver(self, job, cfg, space_name):
        adapter = self.adapters.get(job.platform)
        effect = False
        opened = False
        checking_applied = False
        try:
            if adapter is None:
                self.store.update(job, Status.FAILED, Reason.NOT_IMPLEMENTED)
                return
            space = self.spaces.ensure(space_name)
            if self.preflight.task_space_valid(space) is not True:
                self.store.update(job, Status.FAILED, Reason.TASK_SPACE)
                return
            if not self.settings.dry_run:
                opened = True
                adapter.open_job(job, space)
                if hasattr(adapter, "page") and hasattr(adapter.page,"read_detail"): adapter.page.read_detail()
            if self.preflight.logged_in(job.platform, space) is not True:
                self.store.update(job, Status.FAILED, Reason.LOGIN_REQUIRED)
                return
            if cfg.attachment_name and not (not self.settings.dry_run and job.platform == '51job') and self.preflight.attachment_matches(job.platform, cfg.attachment_name) is not True:
                self.store.update(job, Status.FAILED, Reason.ATTACHMENT)
                return
            if not opened:
                opened = True
                adapter.open_job(job, space)
            if not self.settings.dry_run:
                from platforms.hr import enrich_open_job
                enrich_open_job(self.store, job, adapter.page)
                if hasattr(adapter,'bind_identity'):
                    adapter.bind_identity(self.store.identity(job))
            if self.store.duplicate_for(job, self.settings.cooldown, self.clock()):
                self.store.update(job, Status.SKIPPED, Reason.COOLDOWN)
                return
            checking_applied = True
            applied = adapter.check_applied()
            checking_applied = False
            if applied is True:
                self.store.update(job, Status.SKIPPED, Reason.ALREADY_APPLIED)
                return
            if applied is not False:
                self.store.update(job, Status.UNVERIFIED, Reason.RECEIPT_UNKNOWN)
                return
            # Commit BEFORE the first possible external effect. Crash cannot become a retry.
            self.store.update(job, Status.UNVERIFIED, Reason.INTERRUPTED, self.clock())
            effect = True
            adapter.send_greeting(prepare_greeting(job.platform, cfg.greeting, cfg.greeting_newline_separator))
            if cfg.attachment_name and adapter.choose_attachment(cfg.attachment_name) is not True:
                self.store.update(job, Status.UNVERIFIED, Reason.ATTACHMENT)
                return
            if adapter.confirm_receipt() is True:
                self.store.update(job, Status.SENT, Reason.NONE)
            elif job.platform=='zhilian' and getattr(adapter,'application_receipt_at',None):
                # Keep the no-retry state. Application success is a distinct,
                # positive business receipt, not proof of greeting/attachment.
                self.store.update(job, Status.UNVERIFIED, Reason.APPLICATION_CONFIRMED,
                    error_detail=f'原站申请时间: {adapter.application_receipt_at}')
            else:
                self.store.update(job, Status.UNVERIFIED, Reason.RECEIPT_UNKNOWN)
        except Exception as exc:
            if getattr(exc,'code','') == 'not_started':
                # The adapter proved nothing was clicked; this is a skip, not an unknown outcome.
                self.store.update(job, Status.SKIPPED, Reason.NOT_STARTED, error_detail=str(exc)[:2000])
                return
            if getattr(exc,'code','') in {'risk_control','unexpected_redirect'} and self.safety is not None:
                # Keep the original unverified/effect boundary; persistence precedes notification.
                self.safety.repo.pause(job.platform,getattr(exc,'code'))
                try:self.safety.alert(job.platform,'risk_control')
                except Exception:pass
            elif getattr(exc,'code','') == 'risk_control':
                self.store.pause_platform(job.platform)
            if effect:
                code=getattr(exc,'code','')
                if code in {'risk_control','login_required','task_space','space_unavailable'}:
                    reason={'risk_control':Reason.RISK_CONTROL,'login_required':Reason.LOGIN_REQUIRED,
                            'task_space':Reason.TASK_SPACE,'space_unavailable':Reason.TASK_SPACE}[code]
                    self.store.update(job,Status.UNVERIFIED,reason,error_detail=str(exc)[:2000])
                elif job.platform=='zhilian' and getattr(adapter,'application_receipt_at',None):
                    self.store.update(job,Status.UNVERIFIED,Reason.APPLICATION_CONFIRMED,
                        error_detail=f'原站申请时间: {adapter.application_receipt_at}; 完整回执未确认')
                else:
                    self.store.update(job, Status.UNVERIFIED, Reason.EFFECT_ERROR, error_detail=str(exc)[:2000])
            elif getattr(exc, 'code', '') == 'job_unavailable':
                self.store.update(job, Status.SKIPPED, Reason.JOB_UNAVAILABLE)
            elif getattr(exc, 'code', '') in {'space_unavailable', 'task_space'}:
                self.store.update(job, Status.FAILED, Reason.TASK_SPACE, error_detail=str(exc)[:2000])
            elif getattr(exc, 'code', '') in {'login_required', 'risk_control'}:
                reason = Reason.LOGIN_REQUIRED if exc.code == 'login_required' else Reason.RISK_CONTROL
                self.store.update(job, Status.FAILED, reason, error_detail=str(exc)[:2000])
            elif isinstance(exc, NotImplementedError):
                self.store.update(job, Status.FAILED, Reason.NOT_IMPLEMENTED, error_detail=str(exc)[:2000])
            elif checking_applied:
                self.store.update(job, Status.UNVERIFIED, Reason.RECEIPT_UNKNOWN, error_detail=str(exc)[:2000])
            else:
                self.store.update(job, Status.FAILED, Reason.PRECHECK_ERROR, error_detail=str(exc)[:2000])
        finally:
            if opened:
                try:
                    adapter.close()
                except Exception:
                    # Closing an owned tab must not erase a confirmed receipt; stop this run.
                    raise RuntimeError('Temporary tab cleanup failed; stop queue') from None
