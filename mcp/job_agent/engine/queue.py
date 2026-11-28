"""Synchronous, resumable offline queue; no MCP or browser integration in P1 skeleton."""
from job_agent.observability import observed, report_error
import logging
from contextlib import ExitStack
from job_agent.browser.platform_lock import platform_lock, PlatformBusy
import random
import time
from datetime import datetime
from typing import Callable, Iterable
from zoneinfo import ZoneInfo
from job_agent.engine.adapters import FakeAdapter, PlatformAdapter
from job_agent.config.engine import EngineSettings
from job_agent.engine.models import Job, Record, Status, Reason
from job_agent.engine.resume_contract import resume_block_reason
from job_agent.config.calendar import delivery_policy, effective_limit
from job_agent.engine.preflight import FakePreflight, Preflight
from job_agent.engine.spaces import FakeTaskSpaces, TaskSpaces
from job_agent.engine.store import DeliveryStore
from job_agent.engine.rate import RateDeferred,RateLimiter
from job_agent.platforms.greeting import prepare_greeting
from job_agent.platforms.spec import spec_for


log = logging.getLogger(__name__)


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

    @observed('delivery')
    def run(self, *, batch_token: str | None = None, should_stop: Callable[[], bool] = lambda: False) -> list[Record]:
        from job_agent.service.automation import allowed
        if not allowed('delivery'):
            return self.store.records()
        original_stop = should_stop
        should_stop = lambda: original_stop() or not allowed('delivery')
        cfg = EngineSettings.model_validate(self.settings.model_dump())
        live = not cfg.dry_run
        if live:
            if not cfg.live_send_enabled or self.store.dry_run or not batch_token:
                raise NotImplementedError('Live delivery requires opt-in and a confirmed batch')
            from job_agent.platforms.registry import ADAPTERS
            from job_agent.platforms.preflight import AdapterPreflight
            if (type(self.preflight) is not AdapterPreflight or type(self.spaces) is not TaskSpaces
                    or any(type(a) is not ADAPTERS.get(p) for p, a in self.adapters.items())):
                raise ValueError('Live delivery requires registered concrete dependencies')
            if self.store.get_batch(batch_token)['status'] != 'confirmed':
                raise ValueError('Live delivery requires a confirmed batch')
        elif (not self.store.dry_run or type(self.preflight) is not FakePreflight
                or type(self.spaces) is not FakeTaskSpaces
                or any(type(a) is not FakeAdapter for a in self.adapters.values())):
            raise ValueError('Dry-run accepts only built-in offline fake dependencies')
        with self.store.exclusive(), ExitStack() as locks:
            locked_platforms=set()
            busy_platforms=set()
            busy_attempts={}
            greetings = {p: prepare_greeting(p, s.greeting, s.greeting_newline_separator,
                         paragraph_separator=s.greeting_paragraph_separator)
                         for p, s in cfg.platforms.items()}
            self.store.prepare_run(cfg.model_dump(),batch_token=batch_token,greetings=greetings)
            from job_agent.store.followup import batch_options
            self._followup = batch_options(self.store,batch_token) if hasattr(self.store,'get_batch') else None
            self._batch_token = batch_token
            self._run_jobs = set(self.store.execution_jobs(batch_token)) if batch_token else {r.job for r in self.store.records()}
            checked={}
            self._inactive_platforms=set()
            if live:
                from job_agent.store.automation import AutomationRepository
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
                if self._followup:
                    pcfg = pcfg.model_copy(update={'attachment_name':''})
                if not pcfg.enabled:
                    self._inactive_platforms.add(job.platform)
                    self.store.update(job, Status.PENDING, Reason.DISABLED)
                    continue
                if self._followup:
                    from job_agent.store.followup import used_today
                    if used_today(self.store,cfg,self.clock()) >= self._followup.daily_limit:
                        self.store.update(job,Status.PENDING,Reason.DAILY_LIMIT)
                        continue
                resume_hold = resume_block_reason(job.platform, pcfg) if live else None
                if resume_hold:
                    # No browser, attempt timestamp or external effect for a capability hold.
                    self.store.update(job, Status.PENDING, resume_hold)
                    continue
                if live:
                    if job.platform not in locked_platforms and job.platform not in busy_platforms:
                        try:
                            locks.enter_context(platform_lock(job.platform))
                            locked_platforms.add(job.platform)
                            busy_attempts.pop(job.platform, None)
                        except PlatformBusy:
                            attempts = busy_attempts.get(job.platform, 0) + 1
                            busy_attempts[job.platform] = attempts
                            if attempts < 5:
                                self._retry.append(record)
                                self._retry_due[job.platform] = self.clock() + 3.0
                                continue
                            busy_platforms.add(job.platform)
                    if job.platform in busy_platforms:
                        self._inactive_platforms.add(job.platform)
                        self.store.update(job,Status.PENDING,Reason.PLATFORM_BUSY)
                        continue
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
                if any(((cfg.stop_on_unverified and r.status == Status.UNVERIFIED) or r.reason == Reason.RISK_CONTROL or (r.reason == Reason.LOGIN_REQUIRED and r.status != Status.PENDING))
                       and (not batch_token or r.job in self.store.batch_jobs(batch_token)) for r in rows):
                    continue
                attempted = self.store.attempt_times(job.platform)
                # Persisted interval survives restarts; defer until the next platform deadline.
                deadline = 0 if live else max(attempted, default=0) + pcfg.interval_seconds
                if self.clock() < deadline:
                    self._retry.append(record)
                    self._retry_due[job.platform] = deadline
                    continue
                if should_stop():
                    break
                from job_agent.engine.freshness import check as check_freshness
                if not check_freshness(self, job, cfg.task_space_name):
                    continue
                day = lambda ts: datetime.fromtimestamp(ts, ZoneInfo(cfg.timezone)).date()
                if sum(day(at) == day(self.clock()) for at in attempted) >= effective_limit(cfg, job.platform, self.clock()):
                    self.store.update(job, Status.PENDING, Reason.DAILY_LIMIT)
                    continue
                if live:
                    try:
                        limiter.acquire(job.platform,'delivery',delivery_policy(cfg, job.platform, self.clock()),history=attempted,block=False)
                    except RateDeferred as error:
                        if error.code=='stopped':break
                        if error.code=='cooldown':
                            self._retry.append(record)
                            self._retry_due[job.platform] = error.retry_at if error.retry_at is not None else self.clock() + 2.0
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
            self._retry_due = {}
            for platform in platforms:
                yield groups[platform].pop(0)
            pending = [r for r in self._retry + [r for rows in groups.values() for r in rows]
                       if r.job.platform not in getattr(self,'_inactive_platforms',set())]
            if pending and not should_stop():
                # Ready platforms proceed immediately. Otherwise wake at the nearest
                # deadline, checking cooperative stop at least every two seconds.
                now = self.clock()
                nearest = min(self._retry_due.get(r.job.platform, now) for r in pending)
                if nearest is not None and nearest > now:
                    self.sleep(min(2.0, nearest - now))

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
            if cfg.attachment_name and (self.settings.dry_run or spec_for(job.platform).preflight_attachment) and self.preflight.attachment_matches(job.platform, cfg.attachment_name) is not True:
                self.store.update(job, Status.FAILED, Reason.ATTACHMENT)
                return
            if not opened:
                opened = True
                adapter.open_job(job, space)
            if not self.settings.dry_run:
                from job_agent.platforms.hr import enrich_open_job
                enrich_open_job(self.store, job, adapter.page)
                if hasattr(adapter,'bind_identity'):
                    adapter.bind_identity(self.store.identity(job))
            followup = getattr(self,'_followup',None)
            if followup:
                from job_agent.store.followup import execution_allowed
                if not execution_allowed(self.store,job,self.settings,self.clock(),self._batch_token):
                    self.store.update(job,Status.SKIPPED,Reason.FOLLOWUP_INELIGIBLE)
                    return
                if hasattr(adapter,'bind_delivery_kind'):
                    adapter.bind_delivery_kind('followup')
            if not followup and self.store.duplicate_for(job, self.settings.cooldown, self.clock()):
                self.store.update(job, Status.SKIPPED, Reason.COOLDOWN)
                return
            checking_applied = True
            applied = adapter.check_applied()
            checking_applied = False
            if followup and applied is not True:
                self.store.update(job,Status.SKIPPED,Reason.FOLLOWUP_INELIGIBLE)
                return
            if applied is True and not followup:
                self.store.update(job, Status.SKIPPED, Reason.ALREADY_APPLIED)
                return
            if applied is not False and not followup:
                self.store.update(job, Status.UNVERIFIED, Reason.RECEIPT_UNKNOWN)
                return
            if followup:
                from job_agent.store.followup import execution_allowed
                if not execution_allowed(self.store,job,self.settings,self.clock(),self._batch_token):
                    self.store.update(job,Status.SKIPPED,Reason.FOLLOWUP_INELIGIBLE)
                    return
            # Commit BEFORE the first possible external effect. Crash cannot become a retry.
            self.store.update(job, Status.UNVERIFIED, Reason.INTERRUPTED, self.clock())
            effect = True
            if hasattr(adapter,'bind_resume_recorder'):
                adapter.bind_resume_recorder(lambda selection:self.store.record_resume_selection(job,selection))
            adapter.send_greeting(self.store.execution_greeting(job) if hasattr(self.store, 'execution_greeting')
                                  else prepare_greeting(job.platform, cfg.greeting, cfg.greeting_newline_separator, paragraph_separator=cfg.greeting_paragraph_separator))
            if cfg.attachment_name and adapter.choose_attachment(cfg.attachment_name) is not True:
                self.store.update(job, Status.UNVERIFIED, Reason.ATTACHMENT)
                return
            if adapter.confirm_receipt() is True:
                self.store.update(job, Status.SENT, Reason.NONE)
            elif spec_for(job.platform).supports_application_receipt and getattr(adapter,'application_receipt_at',None):
                # Keep the no-retry state. Application success is a distinct,
                # positive business receipt, not proof of greeting/attachment.
                self.store.update(job, Status.UNVERIFIED, Reason.APPLICATION_CONFIRMED,
                    error_detail=f'原站申请时间: {adapter.application_receipt_at}')
            else:
                self.store.update(job, Status.UNVERIFIED, Reason.RECEIPT_UNKNOWN)
        except Exception as exc:
            report_error(exc,platform=job.platform,stage='delivery',
                         alert=not self.store.dry_run and getattr(exc,'code','') not in {'risk_control','unexpected_redirect','login_required'})
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
                elif spec_for(job.platform).supports_application_receipt and getattr(adapter,'application_receipt_at',None):
                    self.store.update(job,Status.UNVERIFIED,Reason.APPLICATION_CONFIRMED,
                        error_detail=f'原站申请时间: {adapter.application_receipt_at}; 完整回执未确认')
                else:
                    self.store.update(job, Status.UNVERIFIED, Reason.EFFECT_ERROR, error_detail=str(exc)[:2000])
            elif getattr(exc, 'code', '') == 'job_unavailable':
                self.store.update(job, Status.SKIPPED, Reason.JOB_UNAVAILABLE, error_detail=str(exc)[:2000])
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
                    # Preserve this receipt and stop only this platform. Remaining
                    # jobs have had no effect and must not strand a confirmed batch.
                    self._inactive_platforms.add(job.platform)
                    for record in self.store.records():
                        if (record.job in self._run_jobs and record.job.platform == job.platform
                                and record.status == Status.PENDING):
                            self.store.update(record.job, Status.FAILED, Reason.PRECHECK_ERROR,
                                              error_detail='Temporary tab cleanup failed; platform stopped')
                    log.warning('Temporary tab cleanup failed platform=%s; platform stopped', job.platform)
