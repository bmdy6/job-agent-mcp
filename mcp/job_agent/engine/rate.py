"""Shared per-platform policy. Durable reservations precede I/O and survive failure."""
from datetime import datetime
import logging
import random
import time
from zoneinfo import ZoneInfo

log=logging.getLogger(__name__)


from job_agent.config.rate import RatePolicy


class RateDeferred(RuntimeError):
    def __init__(self,code, *, retry_at=None):
        self.retry_at=retry_at
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
                raise RateDeferred('cooldown', retry_at=due)
            self.sleep(min(.25,max(0,due-self.clock())))
