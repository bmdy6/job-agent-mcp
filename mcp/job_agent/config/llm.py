"""Named model settings and legacy resolution. Prices are always user supplied."""
from typing import Literal
from urllib.parse import urlsplit
from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Pricing(StrictModel):
    input_per_million: float = Field(ge=0, allow_inf_nan=False)
    cached_input_per_million: float = Field(ge=0, allow_inf_nan=False)
    output_per_million: float = Field(ge=0, allow_inf_nan=False)
    currency: str = Field(min_length=1, max_length=12, pattern=r'^[A-Za-z]+$')


class ModelSettings(StrictModel):
    base_url: str = 'https://api.openai.com/v1'
    model: str = Field(default='gpt-6.1-sol', min_length=1, max_length=160)
    service_tier: Literal['priority','auto','default','flex'] = 'priority'
    pricing: Pricing | None = None

    @field_validator('pricing', mode='before')
    @classmethod
    def empty_pricing(cls, value):
        return None if value == {} else value

    @field_validator('base_url')
    @classmethod
    def secure_url(cls, value):
        u=urlsplit(value)
        if u.scheme!='https' or not u.hostname or u.username or u.password or u.query or u.fragment:
            raise ValueError('Model base_url must be HTTPS without credentials/query/fragment')
        return value.rstrip('/')


class Budget(StrictModel):
    daily_cost: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    daily_calls: int | None = Field(default=None, ge=0, strict=True)
    currency: str | None = Field(default=None, min_length=1, max_length=12, pattern=r'^[A-Za-z]+$')


class PurposeSettings(Budget):
    model_ref: str | None = Field(default=None, min_length=1, max_length=80)


class LLMSettings(StrictModel):
    models: dict[str, ModelSettings] = Field(default_factory=dict)
    budget: Budget = Field(default_factory=Budget)
    scoring: PurposeSettings = Field(default_factory=PurposeSettings)
    greetings: PurposeSettings = Field(default_factory=PurposeSettings)
    draft_reply: PurposeSettings = Field(default_factory=PurposeSettings)
    reply_classification: PurposeSettings = Field(default_factory=PurposeSettings)


class ProfileSettings(StrictModel):
    name: str = Field(default='候选人', min_length=1, max_length=40)
    role: str = Field(default='做软件开发的', min_length=1, max_length=80)
    availability: str = Field(default='', max_length=80)
    graduation: str = Field(default='', max_length=80)

    @field_validator('name','role','availability','graduation')
    @classmethod
    def single_line(cls, value):
        if '\n' in value or '\r' in value or (value and not value.strip()):
            raise ValueError('Profile values must be single-line text')
        return value


class PromptSettings(StrictModel):
    followup: str = ''
    draft_reply: str = ''
    scoring: str = ''
    greeting: str = ''
    reply_classification: str = ''
    fallback: str = ''


class DeploySettings(StrictModel):
    label_prefix: str = Field(default='com.job-agent', pattern=r'^[A-Za-z0-9]+(?:[.-][A-Za-z0-9]+)*$')


def resolve_model(config, purpose):
    policy=getattr(config.llm,purpose)
    legacy=config.greetings.generate if purpose=='greetings' else config.scoring
    ref=policy.model_ref or getattr(legacy,'model_ref',None)
    if ref:
        return config.llm.models[ref]
    if purpose=='draft_reply' and config.notify.ai.model:
        return ModelSettings(model=config.notify.ai.model,base_url=config.notify.ai.base_url)
    return ModelSettings(model=legacy.model,service_tier=legacy.service_tier,base_url=config.scoring.base_url)


def provider_settings(config, purpose):
    model=resolve_model(config,purpose)
    return config.scoring.model_copy(update=model.model_dump(exclude={'pricing'}))
