"""Monitor-only policies; sparse platform overrides inherit global values."""
from datetime import datetime
import re
from typing import Annotated
from zoneinfo import ZoneInfo
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator, model_serializer
from job_agent.platforms.base import Platform


def valid_hours(value):
    start, end = value
    valid = lambda s: bool(re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', s))
    if not valid(start) or not (valid(end) or end == '24:00') or start == end:
        raise ValueError('Monitor hours require distinct HH:MM endpoints; only end may be 24:00')
    return value


MonitorHours = Annotated[tuple[str, str], AfterValidator(valid_hours)]


def in_hours(hours, now, timezone):
    clock = datetime.fromtimestamp(now, ZoneInfo(timezone)).strftime('%H:%M')
    start, end = hours
    return start <= clock < end if start < end else clock >= start or clock < end


class MonitorNotify(BaseModel):
    model_config = ConfigDict(extra='forbid')
    interview: bool = Field(default=True, strict=True)
    resume_request: bool = Field(default=True, strict=True)
    reply: bool = Field(default=True, strict=True)
    rejection: bool = Field(default=True, strict=True)

    def allows(self, classification):
        return getattr(self, classification if classification in {'interview', 'resume_request', 'rejection'} else 'reply')


class MonitorInterval(BaseModel):
    model_config = ConfigDict(extra='forbid')
    allowed_hours: MonitorHours
    interval_minutes: int = Field(ge=1, le=1440, strict=True)


class MonitorPolicy(BaseModel):
    model_config = ConfigDict(extra='forbid')
    backfill_hours: int = Field(default=48, ge=1, le=8760, strict=True)
    allowed_hours: MonitorHours = ('09:00', '21:00')
    interval_minutes: int = Field(default=5, ge=1, le=1440, strict=True)
    interval_schedule: list[MonitorInterval] = Field(default_factory=list)
    failure_threshold: int = Field(default=3, ge=1, strict=True)
    notify: MonitorNotify = Field(default_factory=MonitorNotify)

    def interval_at(self, now, timezone='Asia/Shanghai'):
        # First matching rule wins; unmatched hours use the base interval.
        return next((rule.interval_minutes for rule in self.interval_schedule
                     if in_hours(rule.allowed_hours, now, timezone)), self.interval_minutes)


class MonitorPlatformSettings(MonitorPolicy):
    enabled: bool = Field(default=True, strict=True)

    @model_serializer(mode='wrap')
    def sparse(self, handler):
        # Settings previews validate a dumped model again. Expanding defaults
        # would turn inheritance into explicit overrides during that roundtrip.
        result = {k:v for k,v in handler(self).items() if k in self.model_fields_set}
        if 'notify' in result:
            result['notify'] = {k:v for k,v in result['notify'].items() if k in self.notify.model_fields_set}
        return result


class MonitorSettings(MonitorPolicy):
    classification_enabled: bool = Field(default=True, strict=True)
    classification_model_enabled: bool = Field(default=False, strict=True)
    classification_batch_limit: int = Field(default=20, ge=1, le=100, strict=True)
    classification_model_limit: int = Field(default=5, ge=0, le=30, strict=True)
    task_space_name: str = 'job-agent 监听'
    platforms: list[Platform] = Field(default_factory=lambda: ['boss', 'zhilian', 'liepin', '51job'])
    platform_overrides: dict[Platform, MonitorPlatformSettings] = Field(default_factory=dict)

    def enabled(self, platform):
        override = self.platform_overrides.get(platform)
        return platform in self.platforms and (override is None or override.enabled)

    def for_platform(self, platform):
        values = self.model_dump(include=set(MonitorPolicy.model_fields))
        override = self.platform_overrides.get(platform)
        if override is not None:
            changed = override.model_dump(exclude_unset=True, exclude={'enabled'})
            values['notify'].update(changed.pop('notify', {}))
            values.update(changed)
        return MonitorPolicy.model_validate(values)

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
