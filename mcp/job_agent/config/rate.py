"""Shared pacing configuration."""
from datetime import datetime
from zoneinfo import ZoneInfo
from pydantic import BaseModel, ConfigDict, Field, model_validator

class RatePolicy(BaseModel):
    model_config=ConfigDict(extra='forbid',validate_assignment=True)
    interval_seconds: tuple[float,float] = (45.0,120.0)
    hourly_limit: int = Field(default=20, ge=0, strict=True)
    daily_limit: int | None = Field(default=None, ge=0, strict=True)
    rest_every: tuple[int,int] = (8,15)
    rest_seconds: tuple[float,float] = (300.0,900.0)
    allowed_hours: tuple[str,str] = ('09:00','21:00')
    timezone: str = 'Asia/Shanghai'

    @model_validator(mode='after')
    def ranges(self):
        import math
        ZoneInfo(self.timezone)
        for name in ('interval_seconds','rest_every','rest_seconds'):
            a,b=getattr(self,name)
            if not all(math.isfinite(v) for v in (a,b)) or not 0<=a<=b:
                raise ValueError('Rate ranges must be finite, nonnegative and ordered')
        if self.rest_every[0]<1:
            raise ValueError('Rest frequency must be positive')
        for value in self.allowed_hours:
            if len(value)!=5 or value[2]!=':' or not 0<=int(value[:2])<=23 or not 0<=int(value[3:])<=59:
                raise ValueError('Allowed hours require HH:MM')
        if self.allowed_hours[0]==self.allowed_hours[1]:
            raise ValueError('Allowed hours must form a nonempty window')
        return self

    def allowed(self, now):
        current=datetime.fromtimestamp(now,ZoneInfo(self.timezone)).strftime('%H:%M')
        start,end=self.allowed_hours
        return start<=current<end if start<end else current>=start or current<end
