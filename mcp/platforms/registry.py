"""Concrete platform factories. No browser actions at construction time."""
from .boss.apply import BossAdapter
from .zhilian.apply import ZhilianAdapter
from .liepin.apply import LiepinAdapter
from .job51.apply import Job51Adapter
from .nowcoder.apply import NowcoderAdapter
from .maimai.apply import MaimaiAdapter
from .boss.search import BossJobSource
from .zhilian.search import ZhilianJobSource

ADAPTERS = {'boss': BossAdapter, 'zhilian': ZhilianAdapter,
            'liepin': LiepinAdapter, '51job': Job51Adapter, 'nowcoder': NowcoderAdapter, 'maimai': MaimaiAdapter}

def create_adapters(browser, settings):
    return {name: cls(browser, settings.platforms[name]) for name, cls in ADAPTERS.items()}

from .liepin.search import LiepinJobSource
from .job51.search import Job51JobSource

from .nowcoder.search import NowcoderJobSource
from .maimai.search import MaimaiJobSource

SOURCES = {'boss': BossJobSource, 'zhilian': ZhilianJobSource,
           'liepin': LiepinJobSource, '51job': Job51JobSource, 'nowcoder': NowcoderJobSource, 'maimai': MaimaiJobSource}


def create_sources():
    return {name: cls() for name, cls in SOURCES.items()}
