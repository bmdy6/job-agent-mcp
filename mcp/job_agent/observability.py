"""Run correlation and bounded diagnostics. No schema or provider payload tracing."""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import fcntl
import json
import logging
import os
from pathlib import Path
import re
import traceback
import time
import uuid

_RUN = ContextVar('job_agent_run_id', default=None)
_STAGE = ContextVar('job_agent_stage', default='startup')
_PLATFORM = ContextVar('job_agent_platform', default='all')
log = logging.getLogger(__name__)


def current_run_id():
    return _RUN.get()


@contextmanager
def run_scope(stage, *, run_id=None, fresh=False, platform=None):
    ident = _RUN.get() if not fresh else None
    ident = ident or run_id or uuid.uuid4().hex
    rt = _RUN.set(ident)
    st = _STAGE.set(stage)
    pt = _PLATFORM.set(platform or _PLATFORM.get())
    try:
        yield ident
    finally:
        _PLATFORM.reset(pt)
        _STAGE.reset(st)
        _RUN.reset(rt)


def install_log_context():
    factory = logging.getLogRecordFactory()
    if getattr(factory, '_job_agent_context', False):
        return
    def records(*args, **kwargs):
        record = factory(*args, **kwargs)
        record.run_id = current_run_id() or '-'
        record.stage = _STAGE.get()
        record.platform = _PLATFORM.get()
        return record
    records._job_agent_context = True
    logging.setLogRecordFactory(records)


# Exception text is untrusted: YAML validation, page data and provider replies can
# contain names, message bodies and secrets without labels. Preserve only known
# infrastructure diagnostics; everything else needs the safe code and source frame.
_SAFE_MESSAGES = (
    'Daily round already running', 'Delivery queue already running',
    'Collection adapter unavailable', 'Reply adapter unavailable',
    'Scoring credential unavailable', 'Scoring worker launch failed',
    'Invalid or incomplete model scoring result', 'Provider response too large',
    'Daily delivery requires explicit live opt-in', 'Collection, delivery and monitor spaces must be separate',
    'Acceptance cap must be 1..3 jobs/platform', 'configuration unavailable',
    'database is locked', 'database disk image is malformed', 'no such table',
    'unable to open database file', 'attempt to write a readonly database',
    'Connection refused', 'connection refused', 'Connection reset',
    'timed out', 'Name or service not known', 'Temporary failure in name resolution',
)


def safe_message(error):
    raw = str(error)
    if type(error).__name__ in {'ValidationError', 'ParserError', 'ScannerError'}:
        return '配置校验失败；原始输入已隐藏，请检查配置格式'
    for message in _SAFE_MESSAGES:
        if message in raw:
            return message[:300]
    if re.fullmatch(r'(HTTP_\d{3}|[a-z][a-z0-9_]{0,63})', raw):
        # Only explicitly structured exception codes are safe to expose.
        code = getattr(error, 'code', None)
        if code == raw or raw.startswith('HTTP_'):
            return raw
    if isinstance(error, OSError) and error.errno:
        import errno
        return errno.errorcode.get(error.errno, 'OS_ERROR')
    return '异常内容可能含私人数据，已隐藏；请按原因码和行号定位'


def safe_token(value, fallback):
    return value if isinstance(value, str) and re.fullmatch(r'[a-zA-Z0-9_:.,-]{1,100}', value) else fallback


def error_details(error, *, platform=None, stage=None):
    frames = traceback.extract_tb(error.__traceback__)[-3:]
    return dict(error_type=type(error).__name__, error_message=safe_message(error),
                platform=safe_token(getattr(error,'platform',None) or platform or _PLATFORM.get(), 'all'),
                stage=safe_token(getattr(error,'stage',None) or stage or _STAGE.get(), 'startup'),
                reason_code=safe_token(getattr(error,'reason_code',getattr(error,'code',None)), type(error).__name__),
                frames=[f'{Path(f.filename).name}:{f.lineno}' for f in frames],
                run_id=current_run_id())


NOTIFIED_LOCK_TIMEOUT_SECONDS = 5


@contextmanager
def _notification_lock():
    from job_agent.config import job_agent_home
    home = job_agent_home()
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(home/'notified.lock', os.O_CREAT|os.O_WRONLY, 0o600)
    deadline = time.monotonic() + NOTIFIED_LOCK_TIMEOUT_SECONDS
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX|fcntl.LOCK_NB)
                break
            except BlockingIOError:
                remaining = deadline-time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('notification receipt lock busy') from None
                time.sleep(min(.05, remaining))
        yield home
    finally:
        os.close(fd)


def recover_alerts(config, platform):
    """Reset alert receipts only; never release a durable platform risk pause."""
    if not config.notify.enabled:
        return
    from job_agent.store import Store
    try:
        with _notification_lock(), Store() as store, store.transaction():
            store.reset_alert_receipts(platform)
    except Exception:
        log.warning('alert recovery unavailable platform=%s', safe_token(platform,'all'))


