"""Read-only polling coordinator. Sources own temporary pages; Browser owns space."""
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

    def _failure(self, platform, kind):
        state = self.state.setdefault(platform, {'count': 0, 'kind': kind, 'notified': []})
        if state['kind'] != kind:
            state = self.state[platform] = {'count': 0, 'kind': kind, 'notified': []}
        state['count'] += 1
        self._save()
        if kind == 'fetch' and state['count'] < self.settings.failure_threshold:
            return
        if not self.config.notify.enabled:
            return
        labels = {'fetch': '连续抓取失败', 'login': '登录失效，请登录后重启监听',
                  'risk': '原站出现风险限制，已暂停该平台，请本人在官方端确认状态',
                  'space': '监听空间不可用，已停止，请检查后重启'}
        for name, channel in self.config.notify.channels.items():
            if not channel.enabled or channel.type != 'telegram' or name in state['notified']:
                continue
            outcome = notify.send_one(channel, 'job-agent 监听告警', f'{platform}：{labels[kind]}')
            if outcome['success']:
                state['notified'].append(name)
                self._save()

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
                    self._failure('space', 'space')
                    return {**result, 'failed': 1, 'stopped': True}
                self.state.pop('space', None)
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
                    continue
                if source.platform in self.paused:
                    # Retry alert delivery without touching logged-out platform.
                    self._failure(source.platform, 'login')
                    row['status'] = 'login_paused'
                    logging.getLogger(__name__).info('platform=%s fetched=0 added=0 status=login_paused', source.platform)
                    continue
                try:
                    if self.safety is not None:
                        self.safety.require(source.platform)
                    if self.limiter is not None:
                        policy=self.config.engine.platforms[source.platform].rate_policy()
                        policy.daily_limit=None  # The delivery quota must not silence read-only reply monitoring.
                        self.limiter.acquire(source.platform,'monitor',policy)
                    cursor = ConversationCursor(self.store, source.platform, self.config.engine.monitor.backfill_hours)
                    events = source.fetch(space, since=cursor.since, cursor=cursor)
                    row.update(cursor.stats())
                    row['fetched'] = len(events)
                    if not events:
                        row['detail'] = '无数据'
                    row['added'] = ingest(self.store, source.platform, events, cursor=cursor)
                    result['added'] += row['added']
                    from service.reply_classification import classify_pending
                    row['classification'] = classify_pending(self.store,self.config,model_limit=model_budget,platform=source.platform)
                    model_budget-=row['classification']['model_calls']
                except SpaceUnavailable:
                    row['status'] = 'space_unavailable'
                    self.stopped = True
                    self._failure('space', 'space')
                    result['failed'] += 1
                    break
                except Exception as error:
                    code = getattr(error, 'code', '')
                    if code in {'risk_control','unexpected_redirect'} and self.safety is not None:
                        self.safety.repo.pause(source.platform,code)
                        try:self.safety.alert(source.platform,'risk_control')
                        except Exception:pass
                    if code in {'risk_control','unexpected_redirect'}:
                        if source.platform not in self.store.paused_platforms(): self.store.pause_platform(source.platform)
                        shared_pauses.update(self.store.paused_platforms())
                    kind = 'risk' if code in {'risk_control','unexpected_redirect'} else 'login' if code in {'not_logged_in', 'login_required', 'login_expired', 'auth_required'} else 'fetch'
                    row['status'] = 'risk_control_paused' if code in {'risk_control','unexpected_redirect'} else code or type(error).__name__
                    if kind == 'login':
                        self.paused.add(source.platform)
                    from engine.rate import RateDeferred
                    if not isinstance(error,RateDeferred):
                        self._failure(source.platform, kind)
                    result['failed'] += 1
                else:
                    self.state.pop(source.platform, None)
                    self._save()
                finally:
                    logging.getLogger(__name__).info('platform=%s fetched=%s added=%s status=%s', source.platform, row['fetched'], row['added'], row['status'])
            notifications = poll_replies(self.store, self.config,wait_for_classification=True)
            for key in ('sent', 'failed', 'skipped'):
                result[key] += notifications[key]
            result['suppressed'] = notifications.get('suppressed', 0)
            return result
        finally:
            os.close(fd)
