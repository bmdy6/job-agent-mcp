"""Opt-in daily orchestration, independent collection/delivery switches."""
from pydantic import BaseModel, ConfigDict, Field


class DailySettings(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: bool = False
    collect_enabled: bool = True
    deliver_enabled: bool = False
    hour: int = Field(default=8, ge=0, le=23, strict=True)
    minute: int = Field(default=0, ge=0, le=59, strict=True)
