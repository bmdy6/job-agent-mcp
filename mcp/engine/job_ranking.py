"""Ordering of unified signals; no platform selectors or site vocabulary here."""
from platforms.signals import ACTIVITY_ORDER


def eligible_activity(job, settings):
    minimum = settings.minimum_hr_activity
    return minimum is None or ACTIVITY_ORDER.index(job.get('hr_activity', 'unknown')) <= ACTIVITY_ORDER.index(minimum)


def rank_jobs(jobs, settings, scores=None):
    scores = scores or {}
    priorities = settings.experience_priority
    def key(job):
        exp = job.get('experience', '').replace(' ', '').replace('～', '-').replace('－', '-')
        preference = priorities.index(exp) if exp in priorities else len(priorities)
        score = scores.get(job['id'], {}).get('score', job.get('legacy_score') or 0)
        return (ACTIVITY_ORDER.index(job.get('hr_activity', 'unknown')), preference,
                -score, job.get('collected_at', ''), job['id'])
    return sorted((j for j in jobs if eligible_activity(j, settings)), key=key)
