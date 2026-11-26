"""Configuration for the owned delivery engine."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator
from .models import Platform, PLATFORMS
from .monitor_config import MonitorSettings
from .cooldown import CooldownSettings

from .rate import RatePolicy


class PlatformSettings(BaseModel):
    model_config = ConfigDict(extra='forbid', validate_assignment=True)
    expected_account_type: Literal['any', 'student', 'professional'] = 'any'
    enabled: bool = Field(default=False, strict=True)
    greeting: str = ''
    greeting_newline_separator: Literal['；', ' '] = '；'
    greeting_max_length: int = Field(default=0, ge=0, strict=True)
    daily_limit: int = Field(default=1, ge=0, strict=True)
    rate: RatePolicy | None = None
    interval_seconds: float = Field(default=120.0, ge=0, allow_inf_nan=False)
    attachment_name: str = ''
    greeting_mode: Literal['send_message', 'platform_default', 'none'] = 'send_message'

    @model_validator(mode='before')
    @classmethod
    def migrate_rate(cls, data):
        if not isinstance(data,dict):
            return data
        data=dict(data)
        rate=data.get('rate')
        values=rate.model_dump(exclude_unset=True) if isinstance(rate,RatePolicy) else dict(rate or {})
        if 'interval_seconds' not in values and 'interval_seconds' in data:
            interval=float(data['interval_seconds'])
            values['interval_seconds']=(interval,max(interval*2,120))
        # Materialize interval defaults once so serialized batch config stays stable.
        values.setdefault('interval_seconds',RatePolicy().interval_seconds)
        data['rate']=values
        return data

    def rate_policy(self):
        values=self.rate.model_dump() if self.rate else RatePolicy().model_dump()
        if values.get('daily_limit') is None:
            values['daily_limit']=self.daily_limit
        return RatePolicy(**values)

    @model_validator(mode='after')
    def check_greeting(self):
        if len(self.greeting.encode('utf-16-le')) // 2 > self.greeting_max_length:
            raise ValueError('Greeting exceeds platform limit; never truncate')
        if self.enabled and self.greeting_max_length == 0:
            raise ValueError('Set a P0-verified greeting limit before enabling platform')
        return self


class EngineSettings(BaseModel):
    model_config = ConfigDict(extra='forbid', validate_assignment=True)
    minimum_hr_activity: Literal['now', 'today', 'three_days', 'week'] | None = None
    experience_priority: list[str] = Field(default_factory=list)
    cooldown: CooldownSettings = Field(default_factory=CooldownSettings)
    backend: Literal['own'] = 'own'
    monitor: MonitorSettings = Field(default_factory=MonitorSettings)
    live_send_enabled: bool = Field(default=False, strict=True)
    dry_run: bool = Field(default=True, strict=True)
    task_space_name: str = Field(default='job-agent 投递', min_length=1)
    timezone: Literal['Asia/Shanghai', 'Asia/Singapore', 'UTC'] = 'Asia/Shanghai'
    platforms: dict[Platform, PlatformSettings] = Field(default_factory=dict)

    @model_validator(mode='before')
    @classmethod
    def platform_limits(cls, data):
        if not isinstance(data, dict): return data
        data = dict(data)
        platforms = dict(data.get('platforms', {}))
        for name, limit in {'boss': 140, 'zhilian': 90}.items():
            value = platforms.get(name, {})
            if isinstance(value, dict):
                platforms[name] = {'daily_limit': limit, **value}
        data['platforms'] = platforms
        return data

    @model_validator(mode='after')
    def defaults(self):
        if self.task_space_name == self.monitor.task_space_name:
            raise ValueError('Dedicated separate delivery and monitor spaces required')
        if not self.task_space_name.strip():
            raise ValueError('Task space name must not be blank')
        for name, cap in {'boss': 1000, 'zhilian': 500, 'liepin': 800}.items():
            settings = self.platforms.get(name)
            if settings and (len(settings.greeting.encode('utf-16-le')) // 2 > cap
                             or settings.greeting_max_length > cap):
                raise ValueError(f'{name} exceeds P0-verified limit {cap}')
        for platform in PLATFORMS:
            self.platforms.setdefault(platform, PlatformSettings())
        return self
