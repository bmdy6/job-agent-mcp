"""Trusted input parameters; no disabling human input in live mode."""
from pydantic import BaseModel, ConfigDict, Field, model_validator


class InputSettings(BaseModel):
    model_config = ConfigDict(extra='forbid')
    detail_seconds: tuple[float,float] = (3,12)
    detail_scrolls: tuple[int,int] = (1,3)
    hesitation_probability: float = Field(default=.05, ge=0, le=1, allow_inf_nan=False)
    hesitation_backtrack: tuple[float,float] = (.1,.3)
    hesitation_ms: tuple[int,int] = (100,400)
    long_pause_probability: float = Field(default=.03, ge=0, le=1, allow_inf_nan=False)
    long_pause_seconds: tuple[float,float] = (3,10)
    shrink: float = Field(default=.6, gt=0, le=1, allow_inf_nan=False)
    duration_ms: tuple[int,int] = (250,900)
    # Intermediate points, including optional hesitation/overshoot points.
    steps: tuple[int,int] = (3,6)
    step_ms: tuple[int,int] = (50,200)
    mouse_sample_window: int = Field(default=20, ge=1, le=1000, strict=True)
    mouse_point_ms: float = Field(default=1100, gt=0, allow_inf_nan=False)
    gesture_margin_ms: int = Field(default=5000, ge=1, strict=True)
    hold_ms: tuple[int,int] = (50,150)
    typing_ms: tuple[int,int] = (60,180)
    noise_px: float = Field(default=1.5, ge=0, allow_inf_nan=False)
    overshoot_probability: float = Field(default=.3, ge=0, le=1, allow_inf_nan=False)
    overshoot_px: tuple[int,int] = (2,12)

    @model_validator(mode='after')
    def ranges(self):
        for name in ('duration_ms','steps','step_ms','hold_ms','typing_ms','overshoot_px'):
            a,b=getattr(self,name)
            if not 1 <= a <= b:
                raise ValueError('Input ranges must be positive and ordered')
        import math
        for name in ('detail_seconds','detail_scrolls','hesitation_ms','long_pause_seconds','hesitation_backtrack'):
            a,b=getattr(self,name)
            if not all(math.isfinite(v) for v in (a,b)) or not 0<a<=b:
                raise ValueError('Human timing ranges must be positive and ordered')
        if self.hesitation_backtrack[1]>1: raise ValueError('Backtrack must be at most 1')
        if self.steps[0] < 3:
            raise ValueError('At least three intermediate points are required')
        if self.duration_ms[0] > (self.steps[0]+1)*self.step_ms[1] or self.duration_ms[1] < (self.steps[1]+1)*self.step_ms[0]:
            raise ValueError('Duration/step timing must overlap for every allowed step count')
        return self
