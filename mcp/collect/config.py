"""Collection settings, independent of delivery and BossHunter."""
from engine.rate import RatePolicy
from typing import Literal
from zoneinfo import ZoneInfo
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PLATFORMS = ('boss', 'zhilian', 'liepin', '51job')
Platform = Literal['boss', 'zhilian', 'liepin', '51job']


class Model(BaseModel):
    model_config = ConfigDict(extra='forbid')


class PlatformSettings(Model):
    rate: RatePolicy = Field(default_factory=RatePolicy)
    enabled: bool = False
    daily_page_limit: int = Field(default=20, ge=0, strict=True)
    city_codes: dict[str, str] = Field(default_factory=dict)


class Filters(Model):
    salary_min_k: int = Field(default=8, ge=0, strict=True)
    title_exclude: list[str] = Field(default_factory=list)
    company_exclude: list[str] = Field(default_factory=list)
    blocked_companies: list[str] = Field(default_factory=list)


class CollectSettings(Model):
    keywords: list[str] = Field(default_factory=lambda: ['AI应用开发工程师', 'Agent开发工程师', '大模型应用开发工程师'])
    cities: list[str] = Field(default_factory=lambda: ['苏州', '上海', '杭州', '南京', '无锡', '合肥'])
    task_space_name: str = Field(default='job-agent 采集', min_length=1)
    timezone: str = 'Asia/Shanghai'
    interval_seconds: tuple[float, float] = (8, 15)
    platform_timeout_seconds: float = Field(default=600, gt=0, allow_inf_nan=False)
    fetch_jd: bool = False
    refresh_daily: bool = False
    filters: Filters = Field(default_factory=Filters)
    platforms: dict[Platform, PlatformSettings] = Field(default_factory=lambda: {p: PlatformSettings() for p in PLATFORMS})

    @model_validator(mode='before')
    @classmethod
    def migrate_rate(cls,data):
        if not isinstance(data,dict) or 'interval_seconds' not in data:
            return data
        data=dict(data)
        platforms=data.get('platforms',{p:{} for p in PLATFORMS})
        migrated={}
        for name,options in platforms.items():
            options=options.model_dump() if isinstance(options,PlatformSettings) else dict(options)
            rate=options.get('rate')
            values=rate.model_dump() if isinstance(rate,RatePolicy) else dict(rate or {})
            values.setdefault('interval_seconds',data['interval_seconds'])
            options['rate']=values
            migrated[name]=options
        data['platforms']=migrated
        return data

    def rate_policy(self, platform):
        options=self.platforms[platform]
        values=options.rate.model_dump(exclude_unset=True) if options.rate else {}
        if values.get('daily_limit') is None:
            values['daily_limit']=options.daily_page_limit
        values.setdefault('timezone',self.timezone)
        return RatePolicy(**values)

    @field_validator('timezone')
    @classmethod
    def valid_zone(cls, value):
        ZoneInfo(value)
        return value

    @field_validator('interval_seconds')
    @classmethod
    def interval(cls, value):
        import math
        if not all(math.isfinite(v) for v in value) or not 8 <= value[0] <= value[1]:
            raise ValueError('interval_seconds must be ordered and at least 8 seconds')
        return value

    @field_validator('keywords', 'cities')
    @classmethod
    def nonempty(cls, values):
        if any(not v.strip() for v in values):
            raise ValueError('blank keywords/cities are forbidden')
        return list(dict.fromkeys(v.strip() for v in values))
