"""CLI composition; only this module selects live versus Fake dependencies."""
import importlib
import logging
import signal
from config import job_agent_home, load_config
from store import Store
from store.collection import CollectionRepository
from .runner import Collector
from .config import PLATFORMS

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
    from platforms import registry
    factory = getattr(registry, 'create_sources', None)
    if factory is not None:
        sources = factory()
        if isinstance(sources, dict):
            return [sources.get(p, MissingSource(p)) for p in platforms]
        by_platform = {s.platform: s for s in sources}
        return [by_platform.get(p, MissingSource(p)) for p in platforms]
    sources = []
    for platform in platforms:
        directory = 'job51' if platform == '51job' else platform
        try:
            module = importlib.import_module(f'platforms.{directory}.search')
            source = module.JobSource()
            if source.platform != platform:
                raise ValueError('Source platform mismatch')
        except (ImportError, AttributeError):
            source = MissingSource(platform)
        sources.append(source)
    return sources


def telegram_alert(config, platform, code):
    from service.safety import telegram_alert as send
    return send(config,platform,code)


def collect_once(platforms=None, *, dry_run=False, page_limit=None):
    if isinstance(platforms, str):
        platforms = [platforms]
    config = load_config()
    settings = config.collect.model_copy(deep=True)
    selected = select_platforms(settings, platforms)
    if dry_run:
        from browser.base import FakeBrowser
        from .fake import FakeSource
        browser = FakeBrowser()
        # Explicitly selected dry-run platforms may ignore live enable switches.
        selected = list(dict.fromkeys(platforms or PLATFORMS))
        settings = settings.model_copy(deep=True)
        from .config import PlatformSettings
        for p in selected:
            if p not in PLATFORMS:
                raise ValueError('Unknown collection platform')
            settings.platforms.setdefault(p, PlatformSettings()).enabled = True
        sources = [FakeSource(p) for p in selected]
    else:
        if not selected:
            return {'platforms': {}, 'failed': 0, 'disabled': True}
        from browser.ego import EgoBrowser
        from browser.notifications import space_alert
        browser = EgoBrowser(keepalive_seconds=config.browser.keepalive_seconds,
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
                from service.safety import PlatformSafety
                from engine.rate import RateLimiter
                from store.automation import AutomationRepository
                kwargs.update(safety=PlatformSafety(browser,store,config),limiter=RateLimiter(AutomationRepository(store)))
            runner = Collector(browser, sources, repo, settings,
                               alert=(lambda *a: None) if dry_run else lambda p, c: telegram_alert(config, p, c), **kwargs)
            return {**runner.run(), 'dry_run': dry_run, 'database': str(path)}
    finally:
        # Ego shutdown closes only instance-owned temporary pages, never finish.
        browser.shutdown(settings.task_space_name)


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
        log.error('collection failed error_type=%s', type(error).__name__)
        return 1
    finally:
        signal.signal(signal.SIGTERM, previous)
