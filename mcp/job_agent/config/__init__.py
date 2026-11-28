"""User-owned settings with read-only migration of obsolete configuration."""

import os
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from job_agent.config.llm import LLMSettings, ProfileSettings, PromptSettings, DeploySettings
from job_agent.config.followup import FollowupSettings
from job_agent.config.qa import QASettings
from job_agent.config.input import InputSettings
from job_agent.config.engine import EngineSettings
from job_agent.config.collect import CollectSettings
from job_agent.config.daily import DailySettings
from job_agent.config.scoring import ScoringSettings
from job_agent.platforms.greeting import AI_CLICHE_TERMS, JARGON_TERMS, CLOSING_LINES, HOOK_PREFIXES, HOOK_SUFFIXES

Platform = Literal['boss', 'liepin', '51job', 'zhilian', 'nowcoder', 'maimai']


class ConfigModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


class BrowserSettings(ConfigModel):
    human_input: InputSettings = Field(default_factory=InputSettings)
    name: str = 'chrome'
    cdp_url: str = 'http://127.0.0.1:9222'
    startup_timeout_seconds: int = Field(default=60, ge=5, le=300, strict=True)
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


class GreetingGenerateSettings(ConfigModel):
    enabled: bool = Field(default=False, strict=True)
    model_ref: str | None = Field(default=None, min_length=1, max_length=80)
    daily_cost: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    model: str = Field(default='gpt-6.1-sol', min_length=1, max_length=160)
    service_tier: Literal['priority', 'auto', 'default', 'flex'] = 'priority'
    facts: list[str] = Field(default_factory=list, max_length=100)
    blacklist: list[str] = Field(default_factory=lambda: list(AI_CLICHE_TERMS), max_length=100)
    jargon_terms: list[str] = Field(default_factory=lambda: list(JARGON_TERMS), max_length=200)
    max_sentence_length: int = Field(default=45, ge=15, le=60, strict=True)
    max_retries: int = Field(default=1, ge=0, le=1, strict=True)
    daily_budget_calls: int = Field(default=300, ge=0, le=10000, strict=True)
    templates_path: str = '$JOB_AGENT_HOME/greeting-templates.yaml'
    hook_prefixes: list[str] = Field(default_factory=lambda: list(HOOK_PREFIXES), min_length=1, max_length=100)
    hook_suffixes: list[str] = Field(default_factory=lambda: list(HOOK_SUFFIXES), min_length=1, max_length=100)
    closing_lines: list[str] = Field(default_factory=lambda: list(CLOSING_LINES), min_length=1, max_length=30)
    similarity_threshold: float = Field(default=0.78, ge=0.0, le=1.0)
    max_sentence_pattern: int = Field(default=3, ge=1, le=3, strict=True)

    @field_validator('closing_lines')
    @classmethod
    def short_closings(cls, values):
        if len(set(values)) != len(values) or any(not v.strip() or len(v) > 18 or '\n' in v or '简历' not in v for v in values):
            raise ValueError('closing_lines must be unique short resume invitations')
        return values

    @field_validator('facts', 'blacklist', 'jargon_terms')
    @classmethod
    def real_facts(cls, values):
        if any(not v.strip() or len(v)>2000 for v in values):
            raise ValueError('facts must contain nonblank bounded statements')
        return values


class GreetingSettings(ConfigModel):
    generate: GreetingGenerateSettings = Field(default_factory=GreetingGenerateSettings)
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
    # QA-guided suggestions are user-only; never automatically sent to HR.
    draft_reply: bool = False
    # Reply categories that get a draft even without a QA match (drafted from
    # profile + greeting facts + confirmed anecdotes). Empty = QA matches only.
    draft_reply_categories: list[Literal['interview','resume_request','question','rejection','other']] = Field(default_factory=list)
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
    heartbeat_cooldown_minutes: int = Field(default=120, ge=5, le=1440, strict=True)
    heartbeat_daily_stale_minutes: int = Field(default=120, ge=10, le=1440, strict=True)
    heartbeat_daily_grace_minutes: int = Field(default=30, ge=5, le=360, strict=True)


class Settings(ConfigModel):
    qa: QASettings = Field(default_factory=QASettings)
    followup: FollowupSettings = Field(default_factory=FollowupSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    profile: ProfileSettings = Field(default_factory=ProfileSettings)
    prompts: PromptSettings = Field(default_factory=PromptSettings)
    deploy: DeploySettings = Field(default_factory=DeploySettings)
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

    @model_validator(mode='after')
    def model_references(self):
        for purpose in ('scoring','greetings','reply_classification','draft_reply'):
            legacy=self.greetings.generate if purpose=='greetings' else self.scoring
            ref=getattr(self.llm,purpose).model_ref or (getattr(legacy,'model_ref',None) if purpose!='reply_classification' else None)
            if ref and ref not in self.llm.models:
                raise ValueError('Unknown model_ref')
        return self

    @field_validator('directions', 'cities')
    @classmethod
    def nonempty_strings(cls, values: list[str]) -> list[str]:
        if any(not value.strip() for value in values):
            raise ValueError('directions/cities cannot contain blank strings')
        return [value.strip() for value in values]


def production_home() -> Path:
    return (Path.home()/'.job-agent').resolve()


def reject_test_production_path(path: Path) -> None:
    import sys
    if (os.environ.get('JOB_AGENT_TESTING') or 'unittest' in sys.modules or 'pytest' in sys.modules):
        if path.resolve() == production_home() or path.resolve().is_relative_to(production_home()):
            raise RuntimeError('Tests must never access production JOB_AGENT_HOME')


def job_agent_home() -> Path:
    path = Path(os.environ.get('JOB_AGENT_HOME', '~/.job-agent')).expanduser().resolve()
    reject_test_production_path(path)
    return path


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
