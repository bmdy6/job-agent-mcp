"""Configuration for the owned delivery engine."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator
from job_agent.engine.models import Platform, PLATFORMS
from job_agent.config.monitor import MonitorSettings
from job_agent.config.cooldown import CooldownSettings

from job_agent.config.rate import RatePolicy
from job_agent.config.calendar import Weekday, DayLimit, DayHours


class PlatformSettings(BaseModel):
    model_config = ConfigDict(extra='forbid', validate_assignment=True)
    expected_account_type: Literal['any', 'student', 'professional'] = 'any'
    enabled: bool = Field(default=False, strict=True)
    greeting: str = ''
    greeting_newline_separator: Literal['；', ' '] = '；'
    greeting_paragraph_separator: Literal['\n\n', ''] = '\n\n'
    greeting_max_length: int = Field(default=0, ge=0, strict=True)
    daily_limit: int = Field(default=1, ge=0, strict=True)
    day_limits: dict[Weekday, DayLimit] = Field(default_factory=dict)
    day_allowed_hours: dict[Weekday, DayHours] = Field(default_factory=dict)
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
        if (self.enabled or self.greeting_max_length) and len(self.greeting.encode('utf-16-le')) // 2 > self.greeting_max_length:
            raise ValueError('Greeting exceeds platform limit; never truncate')
        if self.enabled and self.greeting_max_length == 0:
            raise ValueError('Set a P0-verified greeting limit before enabling platform')
        return self


class EngineSettings(BaseModel):
    model_config = ConfigDict(extra='forbid', validate_assignment=True)
    greeting: str = ''
    greeting_newline_separator: Literal['；', ' '] = '；'
    greeting_paragraph_separator: Literal['\n\n', ''] = '\n\n'
    minimum_hr_activity: Literal['now', 'today', 'three_days', 'week'] | None = None
    experience_priority: list[str] = Field(default_factory=list)
    # Delivery-time filters for jobs already in the pool (collection filters only see new jobs).
    # company_exclude: case-insensitive substrings of the company name, e.g. big companies.
    company_exclude: list[str] = Field(default_factory=list)
    # Same rule for job titles, e.g. 实习 / intern.
    title_exclude: list[str] = Field(default_factory=list)
    # Same rule for the JD body, e.g. 外包 / 驻场 / 人力外派. Phrases in jd_exclude_ignore
    # (e.g. 非外包) are removed first so a JD that denies the term is not excluded.
    jd_exclude: list[str] = Field(default_factory=list)
    jd_exclude_ignore: list[str] = Field(default_factory=list)
    # Skip jobs whose salary lower bound (monthly K) is above this; None disables.
    salary_floor_max_k: float | None = Field(default=None, gt=0)
    # Saturday/Sunday (engine timezone) per-platform cap; min with daily_limit. None disables.
    weekend_daily_limit: int | None = Field(default=None, ge=0, le=200, strict=True)
    day_limits: dict[Weekday, DayLimit] = Field(default_factory=dict)
    day_allowed_hours: dict[Weekday, DayHours] = Field(default_factory=dict)
    cooldown: CooldownSettings = Field(default_factory=CooldownSettings)
    backend: Literal['own'] = 'own'
    monitor: MonitorSettings = Field(default_factory=MonitorSettings)
    live_send_enabled: bool = Field(default=False, strict=True)
    # true: an unverified (unknown) outcome stops that platform for the rest of the batch.
    # false: skip just that job (never retried) and keep delivering the others.
    stop_on_unverified: bool = Field(default=False, strict=True)
    max_job_age_hours: float = Field(default=48.0, gt=0, allow_inf_nan=False)
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
        if any(key in data for key in ('greeting', 'greeting_newline_separator', 'greeting_paragraph_separator')):
            for name in PLATFORMS:
                platforms.setdefault(name, {})
        for name, value in list(platforms.items()):
            if isinstance(value, dict):
                platforms[name] = {key: data[key] for key in ('greeting', 'greeting_newline_separator', 'greeting_paragraph_separator') if key in data} | value
            elif isinstance(value, PlatformSettings):
                inherited = {key: data[key] for key in ('greeting', 'greeting_newline_separator', 'greeting_paragraph_separator') if key in data and key not in value.model_fields_set}
                platforms[name] = PlatformSettings.model_validate(value.model_dump() | inherited) if inherited else value
        for name, limit in {'boss': 140, 'zhilian': 90, 'nowcoder': 2, 'maimai': 2}.items():
            value = platforms.get(name, {})
            if isinstance(value, dict):
                platforms[name] = {'daily_limit': limit, **value}
                if name in {'nowcoder','maimai'}:
                    platforms[name]['rate'] = {'interval_seconds': (120,240), 'hourly_limit': 2, **(value['rate'].model_dump(exclude_unset=True) if isinstance(value.get('rate'),RatePolicy) else (value.get('rate') or {}))}
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
            self.platforms.setdefault(platform, PlatformSettings(
                daily_limit=2, rate=RatePolicy(interval_seconds=(120,240),hourly_limit=2))
                if platform in {'nowcoder','maimai'} else PlatformSettings())
        return self
