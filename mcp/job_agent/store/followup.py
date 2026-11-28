"""Followup policy over append-only delivery evidence; schema v7, no sidecar state."""
import json
import random
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from job_agent.config.calendar import effective_limit
from job_agent.config.followup import FollowupSettings
from job_agent.platforms.spec import spec_for


def epoch(stamp):
    value = datetime.fromisoformat(stamp.replace('Z','+00:00'))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.timestamp()


def schedule(store, row, options, first_at):
    key = 'followup-delay:' + str(row['id'])
    event = store.connection.execute('SELECT summary FROM events WHERE source_key=?',(key,)).fetchone()
    if not event:
        store.append_event({'job_id':row['id'], 'after_days':random.randint(*options.after_days), 'first_sent_at':first_at}, source_key=key)
        event = store.connection.execute('SELECT summary FROM events WHERE source_key=?',(key,)).fetchone()
    return json.loads(event[0])


def eligible(store, job_id, options, engine, at, *, batch_token=None):
    row = store.get_job(job_id)
    if not row or row['deleted_at'] or not row['url']:
        return None
    pcfg = engine.platforms.get(row['platform'])
    if (not pcfg or not pcfg.enabled or pcfg.greeting_mode != 'send_message'
            or not spec_for(row['platform']).supports_followup):
        return None
    if row['legacy_status'] in {'follow_up_sent','followup_sent'} or store.connection.execute("SELECT 1 FROM source_history WHERE job_id=? AND action IN ('follow_up_sent','followup_sent') LIMIT 1",(job_id,)).fetchone():
        return None
    if store.connection.execute('SELECT 1 FROM replies WHERE job_id=? LIMIT 1',(job_id,)).fetchone():
        return None
    if store.connection.execute("SELECT 1 FROM deliveries WHERE job_id=? AND kind='followup' AND (? IS NULL OR batch_id IS NOT ? OR status<>'pending') LIMIT 1",(job_id,batch_token,batch_token)).fetchone():
        return None
    if store.connection.execute('SELECT 1 FROM events WHERE source_key=?',('followup-attempt:'+str(job_id),)).fetchone():
        return None
    pending = store.connection.execute('SELECT batch_id FROM delivery_queue WHERE job_id=?',(job_id,)).fetchone()
    if pending and pending[0] and pending[0] != batch_token:
        batch = store.get_batch(pending[0])
        if batch['greetings'].get('_kind') == 'followup':
            return None  # Reserved batches never re-enter planning, even after a crash.
    sent_rows = store.connection.execute("SELECT * FROM deliveries WHERE job_id=? AND status='sent' AND kind<>'followup'",(job_id,)).fetchall()
    if not sent_rows:
        return None
    try:
        sent = min(sent_rows,key=lambda r:epoch(r['created_at']))
        first_at = epoch(sent['created_at'])
        # The first successful batch's own queue marker is exact. Older failed
        # attempts must not move that first confirmed contact further into the past.
        attempt = store.connection.execute('SELECT attempted_at FROM delivery_queue WHERE job_id=? AND batch_id IS ?',
            (job_id,sent['batch_id'])).fetchone()
        if attempt and attempt[0] is not None and 0<=first_at-attempt[0]<=600:
            first_at = attempt[0]
        planned = schedule(store,row,options,first_at)
        first_at = planned.get('first_sent_at',first_at)
        age = at-first_at
        if not 0 <= age <= options.max_age_days*86400:
            return None
        delay = planned['after_days']
        if age < delay*86400:
            return None
    except (ValueError,TypeError,OverflowError):
        return None
    original = sent['greeting'] or next((r['greeting'] for r in sent_rows if r['greeting']), '')
    return {**row, 'first_sent_at':first_at, 'after_days':delay, 'original_greeting':original}


def used_today(store, engine, at):
    day = datetime.fromtimestamp(at,ZoneInfo(engine.timezone)).date()
    return sum(datetime.fromtimestamp(json.loads(r[0])['at'],ZoneInfo(engine.timezone)).date()==day
        for r in store.connection.execute("SELECT summary FROM events WHERE source_key LIKE 'followup-attempt:%'"))


def candidates(store, config, *, clock=None, blocked=()):
    options = config.followup
    if not options.enabled:
        return []
    at = time.time() if clock is None else clock
    blocked = set(blocked) | set(store.paused_platforms())
    from job_agent.engine.rate import operation_times, daily_usage
    from job_agent.config.calendar import delivery_policy
    remaining = {}
    has_rate = store.connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='operation_rate'").fetchone()
    for p in config.engine.platforms:
        history = store.attempt_times(p)
        operations = [r[0] for r in store.connection.execute(
            "SELECT at FROM operation_rate WHERE platform=? AND scope='delivery'",(p,))] if has_rate else []
        policy = delivery_policy(config.engine,p,at)
        used = max(daily_usage(history,at,config.engine.timezone),
                   daily_usage(operation_times(operations,history),at,policy.timezone))
        remaining[p] = max(0,effective_limit(config.engine,p,at)-used)
    cap = max(0,options.daily_limit-used_today(store,config.engine,at))
    result = []
    with store.transaction():
        for row in store.list_jobs():
            if len(result)>=cap:
                break
            p = row['platform']
            if p in blocked or remaining.get(p,0)<=0:
                continue
            found = eligible(store,row['id'],options,config.engine,at)
            if found:
                result.append(found); remaining[p]-=1
    return result


def batch_options(store, token):
    if not token:
        return None
    greetings = store.get_batch(token)['greetings']
    if greetings.get('_kind') != 'followup':
        return None
    return FollowupSettings.model_validate(greetings['_followup'])


def execution_allowed(store, job, engine, at, token):
    options = batch_options(store,token)
    if options is None:
        return True
    if not options.enabled or not options.auto or used_today(store,engine,at)>=options.daily_limit:
        return False
    return eligible(store,store.identity(job)['id'],options,engine,at,batch_token=token) is not None
