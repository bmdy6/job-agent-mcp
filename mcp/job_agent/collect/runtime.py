"""CLI composition; only this module selects live versus Fake dependencies."""
from job_agent.observability import observed, report_error
import importlib
import logging
import signal
from job_agent.config import job_agent_home, load_config
from job_agent.store import Store
from job_agent.store.collection import CollectionRepository
from job_agent.collect.runner import Collector
from job_agent.config.collect import PLATFORMS

log = logging.getLogger(__name__)


def select_platforms(settings, platforms=None):
    if isinstance(platforms, str):
        platforms = [platforms]
    selected = list(dict.fromkeys(platforms)) if platforms is not None else list(settings.platforms)
    if any(p not in PLATFORMS for p in selected):
        raise ValueError('Unknown collection platform')
    return [p for p in selected if p in settings.platforms and settings.platforms[p].enabled]


class MissingSource:
    def __init__(self, platform):
        self.platform = platform

    def search(self, space, query):
        raise RuntimeError('Collection adapter unavailable')


def create_sources(browser, settings, platforms):
    """Use the registry factory when available; adapters remain owned by L/M.

    L/M register create_sources() and search.JobSource aliases. Sources receive
    their Browser Space on every call; construction never opens a page.
    """
    from job_agent.platforms import registry
    factory = getattr(registry, 'create_sources', None)
    if factory is not None:
        sources = factory()
        if isinstance(sources, dict):
            return [sources.get(p, MissingSource(p)) for p in platforms]
        by_platform = {s.platform: s for s in sources}
        return [by_platform.get(p, MissingSource(p)) for p in platforms]
    from job_agent.platforms.spec import SPECS
    sources = []
    for platform in platforms:
        directory = SPECS[platform].module_dir if platform in SPECS else platform
        try:
            module = importlib.import_module(f'job_agent.platforms.{directory}.search')
            source = module.JobSource()
            if source.platform != platform:
                raise ValueError('Source platform mismatch')
        except (ImportError, AttributeError):
            source = MissingSource(platform)
        sources.append(source)
    return sources


def telegram_alert(config, platform, code):
    from job_agent.service.safety import telegram_alert as send
    return send(config,platform,code)


@observed('collection')
def collect_once(platforms=None, *, dry_run=False, page_limit=None):
    if isinstance(platforms, str):
        platforms = [platforms]
    from job_agent.service.automation import hold
    if not dry_run and (deferred := hold('collect')):
        return {**deferred, 'platforms': {}, 'failed': 0, 'stopped': True}
    config = load_config()
    settings = config.collect.model_copy(deep=True)
    selected = select_platforms(settings, platforms)
    if dry_run:
        from job_agent.browser.base import FakeBrowser
        from job_agent.collect.fake import FakeSource
        browser = FakeBrowser()
        # Explicitly selected dry-run platforms may ignore live enable switches.
        selected = list(dict.fromkeys(platforms or PLATFORMS))
        settings = settings.model_copy(deep=True)
        from job_agent.config.collect import PlatformSettings
        for p in selected:
            if p not in PLATFORMS:
                raise ValueError('Unknown collection platform')
            settings.platforms.setdefault(p, PlatformSettings()).enabled = True
        sources = [FakeSource(p) for p in selected]
    else:
        if not selected:
            return {'platforms': {}, 'failed': 0, 'disabled': True}
        from job_agent.browser.ego import EgoBrowser
        from job_agent.browser.notifications import space_alert
        browser = EgoBrowser(startup_timeout_s=config.browser.startup_timeout_seconds, keepalive_seconds=config.browser.keepalive_seconds,
                             on_event=lambda name, event: space_alert(config.notify, name, event),
                             human_input=config.browser.human_input)
        sources = create_sources(browser, settings, selected)
        for source in sources:
            source.city_codes = settings.platforms[source.platform].city_codes
    home = job_agent_home()
    path = home / ('collect-dry-run.sqlite3' if dry_run else 'jobs.sqlite3')
    try:
        with Store(path, dry_run=dry_run) as store:
            repo = CollectionRepository(store, timezone=settings.timezone)
            if page_limit is not None:
                for options in settings.platforms.values():
                    options.daily_page_limit = min(options.daily_page_limit,page_limit)
            kwargs = {}
            if dry_run:
                import time
                virtual_now=[time.time()]
                def advance(seconds): virtual_now[0]+=seconds
                kwargs={'clock':lambda:virtual_now[0],'sleep':advance}
            if not dry_run:
                from job_agent.service.safety import PlatformSafety
                from job_agent.engine.rate import RateLimiter
                from job_agent.store.automation import AutomationRepository
                kwargs.update(safety=PlatformSafety(browser,store,config),limiter=RateLimiter(AutomationRepository(store)))
            runner = Collector(browser, sources, repo, settings,
                               alert=(lambda *a: None) if dry_run else lambda p, c: telegram_alert(config, p, c), **kwargs)
            return {**runner.run(), 'dry_run': dry_run, 'database': str(path)}
    finally:
        # Ego shutdown closes only instance-owned temporary pages, never finish.
        browser.shutdown(settings.task_space_name)


@observed('collection')
def run(platforms=None, *, dry_run=False):
    import json
    def terminate(*_):
        raise KeyboardInterrupt
    previous = signal.signal(signal.SIGTERM, terminate)
    try:
        result = collect_once(platforms, dry_run=dry_run)
        print(json.dumps(result, ensure_ascii=False))
        return int(bool(result.get('failed')))
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        report_error(error,stage='collection',alert=not dry_run)
        return 1
    finally:
        signal.signal(signal.SIGTERM, previous)
