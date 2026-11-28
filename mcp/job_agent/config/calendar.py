"""One calendar policy for planning, execution and settings inspection."""
from datetime import datetime
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from pydantic import AfterValidator, Field
from job_agent.config.rate import RatePolicy

Weekday = Literal['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']
DayLimit = Annotated[int, Field(ge=0, strict=True)]


def valid_hours(value):
    return RatePolicy(allowed_hours=value).allowed_hours


DayHours = Annotated[tuple[str, str], AfterValidator(valid_hours)]


def weekday(engine, now):
    return ('mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun')[
        datetime.fromtimestamp(now, ZoneInfo(engine.timezone)).weekday()]


def effective_limit(engine, platform, now):
    options = engine.platforms[platform]
    day = weekday(engine, now)
    limits = [options.daily_limit]
    rule = options.day_limits.get(day, engine.day_limits.get(day))
    if rule is not None:
        limits.append(rule)
    if day in {'sat', 'sun'} and engine.weekend_daily_limit is not None:
        limits.append(engine.weekend_daily_limit)
    rate = RatePolicy.model_validate(options.rate or {})
    if rate.daily_limit is not None:
        limits.append(rate.daily_limit)
    return min(limits)


def delivery_policy(engine, platform, now):
    options = engine.platforms[platform]
    values = RatePolicy.model_validate(options.rate or {}).model_dump()
    values['daily_limit'] = effective_limit(engine, platform, now)
    day = weekday(engine, now)
    hours = options.day_allowed_hours.get(day, engine.day_allowed_hours.get(day))
    if hours is not None:
        values['allowed_hours'] = hours
    return RatePolicy(**values)
