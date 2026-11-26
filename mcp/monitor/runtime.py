"""CLI wiring. Lazy imports allow development against FakeBrowser/ReplySource."""
import fcntl
import os
import signal
import importlib
import json
import logging
import time
from store import Store
from config import job_agent_home
from service.tasks import read, write
from .config import load_settings
from .poller import Monitor

log = logging.getLogger(__name__)


def create_browser(config):
    from browser.ego import EgoBrowser
    from browser.notifications import space_alert
    def on_event(name, event):
        # Poller owns the persisted unavailable alert. Driver handles recovery/heartbeat.
        if event != 'unavailable':
            space_alert(config.notify, name, event)
    return EgoBrowser(keepalive_seconds=config.browser.keepalive_seconds, on_event=on_event,
                      human_input=config.browser.human_input)


class UnavailableSource:
    def __init__(self, platform):
        self.platform = platform
    def fetch(self, space, *, since=None, cursor=None):
        raise RuntimeError('Reply adapter unavailable')


def create_sources(browser, settings):
    """Integration convention: replies.ReplySource(browser), exposing fetch(space).

    A missing adapter is a platform failure, not an excuse to skip it silently.
    Concrete factory names can be connected here when G/H land.
    """
    sources = []
    for platform in settings.platforms:
        directory = 'job51' if platform == '51job' else platform
        try:
            module = importlib.import_module(f'platforms.{directory}.replies')
            source = module.ReplySource()
            if source.platform != platform:
                raise ValueError('Adapter platform mismatch')
        except Exception:
            source = UnavailableSource(platform)
        sources.append(source)
    return sources


def run(*, once=False, should_stop=lambda: False):
    browser = None
    settings = None
    lock = None
    previous_term = None
    runtime = None
    runtime_path = None
    safety = None
    try:
        config, settings = load_settings()
        home = job_agent_home()
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock = os.open(home/'monitor-run.lock', os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        def terminate(signum, frame):
            raise KeyboardInterrupt
        previous_term = signal.signal(signal.SIGTERM, terminate)
        runtime_path = home / 'monitor-runtime.json'
        runtime = {**(read(runtime_path) or {}), 'pid': os.getpid(), 'state': 'running', 'started_at': time.time()}
        from service.deployment import status as deployment_status
        runtime['deployment'] = deployment_status()
        runtime.pop('stopped_at', None)
        write(runtime_path, runtime)
        browser = create_browser(config)
        with Store() as store:
            from service.safety import PlatformSafety
            from store.automation import AutomationRepository
            from engine.rate import RateLimiter
            # Poller owns detailed alerts, including session-preflight failures.
            safety=PlatformSafety(browser,store,config,space_name=settings.task_space_name, alert=lambda *args: None)
            monitor = Monitor(browser, create_sources(browser, settings), store, config, settings,
                              safety=safety,limiter=RateLimiter(safety.repo))
            while not should_stop():
                result = monitor.poll_once()
                runtime.update(last_poll_at=time.time(), result=result, platforms=getattr(monitor, 'state', runtime.get('platforms', {})))
                write(runtime_path, runtime)
                if monitor.stopped:
                    runtime['state'] = 'space_unavailable'
                    # Clean exit prevents failure-only KeepAlive from reclaiming space.
                    return 0
                if once:
                    print(json.dumps(result))
                    return int(bool(result['failed']))
                if result.get('busy'):
                    return 0
                log.info('monitor completed added=%s sent=%s failed=%s suppressed=%s', result['added'], result['sent'], result['failed'], result.get('suppressed', 0))
                deadline = time.monotonic() + settings.interval_minutes * 60
                while time.monotonic() < deadline and not should_stop():
                    time.sleep(min(0.25, max(0, deadline-time.monotonic())))
            return 0
    except KeyboardInterrupt:
        return 0
    except Exception as error:
        if runtime is not None:
            runtime['state'] = 'failed'
        log.error('monitor failed error_type=%s', type(error).__name__)
        return 1
    finally:
        if browser is not None:
            try:
                try:
                    browser.shutdown(settings.task_space_name)
                finally:
                    if safety is not None and safety.checked and safety.space_name != settings.task_space_name:
                        browser.shutdown(safety.space_name)
            except Exception as error:
                log.error('monitor shutdown failed error_type=%s', type(error).__name__)

        if runtime is not None:
            runtime.update(stopped_at=time.time())
            if runtime['state'] == 'running':
                runtime['state'] = 'stopped'
            write(runtime_path, runtime)
        if previous_term is not None:
            signal.signal(signal.SIGTERM, previous_term)
        if lock is not None:
            os.close(lock)
