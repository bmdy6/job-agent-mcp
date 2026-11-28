"""CLI wiring. Lazy imports allow development against FakeBrowser/ReplySource."""
from job_agent.observability import observed, report_error
import fcntl
import os
import signal
import importlib
import json
import logging
import time
from job_agent.store import Store
from job_agent.config import job_agent_home
from job_agent.monitor.config import load_settings
from job_agent.monitor.poller import Monitor

log = logging.getLogger(__name__)


def create_browser(config):
    from job_agent.browser.ego import EgoBrowser
    from job_agent.browser.notifications import space_alert
    def on_event(name, event):
        # Poller owns the persisted unavailable alert. Driver handles recovery/heartbeat.
        if event != 'unavailable':
            space_alert(config.notify, name, event)
    return EgoBrowser(startup_timeout_s=config.browser.startup_timeout_seconds, keepalive_seconds=config.browser.keepalive_seconds, on_event=on_event,
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
    from job_agent.platforms.spec import SPECS
    sources = []
    for platform in settings.platforms:
        directory = SPECS[platform].module_dir if platform in SPECS else platform
        try:
            module = importlib.import_module(f'job_agent.platforms.{directory}.replies')
            source = module.ReplySource()
            if source.platform != platform:
                raise ValueError('Adapter platform mismatch')
        except Exception:
            source = UnavailableSource(platform)
        sources.append(source)
    return sources


@observed('monitor_startup')
def run(*, once=False, should_stop=lambda: False):
    browser = None
    settings = None
    lock = None
    previous_term = None
    runtime = None
    store = None
    safety = None
    try:
        from job_agent.service.automation import allowed
        if once and not allowed('monitor'):
            return 0
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
        store = Store()
        runtime = {**(store.latest_run('monitor') or {}), 'pid': os.getpid(), 'state': 'running', 'started_at': time.time()}
        from job_agent.service.deployment import status as deployment_status
        runtime['deployment'] = deployment_status()
        runtime.setdefault('first_started_at',runtime['started_at'])
        runtime.pop('stopped_at', None)
        store.save_run('monitor', runtime)
        while not should_stop():
            if not allowed('monitor'):
                if once:
                    return 0
                if runtime['state'] != 'paused':
                    runtime['state'] = 'paused'
                    store.save_run('monitor', runtime)
                time.sleep(.25)
                continue
            runtime['state'] = 'running'
            if browser is None:
                browser = create_browser(config)
                from job_agent.service.safety import PlatformSafety
                from job_agent.store.automation import AutomationRepository
                from job_agent.engine.rate import RateLimiter
                # Poller owns detailed alerts, including session-preflight failures.
                safety=PlatformSafety(browser,store,config,space_name=settings.task_space_name, alert=lambda *args: None)
                monitor = Monitor(browser, create_sources(browser, settings), store, config, settings,
                                  safety=safety,limiter=RateLimiter(safety.repo))
            result = monitor.poll_once(scheduled=not once)
            if any(row.get('status') == 'ok' for row in result.get('platforms', {}).values()):
                runtime['last_success_at']=time.time()
            runtime.update(last_poll_at=time.time(), result=result, run_id=result.get('run_id'), platforms=getattr(monitor, 'state', runtime.get('platforms', {})))
            store.save_run('monitor', runtime)
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
            # Check platform-specific windows and cadence every minute.
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline and not should_stop():
                time.sleep(min(0.25, max(0, deadline-time.monotonic())))
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as error:
        if runtime is not None:
            runtime['state'] = 'failed'
            runtime['reason_code'] = getattr(error,'code',type(error).__name__)
            runtime['current_step'] = 'poll'
        report_error(error,stage='monitor_startup')
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
                report_error(error,stage='shutdown')

        try:
            if runtime is not None:
                runtime.update(stopped_at=time.time())
                if runtime['state'] == 'running':
                    runtime['state'] = 'stopped'
                store.save_run('monitor', runtime)
        finally:
            if store is not None:
                store.close()
            if previous_term is not None:
                signal.signal(signal.SIGTERM, previous_term)
            if lock is not None:
                os.close(lock)
