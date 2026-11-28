"""Opt-in daily orchestration, independent collection/delivery switches."""
from pydantic import BaseModel, ConfigDict, Field


class DailySettings(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: bool = False
    collect_enabled: bool = True
    deliver_enabled: bool = False
    hour: int = Field(default=8, ge=0, le=23, strict=True)
    minute: int = Field(default=0, ge=0, le=59, strict=True)
    # A single unverified outcome pauses only that platform; the 30-minute periodic
    # trigger resumes it after the cooldown. Too many unknown outcomes stop it for the day.
    unverified_cooldown_minutes: int = Field(default=30, ge=0, le=1440, strict=True)
    unverified_daily_stop: int = Field(default=3, ge=1, le=50, strict=True)
