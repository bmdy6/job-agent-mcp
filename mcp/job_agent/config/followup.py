"""Opt-in followups; all runtime state stays in jobs.sqlite3."""
from typing import Annotated
from pydantic import BaseModel, ConfigDict, Field, model_validator


class GenerateSettings(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: bool = Field(default=False, strict=True)


class FollowupSettings(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: bool = Field(default=False, strict=True)
    auto: bool = Field(default=False, strict=True)
    after_days: tuple[Annotated[int, Field(strict=True)], Annotated[int, Field(strict=True)]] = (1, 3)
    max_age_days: int = Field(default=7, ge=1, le=365, strict=True)
    daily_limit: int = Field(default=10, ge=0, le=200, strict=True)
    messages: list[str] = Field(default_factory=lambda: [
        '你好，我还在关注这个岗位。有需要补充的材料或项目细节，随时找我就好。'], min_length=1, max_length=100)
    generate: GenerateSettings = Field(default_factory=GenerateSettings)

    @model_validator(mode='after')
    def valid(self):
        low, high = self.after_days
        if any(type(v) is not int for v in self.after_days) or not 1 <= low <= high <= self.max_age_days:
            raise ValueError('after_days must be an ordered positive range within max_age_days')
        if any(not isinstance(t,str) or not t.strip() or len(t)>1000 for t in self.messages):
            raise ValueError('Followup messages must be nonempty bounded text')
        return self
