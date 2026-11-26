"""Only public Store methods; no SQL and no schema migration."""
from datetime import datetime, timezone

def jobs_by_id(store):
    return {j['id']: j for j in store.list_jobs(include_deleted=True)}

def sent_jobs(store):
    sent = {d['job_id'] for d in store.list_deliveries() if d['status'] == 'sent'}
    return {i: j for i, j in jobs_by_id(store).items() if i in sent}

def ingest(store, platform, events, *, cursor=None):
    with store.transaction():
        all_jobs = jobs_by_id(store)
        eligible = {(j['platform'], str(j['platform_job_id'])): j for j in sent_jobs(store).values()}
        seen = {(all_jobs[r['job_id']]['platform'], r['message_id']) for r in store.list_replies()}
        added = 0
        for event in events:
            key = (event.platform, event.message_id)
            job = eligible.get((event.platform, event.platform_job_id))
            if (event.platform != platform or event.direction != 'incoming' or not event.message_id
                    or not job or key in seen):
                continue
            store.add_reply(job['id'], message_id=event.message_id, direction='inbound', body=event.body,
                            occurred_at=datetime.fromtimestamp(event.occurred_at, timezone.utc).isoformat(), chat_url=event.chat_url,
                            id_origin='application_status' if event.platform == '51job' and event.extra.get('id_origin') == 'application_status' else 'platform')
            seen.add(key)
            added += 1
        if cursor is not None:
            cursor.commit(store)
        return added
