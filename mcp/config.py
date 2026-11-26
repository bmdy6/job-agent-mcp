"""User-owned settings with read-only migration of obsolete configuration."""

import os
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from browser.input_config import InputSettings
from engine.config import EngineSettings
from collect.config import CollectSettings
from daily_config import DailySettings
from scoring_config import ScoringSettings

Platform = Literal['boss', 'liepin', '51job', 'zhilian', 'nowcoder', 'maimai']


class ConfigModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


class BrowserSettings(ConfigModel):
    human_input: InputSettings = Field(default_factory=InputSettings)
    name: str = 'chrome'
    cdp_url: str = 'http://127.0.0.1:9222'
    keepalive_seconds: int = Field(default=300, ge=0, le=86400, strict=True)

    @field_validator('cdp_url')
    @classmethod
    def local_cdp(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (parsed.scheme not in {'http', 'ws'} or parsed.hostname not in {'127.0.0.1', 'localhost', '::1'}
                or parsed.username or parsed.password or not parsed.port):
            raise ValueError('browser.cdp_url must be a loopback HTTP/WS URL with a port')
        return value


class PracticeSettings(ConfigModel):
    max_interviews_per_week: int = Field(default=3, ge=0, strict=True)


class GreetingSettings(ConfigModel):
    top_n: int = Field(default=20, ge=0, le=100, strict=True)


class DeliverySettings(ConfigModel):
    confirm_ttl_minutes: int = Field(default=30, ge=1, le=30, strict=True)
    preview_limit: int = Field(default=5, ge=1, le=30, strict=True)


class AnalyticsSettings(ConfigModel):
    greeting_min_samples: int = Field(default=20, ge=2, le=1000, strict=True)
    activity_days: int = Field(default=7, ge=1, le=365, strict=True)
    score_bands: list[int] = Field(default_factory=lambda: [60, 80])

    @field_validator('score_bands', mode='before')
    @classmethod
    def ordered_bands(cls, values):
        if (not isinstance(values, list) or not values
                or any(type(v) is not int or not 0 < v < 100 for v in values)
                or values != sorted(set(values))):
            raise ValueError('score_bands must be increasing unique integer thresholds between 1 and 99')
        return values


EnvName = Annotated[str, Field(pattern=r'^[A-Za-z_][A-Za-z0-9_]*$')]


class TelegramSettings(ConfigModel):
    type: Literal['telegram'] = 'telegram'
    enabled: bool = False
    bot_token_env: EnvName = 'JOB_AGENT_TG_BOT_TOKEN'
    chat_id: str = ''


class WebhookSettings(ConfigModel):
    type: Literal['webhook'] = 'webhook'
    enabled: bool = False
    format: Literal['generic', 'wecom', 'serverchan'] = 'generic'
    url_env: EnvName = 'JOB_AGENT_WEBHOOK_URL'
    sendkey_env: EnvName = 'JOB_AGENT_SERVERCHAN_SENDKEY'


class DraftSettings(ConfigModel):
    base_url: str = 'https://api.openai.com/v1'
    model: str = ''
    api_key_env: EnvName = 'OPENAI_API_KEY'
    resume_path: str = '~/.job-agent/resume.md'


class NotifySettings(ConfigModel):
    enabled: bool = False
    interval_minutes: int = Field(default=5, ge=1, le=1440, strict=True)
    # Accepted for old user files; saved-reply notifications do not generate drafts.
    draft_reply: bool = False
    delivery_summary: bool = True
    ai: DraftSettings = Field(default_factory=DraftSettings)
    channels: dict[str, Annotated[TelegramSettings | WebhookSettings, Field(discriminator='type')]] = Field(default_factory=dict)

    @field_validator('channels')
    @classmethod
    def channel_names(cls, values):
        import re
        if any(not re.fullmatch(r'[a-z][a-z0-9_-]{0,39}', name) for name in values):
            raise ValueError('channel names must be short lowercase identifiers')
        return values


class ObservabilitySettings(ConfigModel):
    log_max_bytes: int = Field(default=5_000_000, ge=100_000, le=500_000_000, strict=True)
    log_backups: int = Field(default=5, ge=1, le=50, strict=True)
    heartbeat_stale_polls: int = Field(default=3, ge=2, le=20, strict=True)
    heartbeat_daily_grace_minutes: int = Field(default=30, ge=5, le=360, strict=True)


class Settings(ConfigModel):
    scoring: ScoringSettings = Field(default_factory=ScoringSettings)
    daily: DailySettings = Field(default_factory=DailySettings)
    collect: CollectSettings = Field(default_factory=CollectSettings)
    engine: EngineSettings = Field(default_factory=EngineSettings)
    notify: NotifySettings = Field(default_factory=NotifySettings)
    greetings: GreetingSettings = Field(default_factory=GreetingSettings)
    delivery: DeliverySettings = Field(default_factory=DeliverySettings)
    analytics: AnalyticsSettings = Field(default_factory=AnalyticsSettings)
    observability: ObservabilitySettings = Field(default_factory=ObservabilitySettings)
    mode: Literal['practice', 'job'] = 'job'
    practice: PracticeSettings = Field(default_factory=PracticeSettings)
    stretch_salary_min: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    directions: list[str] = Field(default_factory=list)
    cities: list[str] = Field(default_factory=list)
    platforms: list[Platform] = Field(default_factory=list)
    browser: BrowserSettings = Field(default_factory=BrowserSettings)

    @field_validator('directions', 'cities')
    @classmethod
    def nonempty_strings(cls, values: list[str]) -> list[str]:
        if any(not value.strip() for value in values):
            raise ValueError('directions/cities cannot contain blank strings')
        return [value.strip() for value in values]


def job_agent_home() -> Path:
    return Path(os.environ.get('JOB_AGENT_HOME', '~/.job-agent')).expanduser().resolve()


def load_config() -> Settings:
    path = job_agent_home() / 'config.yaml'
    try:
        data = yaml.safe_load(path.read_text(encoding='utf-8')) if path.exists() else {}
    except yaml.YAMLError as exc:
        raise ValueError('Invalid JOB_AGENT_HOME/config.yaml YAML') from exc
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ValueError('config.yaml must be a mapping')
    # Optional additive settings live separately while older monitor runtimes
    # still validate config.yaml with extra='forbid'. Main config wins if upgraded.
    extension = job_agent_home() / 'daily.yaml'
    if extension.exists():
        try:
            extra = yaml.safe_load(extension.read_text(encoding='utf-8')) or {}
        except yaml.YAMLError as exc:
            raise ValueError('Invalid daily.yaml YAML') from exc
        allowed = {'daily': None, 'engine': {'cooldown'}, 'collect': {'refresh_daily'}}
        if not isinstance(extra, dict) or extra.keys()-allowed.keys():
            raise ValueError('daily.yaml accepts only daily, engine.cooldown, collect.refresh_daily')
        for section, keys in allowed.items():
            if section not in extra:
                continue
            value = extra[section]
            if not isinstance(value, dict) or (keys is not None and value.keys()-keys):
                raise ValueError('Invalid daily.yaml section')
            data[section] = {**value, **data.get(section, {})}
    return Settings.model_validate(data)
