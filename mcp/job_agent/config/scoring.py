"""Validated paid-scoring settings. No credentials are resolved at config load."""
from typing import Literal
from urllib.parse import urlsplit
from pydantic import BaseModel, ConfigDict, Field, field_validator


class ScoringSettings(BaseModel):
    model_config = ConfigDict(extra='forbid')
    model_ref: str | None = Field(default=None, min_length=1, max_length=80)
    daily_cost: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    daily_budget_calls: int | None = Field(default=None, ge=0, strict=True)
    require_ratelimit_header: bool = True
    model: str = Field(default='gpt-6.1-sol',min_length=1,max_length=160)
    service_tier: Literal['priority','auto','default','flex'] = 'priority'
    base_url: str = 'https://api.openai.com/v1'
    max_completion_tokens: int = Field(default=1200,ge=256,le=8192,strict=True)
    connect_timeout_seconds: int = Field(default=5,ge=1,le=30,strict=True)
    timeout_seconds: int = Field(default=60,ge=5,le=180,strict=True)
    max_jobs: int = Field(default=10,ge=1,le=10,strict=True)
    max_input_chars: int = Field(default=60000,ge=1000,le=200000,strict=True)
    resume_path: str = '~/.job-agent/resume.md'
    key_source: Literal['vault','env'] = 'vault'
    api_key_env: str = Field(default='OPENAI_API_KEY',pattern=r'^[A-Za-z_][A-Za-z0-9_]*$')
    vault_path: str = '~/.config/ai-vault/vault.json'
    vault_group: str = 'personal-infra'
    vault_service: str = 'openai'
    vault_key: str = 'OPENAI_PLATFORM_API_KEY'

    @field_validator('base_url')
    @classmethod
    def secure_url(cls,value):
        u=urlsplit(value)
        if u.scheme!='https' or not u.hostname or u.username or u.password or u.query or u.fragment:
            raise ValueError('scoring.base_url must be an HTTPS URL without credentials/query/fragment')
        return value.rstrip('/')
