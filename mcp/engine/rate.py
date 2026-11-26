"""Shared per-platform policy. Durable reservations precede I/O and survive failure."""
from datetime import datetime
import logging
import random
import time
from zoneinfo import ZoneInfo
from pydantic import BaseModel, ConfigDict, Field, model_validator

log=logging.getLogger(__name__)


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


class RateDeferred(RuntimeError):
    def __init__(self,code):
        self.code=code
        super().__init__(code)


def operation_times(operations, history=()):
    """Merge reservations with pre-reservation attempt history, as the limiter does."""
    return list(operations) + [t for t in history if t < min(operations, default=float('inf'))]


def daily_usage(times, now, timezone):
    day = lambda t: datetime.fromtimestamp(t, ZoneInfo(timezone)).date()
    return sum(day(t) == day(now) for t in times)


class RateLimiter:
    def __init__(self,repo,*,clock=time.time,sleep=time.sleep,seed=None,should_stop=lambda:False):
        self.repo,self.clock,self.sleep=repo,clock,sleep
        self.random=random.Random(seed)
        self.should_stop=should_stop

    def acquire(self,platform,scope,policy,*,history=(), block=True):
        started=self.clock()
        while True:
            if self.should_stop():
                raise RateDeferred('stopped')
            # Check and reserve atomically across collection/monitor/delivery processes.
            with self.repo.store.transaction():
                now=self.clock()
                if platform in self.repo.pauses():
                    raise RateDeferred('risk_control')
                if not policy.allowed(now):
                    raise RateDeferred('outside_hours')
                state=self.repo.state(platform,'pacing')
                operations=self.repo.operations(platform,scope)
                times=operation_times(operations, history)
                if policy.daily_limit is not None and daily_usage(times, now, policy.timezone)>=policy.daily_limit:
                    raise RateDeferred('daily_limit')
                if sum(now-3600<t<=now for t in times)>=policy.hourly_limit:
                    raise RateDeferred('hourly_limit')
                due=state.get('due',0)
                if not state and times:
                    due=max(times)+self.random.uniform(*policy.interval_seconds)
                    self.repo.set_state(platform,'pacing',{'due':due,'count':0,'target':self.random.randint(*policy.rest_every)})
                if now>=due:
                    count=state.get('count',0)+1
                    target=state.get('target') or self.random.randint(*policy.rest_every)
                    wait=self.random.uniform(*policy.interval_seconds)
                    if count>=target:
                        wait=max(wait,self.random.uniform(*policy.rest_seconds))
                        count,target=0,self.random.randint(*policy.rest_every)
                    self.repo.record(platform,scope,now,{'due':now+wait,'count':count,'target':target})
                    log.info('rate platform=%s scope=%s at=%.3f waited=%.3f interval=%s',platform,scope,now,now-started,now-max(times) if times else None)
                    return
            if not block:
                raise RateDeferred('cooldown')
            self.sleep(min(.25,max(0,due-self.clock())))
