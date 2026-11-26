"""Read-only polling coordinator. Sources own temporary pages; Browser owns space."""
from browser.platform_lock import platform_lock, PlatformBusy
import fcntl
import json
import os
import time
import logging
from browser.base import Browser, SpaceUnavailable
from platforms.base import ReplySource
from config import job_agent_home
import notify
from .config import MonitorSettings
from .repository import ingest, sent_jobs
from .notifications import poll_replies
from .delta import ConversationCursor
from .diagnostics import failure, alert_body

class Monitor:
    def __init__(self, browser: Browser, sources: list[ReplySource], store, config, settings=None, *, safety=None,limiter=None):
        self.browser, self.sources, self.store, self.config = browser, sources, store, config
        self.settings = settings or MonitorSettings()
        self.safety,self.limiter=safety,limiter
        self.paused = set()
        self.stopped = False
        self.home = job_agent_home()
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.state_path = self.home / 'monitor-state.json'

    def _save(self):
        temp = self.state_path.with_suffix('.tmp')
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(self.state, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, self.state_path)

    def _failure(self, platform, kind, *, stage='fetch', reason_code=None, increment=True):
        state = self.state.setdefault(platform, {})
        state.update(count=state.get('count', 0) + int(increment), kind=kind,
                     stage=stage, reason_code=reason_code or kind)
        if increment or not state.get('last_failure_at'):
            state['last_failure_at'] = time.time()
        key = stage + ':' + state['reason_code']
        receipts = state.setdefault('reason_notified', {})
        # Migrate old coarse receipts without repeating the same generic alert.
        if not receipts and state.get('notified') and 'stage' not in state.get('last_failure', {}):
            receipts[key] = list(state['notified'])
        state['notified'] = receipts.setdefault(key, [])
        state['last_failure'] = dict(stage=stage, reason_code=state['reason_code'], at=state['last_failure_at'])
        self._save()
        if kind == 'fetch' and state['count'] < self.settings.failure_threshold:
            return
        if not self.config.notify.enabled:
            return
        for name, channel in self.config.notify.channels.items():
            if not channel.enabled or channel.type != 'telegram' or name in state['notified']:
                continue
            outcome = notify.send_one(channel, 'job-agent 监听告警', alert_body(platform, state))
            if outcome['success']:
                state['notified'].append(name)
                self._save()

    def _success(self, platform):
        state = self.state.setdefault(platform, {})
        state.setdefault('stage', 'fetch' if state.get('count') else 'complete')
        state.setdefault('kind', 'fetch')
        state.setdefault('reason_code', state['kind'] if state.get('count') else 'ok')
        pending = state.setdefault('recovery_pending', [])
        if state.get('count', 0):
            for name in state.get('notified', []) + [n for names in state.get('reason_notified', {}).values() for n in names]:
                if name not in pending:
                    pending.append(name)
            state.update(count=0, notified=[], reason_notified={})
        state['last_success_at'] = time.time()
        self._save()
        if self.config.notify.enabled:
            for name in list(pending):
                channel = self.config.notify.channels.get(name)
                if channel and channel.enabled and channel.type == 'telegram':
                    outcome = notify.send_one(channel, 'job-agent 监听已恢复', alert_body(platform, state, recovered=True))
                    if outcome['success']:
                        pending.remove(name)
                        self._save()

    def _diagnostics(self, platform, row):
        state = self.state.get(platform, {})
        row.update(consecutive_failures=state.get('count', 0),
                   last_success_at=state.get('last_success_at'), last_failure=state.get('last_failure'))

    def poll_once(self):
        result = {'sent': 0, 'failed': 0, 'skipped': 0, 'added': 0, 'platforms': {}}
        fd = os.open(self.home/'monitor.lock', os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return {**result, 'busy': True}
            self.state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {}
            if self.stopped:
                return result
            # A persisted pause belongs to the platform, not just the collection
            # or delivery worker. Snapshot before obtaining a browser space; a
            # pause present at the start stays effective for this whole poll.
            shared_pauses = self.store.paused_platforms()
            space = None
            if not self.sources or any(s.platform not in shared_pauses and s.platform not in self.paused for s in self.sources):
                try:
                    space = self.browser.space(self.settings.task_space_name)
                except SpaceUnavailable:
                    self.stopped = True
                    self._failure('space', 'space', stage='resolve_space', reason_code='task_space')
                    return {**result, 'failed': 1, 'stopped': True}
                if self.state.get('space', {}).get('count') or self.state.get('space', {}).get('recovery_pending'):
                    self._success('space')
            model_budget=self.config.engine.monitor.classification_model_limit
            for source in self.sources:
                row = {'fetched': 0, 'added': 0, 'status': 'ok', 'at': time.time()}
                result['platforms'][source.platform] = row
                # Re-read immediately before the source so a pause created by
                # another worker during this poll is respected as well.
                shared_pauses.update(self.store.paused_platforms())
                if source.platform in shared_pauses:
                    row['status'] = 'risk_control_paused' if shared_pauses[source.platform] == 'risk_control' else 'platform_paused'
                    row['detail'] = '本地持久暂停，本轮未访问原站；不代表登录失效'
                    logging.getLogger(__name__).info('platform=%s fetched=0 added=0 status=%s', source.platform, row['status'])
                    state = self.state.get(source.platform, {})
                    if state.get('kind') == 'risk' and state.get('count'):
                        self._failure(source.platform, 'risk', stage=state.get('stage', 'fetch'),
                                      reason_code=state.get('reason_code', 'risk_control'), increment=False)
                    self._diagnostics(source.platform, row)
                    continue
                if source.platform in self.paused:
                    # Retry alert delivery without touching logged-out platform.
                    state = self.state.get(source.platform, {})
                    self._failure(source.platform, 'login', stage=state.get('stage', 'fetch'), reason_code=state.get('reason_code', 'login_required'), increment=False)
                    self._diagnostics(source.platform, row)
                    row['status'] = 'login_paused'
                    logging.getLogger(__name__).info('platform=%s fetched=0 added=0 status=login_paused', source.platform)
                    continue
                ownership = platform_lock(source.platform)
                try:
                    ownership.__enter__()
                except PlatformBusy:
                    row["status"] = "platform_busy"
                    result["skipped"] += 1
                    continue
                phase = 'session_preflight'
                try:
                    if self.safety is not None:
                        self.safety.require(source.platform)
                    phase = 'rate_limit'
                    if self.limiter is not None:
                        policy=self.config.engine.platforms[source.platform].rate_policy()
                        policy.daily_limit=None  # The delivery quota must not silence read-only reply monitoring.
                        self.limiter.acquire(source.platform,'monitor',policy)
                    phase = 'fetch'
                    cursor = ConversationCursor(self.store, source.platform, self.config.engine.monitor.backfill_hours)
                    events = source.fetch(space, since=cursor.since, cursor=cursor)
                    row.update(cursor.stats())
                    row['fetched'] = len(events)
                    if not events:
                        row['detail'] = '无数据'
                    phase = 'ingest'
                    row['added'] = ingest(self.store, source.platform, events, cursor=cursor)
                    result['added'] += row['added']
                    phase = 'classify'
                    from service.reply_classification import classify_pending
                    row['classification'] = classify_pending(self.store,self.config,model_limit=model_budget,platform=source.platform)
                    model_budget-=row['classification']['model_calls']
                except SpaceUnavailable as error:
                    row.update(failure(error))
                    row['status'] = 'space_unavailable'
                    self.stopped = True
                    self._failure('space', 'space', **failure(error))
                    result['failed'] += 1
                    break
                except Exception as error:
                    code = getattr(error, 'code', '')
                    if code in {'risk_control','unexpected_redirect'} and self.safety is not None:
                        self.safety.repo.pause(source.platform,code)
                        # Monitor owns the detailed, persisted per-reason alert.
                    if code in {'risk_control','unexpected_redirect'}:
                        if source.platform not in self.store.paused_platforms(): self.store.pause_platform(source.platform)
                        shared_pauses.update(self.store.paused_platforms())
                    kind = 'risk' if code in {'risk_control','unexpected_redirect'} else 'login' if code in {'not_logged_in', 'login_required', 'login_expired', 'auth_required'} else 'fetch'
                    row['status'] = 'risk_control_paused' if code in {'risk_control','unexpected_redirect'} else code or type(error).__name__
                    if kind == 'login':
                        self.paused.add(source.platform)
                    from engine.rate import RateDeferred
                    if not isinstance(error,RateDeferred) or phase == 'session_preflight':
                        detail = failure(error, stage=phase)
                        row.update(detail)
                        self._failure(source.platform, kind, **detail)
                    result['failed'] += 1
                else:
                    self._success(source.platform)
                finally:
                    ownership.__exit__(None,None,None)
                    self._diagnostics(source.platform, row)
                    logging.getLogger(__name__).info('platform=%s fetched=%s added=%s status=%s stage=%s reason_code=%s', source.platform, row['fetched'], row['added'], row['status'], row.get('stage', 'complete'), row.get('reason_code', 'ok'))
            notifications = poll_replies(self.store, self.config,wait_for_classification=True)
            for key in ('sent', 'failed', 'skipped'):
                result[key] += notifications[key]
            result['suppressed'] = notifications.get('suppressed', 0)
            return result
        finally:
            os.close(fd)
