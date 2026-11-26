from pydantic import BaseModel, ConfigDict, Field, field_validator
from platforms.base import Platform

class MonitorSettings(BaseModel):
    model_config = ConfigDict(extra='forbid')
    classification_enabled: bool = Field(default=True, strict=True)
    classification_model_enabled: bool = Field(default=False, strict=True)
    classification_batch_limit: int = Field(default=20, ge=1, le=100, strict=True)
    classification_model_limit: int = Field(default=5, ge=0, le=30, strict=True)
    task_space_name: str = 'job-agent 监听'
    backfill_hours: int = Field(default=48, ge=1, le=8760, strict=True)
    interval_minutes: int = Field(default=5, ge=1, le=1440, strict=True)
    failure_threshold: int = Field(default=3, ge=1, strict=True)
    platforms: list[Platform] = Field(default_factory=lambda: ['boss', 'zhilian', 'liepin', '51job'])

    @field_validator('task_space_name')
    @classmethod
    def space_name(cls, value):
        if not value.strip():
            raise ValueError('A dedicated monitor space is required')
        return value.strip()

    @field_validator('platforms')
    @classmethod
    def unique_platforms(cls, value):
        if len(value) != len(set(value)):
            raise ValueError('Duplicate monitor platforms')
        return value
