"""Configurable identity comparison. Platform HR IDs are never compared across sites."""
import re
import unicodedata


from job_agent.config.cooldown import CooldownSettings


def normalize(value, patterns=()):
    value = unicodedata.normalize('NFKC', str(value or '')).casefold().strip()
    for pattern in patterns:
        value = re.sub(pattern, '', value, flags=re.IGNORECASE).strip()
    return re.sub(r'[\s·,，。:：()（）\[\]【】_-]+', '', value)


def duplicate(job, prior, settings, now):
    if now - prior['attempted_at'] >= settings.days * 86400:
        return False
    for field, patterns in [('company',settings.company_patterns), ('title',settings.title_patterns)]:
        a,b = normalize(job.get(field),patterns), normalize(prior.get(field),patterns)
        if not a or not b or a != b:
            return False
    a,b = str(job.get('hr_id') or ''), str(prior.get('hr_id') or '')
    if job['platform'] == prior['platform'] and a and b:
        return a == b
    a,b = normalize(job.get('hr_name')), normalize(prior.get('hr_name'))
    return a == b if a and b else True
