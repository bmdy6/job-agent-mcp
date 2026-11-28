"""Vacancy age is measured from collection, never from the last metadata refresh."""
from datetime import datetime, timezone


def needs_check(collected_at, max_hours, clock):
    try:
        stamp = datetime.fromisoformat(collected_at.replace('Z','+00:00'))
        stamp = stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)
        age = clock-stamp.timestamp()
        return age > max_hours*3600 or age < 0
    except (ValueError, TypeError, AttributeError):
        return True


def check(engine, job, space_name):
    """Read-only open must verify readiness. No quota, check_applied, input or send."""
    from job_agent.engine.models import Status, Reason
    identity = engine.store.identity(job) if hasattr(engine.store,'identity') else {}
    if not needs_check(identity.get('collected_at'), engine.settings.max_job_age_hours, engine.clock()):
        return True
    adapter = engine.adapters.get(job.platform)
    if adapter is None:
        engine.store.update(job, Status.PENDING, Reason.FRESHNESS_UNKNOWN)
        return False
    if engine.settings.dry_run:
        # Offline simulation has no temporary page lifecycle; preserve fake send call receipts.
        state = adapter.check_freshness(job)
        if state == 'active':
            return True
        engine.store.update(job, Status.SKIPPED if state=='closed' else Status.PENDING,
                            Reason.JOB_CLOSED if state=='closed' else Reason.FRESHNESS_UNKNOWN)
        return False
    result = False
    status, reason = Status.PENDING, Reason.FRESHNESS_UNKNOWN
    try:
        space = engine.spaces.ensure(space_name)
        if engine.preflight.task_space_valid(space) is not True:
            reason = Reason.TASK_SPACE
        else:
            adapter.open_job(job,space)
            # open_job is the existing read-only identity/readiness contract.
            result = True
    except Exception as error:
        code = getattr(error,'code','')
        if code=='job_unavailable':
            status, reason = Status.SKIPPED, Reason.JOB_CLOSED
        elif code in {'risk_control','login_required','space_unavailable','task_space'}:
            reason = {'risk_control':Reason.RISK_CONTROL,'login_required':Reason.LOGIN_REQUIRED,
                      'space_unavailable':Reason.TASK_SPACE,'task_space':Reason.TASK_SPACE}[code]
            engine._inactive_platforms.add(job.platform)
            if code=='risk_control':
                if engine.safety is not None:
                    engine.safety.repo.pause(job.platform,code)
                else:
                    engine.store.pause_platform(job.platform)
    finally:
        try:
            adapter.close()
        except Exception:
            result = False
            engine._inactive_platforms.add(job.platform)
    if not result:
        engine.store.update(job,status,reason)
    return result
