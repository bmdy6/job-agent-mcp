"""Owned funnel; application events add stages without counting event rows."""
from job_agent.applications import SENT, REPLIED
from job_agent.user_data import company_key

FIELDS = ('applied', 'replied', 'interview_scheduled', 'interview_done', 'offers', 'rejected', 'interview_invited')


def stages(event_names):
    names = set(event_names)
    result = set()
    if names & {'greeted', 'manual_applied', 'replied', 'resume_requested',
                'interview_scheduled', 'interviewed', 'interview_done', 'offer'}:
        result.add('applied')
    if names & {'replied', 'resume_requested', 'interview_scheduled', 'interviewed', 'interview_done', 'offer'}:
        result.add('replied')
    if names & {'interview_scheduled', 'interviewed', 'interview_done', 'offer'}:
        result.add('interview_scheduled')
    if names & {'interviewed', 'interview_done', 'offer'}:
        result.add('interview_done')
    if 'offer' in names:
        result.add('offers')
    if 'interview_invited' in names:
        result.update({'applied','replied','interview_invited'})
    if 'rejected' in names:
        result.add('rejected')
    return result


def records_for(jobs, events):
    records = {}
    for job in jobs:
        status = job.get('status')
        records[str(job['id'])] = {
            'platform': job.get('source_platform') or 'boss', 'city': job.get('city') or 'unknown',
            'score': job.get('score'), 'greeting_version': job.get('greeting_version'),
            'stages': ({'applied'} if status in SENT else set())
                      | ({'replied'} if status in REPLIED else set())
                      | ({'rejected'} if status == 'rejected' else set())}
    for event in events:
        record = records.setdefault(str(event['job_id']), {'stages': set()})
        for field in ('platform', 'city', 'score'):
            if record.get(field) is None and event.get(field) is not None:
                record[field] = event[field]
        record['stages'].update(stages([event['event']]))
    return records


def counts(records):
    return {field: sum(field in r['stages'] for r in records) for field in FIELDS}


def with_rates(value):
    return {**value, 'reply_rate': value['replied']/value['applied'] if value['applied'] else None,
            'interview_rate': value['interview_scheduled']/value['applied'] if value['applied'] else None,
            'invitation_rate': value.get('interview_invited',0)/value['applied'] if value['applied'] else None,
            'reply_to_invitation_rate': value.get('interview_invited',0)/value['replied'] if value['replied'] else None}


def band(score, thresholds):
    if not isinstance(score, (int, float)) or isinstance(score, bool) or not 0 <= score <= 100:
        return 'unknown'
    for lower, upper in zip([0]+thresholds, thresholds+[101]):
        if lower <= score < upper:
            return f'{lower}-{upper-1}'
    return 'unknown'


def merged_stats(stats, workbench, activity, events, jobs, group_by, thresholds):
    # The workbench owns early-stage definitions; /api/stats is a status histogram.
    upstream = workbench['funnel']
    base = {'applied': upstream['发送'], 'replied': upstream['回复'],
            'interview_scheduled': 0, 'interview_done': 0, 'offers': 0, 'rejected': upstream['拒绝'], 'interview_invited': 0}
    original = counts(records_for(jobs, []).values())
    merged = records_for(jobs, events)
    extended = counts(merged.values())
    total = with_rates({field: base[field]+extended[field]-original[field] for field in FIELDS})
    buckets = {}
    if group_by:
        for record in merged.values():
            key = band(record.get('score'), thresholds) if group_by == 'score_band' else record.get(group_by) or 'unknown'
            buckets.setdefault(key, []).append(record)
    groups = [{'key': key, **with_rates(counts(items))} for key, items in sorted(buckets.items())]
    warnings = []
    if (group_by or events) and len(jobs) != upstream['采集总数']:
        warnings.append('Saved job population differs from the workbench snapshot; refresh before interpreting local-event reconciliation or groups.')
    if group_by and any(sum(g[f] for g in groups) != total[f] for f in FIELDS):
        warnings.append('Owned store changed between snapshots; grouped counts may differ from the authoritative workbench total. Refresh after its task settles.')
    labels = {'采集总数': upstream['采集总数'], '初筛通过': upstream.get('初筛通过'),
              'AI评分': upstream.get('AI评分'), '人工确认': upstream.get('人工确认'),
              '总投递': total['applied'], '回复': total['replied'], '面试邀约': total.get('interview_invited',0), '约面': total['interview_scheduled'],
              '面试完成': total['interview_done'], 'offer': total['offers'], '拒绝': total['rejected']}
    table = '| 阶段 | 岗位数 |\n| --- | --- |\n'+'\n'.join(f'| {k} | {v} |' for k, v in labels.items() if v is not None)
    return {'total': total, 'table': table, 'group_by': group_by, 'groups': groups,
            'own': {'status_counts': stats, 'funnel': upstream}, 'activity': activity,
            'warnings': warnings,
            'semantics': 'Unique job IDs; local manual applications/interviews extend Owned store. Interview/offer implies application and reply; rejection alone does not. Rates use applied jobs. Activity is raw daily history, not unique applications. Reads one consistent SQLite snapshot.'}


def brief_reminders(jobs, events):
    by_id = {str(j['id']): j for j in jobs}
    candidates = [dict(j, reason='已回复') for j in jobs if j.get('status') in REPLIED]
    for event in events:
        if event['event'] == 'interview_scheduled':
            job = {**event, **by_id.get(str(event['job_id']), {})}
            candidates.append(dict(job, reason='已约面'))
    companies = {}
    for job in candidates:
        if not job.get('company'):
            continue
        key = company_key(job['company'])
        companies[key] = {'company': job['company'], 'job_id': str(job.get('id') or job['job_id']),
                          'url': job.get('url'), 'reason': job['reason'],
                          'message': f"{job['company']}{job['reason']}，可以让我生成公司简报"}
    return {'count': len(companies), 'items': list(companies.values())}
