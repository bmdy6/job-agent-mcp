"""Pure application aggregation. Counts are unique jobs, never event rows."""

import re
from user_data import company_key

SENT = {'sent', 'replied', 'resume_sent', 'needs_resume', 'follow_up_sent'}
REPLIED = {'replied', 'resume_sent', 'needs_resume'}


def job_link(job: dict, events: list[dict]) -> dict:
    result = {key: job.get(key) for key in ('id', 'title', 'company', 'status', 'url')}
    result['platform'] = job.get('source_platform') or 'boss'
    chat = job.get('chat_url') or next((e['chat_url'] for e in reversed(events)
                                      if str(e['job_id']) == str(job['id']) and e.get('chat_url')), None)
    if chat:
        result['chat_url'] = chat
    return result


def company_context(company: str, events: list[dict], jobs: list[dict]) -> dict:
    key = company_key(company)
    def matches(value):
        return bool(value and company_key(value) == key)
    selected = [job for job in jobs if matches(job.get('company'))]
    ids = {str(job['id']) for job in selected}
    return {'company': company, 'key': key,
            'events': [e for e in events if matches(e.get('company')) or str(e['job_id']) in ids],
            'jobs': [job_link(j, events) for j in selected]}


def application_funnel(events: list[dict], jobs: list[dict], group_by: str | None = None) -> dict:
    if group_by not in {None, 'resume_version', 'platform', 'variant'}:
        raise ValueError('Unsupported funnel group_by')
    records = {}
    for job in jobs:
        if job.get('status') not in SENT:
            continue
        records[str(job['id'])] = {'platform': job.get('source_platform') or 'boss',
                                  'stages': {'greeted'} | ({'replied'} if job['status'] in REPLIED else set())}
    # Attribute each job to its earliest recorded nonempty metadata.
    # Later events cannot split cohorts.
    for event in events:
        record = records.setdefault(str(event['job_id']), {'stages': set()})
        for field in ('resume_version', 'variant', 'platform'):
            if event.get(field) and field not in record:
                record[field] = event[field]
        name = {'manual_applied': 'greeted', 'interview_done': 'interviewed'}.get(event['event'], event['event'])
        record['stages'].add(name)
    for record in records.values():
        stages = record['stages']
        if stages & {'replied', 'resume_requested', 'interview_scheduled', 'interviewed', 'offer'}:
            stages.update({'greeted', 'replied'})
        if stages & {'interviewed', 'offer'}:
            stages.add('interview_scheduled')
        if 'offer' in stages:
            stages.add('interviewed')

    def metrics(items):
        counts = {stage: sum(stage in item['stages'] for item in items)
                  for stage in ('greeted', 'replied', 'interview_scheduled', 'interviewed', 'offer')}
        greeted = counts['greeted']
        return {'greeted': greeted, 'replied': counts['replied'],
                'reply_rate': counts['replied']/greeted if greeted else None,
                'interview_scheduled': counts['interview_scheduled'],
                'interview_rate': counts['interview_scheduled']/greeted if greeted else None,
                'interviewed': counts['interviewed'], 'offers': counts['offer']}

    groups = {}
    if group_by:
        for record in records.values():
            groups.setdefault(record.get(group_by) or 'unknown', []).append(record)
    return {'group_by': group_by, 'total': metrics(list(records.values())),
            'groups': [{'key': key, **metrics(items)} for key, items in sorted(groups.items())]}


def exclusion_matches(job: dict, preferences: dict) -> dict:
    """Expose literal evidence only; never evaluate suitability or mutate a job."""
    matches = []
    company = job.get('company') or ''
    for blocked in preferences.get('blocked_companies', []):
        if company and company_key(blocked) == company_key(company):
            matches.append({'field': 'blocked_companies', 'value': blocked})
    title_company = (str(job.get('title') or '')+'\n'+company).casefold()
    for field, text in [('deal_breakers', title_company), ('jd_deal_breakers', str(job.get('jd') or '').casefold())]:
        matches.extend({'field': field, 'value': word} for word in preferences.get(field, [])
                       if word and word.casefold() in text)
    # Only parse explicit monthly K ranges. Other salary formats remain unknown.
    salary = str(job.get('salary') or '')
    parsed = re.search(r'(?i)(\d+(?:\.\d+)?)\s*[-~–至]\s*(\d+(?:\.\d+)?)\s*k', salary)
    floor = (preferences.get('salary') or {}).get('min')
    if parsed and floor and float(parsed[2]) < float(floor):
        matches.append({'field': 'salary_min', 'value': floor, 'job_salary': salary})
    return {'matches': matches, 'salary_checked': bool(parsed),
            'scope': 'literal title/company exclusions, JD exclusions, normalized blocked company, monthly K salary upper bound'}
