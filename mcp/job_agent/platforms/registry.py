"""Concrete platform factories. No browser actions at construction time."""
from job_agent.platforms.boss.apply import BossAdapter
from job_agent.platforms.zhilian.apply import ZhilianAdapter
from job_agent.platforms.liepin.apply import LiepinAdapter
from job_agent.platforms.job51.apply import Job51Adapter
from job_agent.platforms.nowcoder.apply import NowcoderAdapter
from job_agent.platforms.maimai.apply import MaimaiAdapter
from job_agent.platforms.boss.search import BossJobSource
from job_agent.platforms.zhilian.search import ZhilianJobSource

ADAPTERS = {'boss': BossAdapter, 'zhilian': ZhilianAdapter,
            'liepin': LiepinAdapter, '51job': Job51Adapter, 'nowcoder': NowcoderAdapter, 'maimai': MaimaiAdapter}

def create_adapters(browser, settings):
    return {name: cls(browser, settings.platforms[name]) for name, cls in ADAPTERS.items()}

from job_agent.platforms.liepin.search import LiepinJobSource
from job_agent.platforms.job51.search import Job51JobSource

from job_agent.platforms.nowcoder.search import NowcoderJobSource
from job_agent.platforms.maimai.search import MaimaiJobSource

SOURCES = {'boss': BossJobSource, 'zhilian': ZhilianJobSource,
           'liepin': LiepinJobSource, '51job': Job51JobSource, 'nowcoder': NowcoderJobSource, 'maimai': MaimaiJobSource}


def create_sources():
    return {name: cls() for name, cls in SOURCES.items()}