def notify_once(config, key, title, text, *, telegram_only=False, platform='all'):
    """Best-effort alert with per-channel cooldown and the existing durable outbox."""
    from job_agent.notify import send_one
    if not config.notify.enabled:
        return True
    channels = {n:c for n,c in config.notify.channels.items()
                if c.enabled and (not telegram_only or c.type=='telegram')}
    if not channels:
        return True
    try:
        from job_agent.store import Store
        with _notification_lock(), Store() as store:
            receipts = store.alert_receipts()
            success = True
            for name,channel in channels.items():
                previous = receipts.get((key,name), {})
                sent_at = previous.get('alert_at')
                if sent_at is not None and time.time()-sent_at < config.observability.heartbeat_cooldown_minutes*60:
                    continue
                outcome = send_one(channel,title,text,keep_failed=True)
                if outcome['success']:
                    store.save_alert_receipt(key, name, dict(event=key,job=key,channel=name,
                        alert_at=time.time(),platform=platform))
                else:
                    success = False
                    log.warning('alert deferred channel=%s error_code=%s',name,
                                safe_token(outcome.get('error_code'),'unavailable'))
            return success
    except Exception:
        # Receipt contention/storage must never change the business result. Queue
        # without sending so the receipt lock owner remains the only active sender.
        log.warning('alert receipts unavailable; deferring notification')
        for channel in channels.values():
            try:
                send_one(channel,title,text,queue_only=True,keep_failed=True)
            except Exception:
                log.warning('alert outbox unavailable')
        return False


def _alert(details, config):
    from job_agent.monitor.receipts import digest
    origin = details['frames'][-1:] if details['error_message'].startswith('异常内容') else []
    key = 'error:'+digest(*(details[k] for k in ('platform','stage','error_type','reason_code','error_message')),origin)
    text = (f"{details['platform']} 平台在 {details['stage']} 阶段失败："
            f"{details['error_type']}，{details['error_message']}；建议按运行编号检查该阶段日志和连接配置。")
    return notify_once(config,key,'job-agent 运行告警',text,telegram_only=True,
                       platform=details['platform'])


# Incomplete-but-healthy outcomes: the round stops early and resumes later.
# They are logged as warnings and never alert (collection keeps saved jobs, #67).
NON_ALERT_CODES = frozenset({'collection_timeout', 'daily_limit'})


def report_error(error, *, platform=None, stage=None, config=None, alert=True):
    previous = getattr(error, '_job_agent_report', None)
    if previous and previous.get('run_id') == current_run_id():
        return previous
    details = error_details(error, platform=platform, stage=stage)
    try:
        error._job_agent_report = details
    except (AttributeError, TypeError):
        pass
    benign = details['reason_code'] in NON_ALERT_CODES
    with run_scope(details['stage'],platform=details['platform']):
        (log.warning if benign else log.error)('run %s %s', 'incomplete' if benign else 'failed',
                                               json.dumps(details,ensure_ascii=False))
    if alert and not benign:
        try:
            if config is None:
                from job_agent.config import load_config
                config = load_config()
            _alert(details,config)
        except Exception:
            # Observability must preserve the original exception and effects boundary.
            log.warning('error alert unavailable run_id=%s', current_run_id() or '-')
    return details


def observed(stage, *, fresh=False, id_argument=None, platform_argument=None):
    """Nested work inherits daily ID; independently invoked work starts a new ID."""
    def decorate(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            ident = None
            if id_argument:
                import inspect
                ident = inspect.signature(fn).bind(*args,**kwargs).arguments.get(id_argument)
            platform = kwargs.get(platform_argument) if platform_argument else None
            with run_scope(stage,run_id=ident,fresh=fresh,platform=platform):
                log.info('run started stage=%s run_id=%s',stage,current_run_id())
                try:
                    result = fn(*args,**kwargs)
                except Exception as error:
                    dry_run=kwargs.get('dry_run',False)
                    if stage == 'delivery' and args:
                        store=getattr(args[0],'store',args[0])
                        dry_run=bool(getattr(store,'dry_run',False))
                    report_error(error,alert=not dry_run)
                    if stage == 'cli':
                        return 1
                    raise
                if stage == 'monitor' and isinstance(result,dict):
                    result['run_id']=current_run_id()
                log.info('run finished stage=%s run_id=%s',stage,current_run_id())
                return result
        return wrapped
    return decorate


def audit_detail(value):
    """Preserve existing JSON keys (including resume_selection) and plain evidence."""
    if not current_run_id():
        return value
    try:
        data = json.loads(value)
    except (ValueError,TypeError):
        data = None
    if not isinstance(data,dict):
        data = {'detail':value} if value else {}
    data['run_id'] = current_run_id()
    return json.dumps(data,ensure_ascii=False,sort_keys=True)


install_log_context()
