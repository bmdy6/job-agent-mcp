"""Persistent one-use approvals. BossHunter remains the only sender and throttle owner."""
import hashlib
import json
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager

from config import load_config
from user_data import local_path, private_directory

BOUND_FIELDS = ('id', 'company', 'title', 'salary', 'url', 'source_platform', 'greeting', 'status', 'greeting_selection', 'action_id', 'delivery_enabled')


PLATFORMS = {'boss', 'liepin', 'zhilian', '51job'}
ACTION_LABELS = {'greeting': '发招呼语', 'apply_resume': '投递简历'}


def annotate(job, capabilities=None):
    platform = job.get('source_platform') or 'boss'
    capability = (capabilities or {}).get(platform, {})
    default = 'greeting' if platform == 'boss' else 'apply_resume'
    action = capability.get('action_id', default)
    allowed = {'greeting'} if platform == 'boss' else ({'greeting', 'apply_resume'} if platform == 'liepin' else {'apply_resume'})
    valid = platform in PLATFORMS and action in allowed
    return {**job, 'source_platform': platform, 'action_id': action,
            'action_label': ACTION_LABELS.get(action, '不支持'),
            'delivery_enabled': valid and (capability.get('enabled') is True
                                          if 'enabled' in capability else platform == 'boss')}


def eligible(job):
    # Application-only platforms do not need a greeting. An unreviewed greeting
    # never becomes sendable merely by switching its platform label.
    return ((job.get('source_platform') or 'boss') in PLATFORMS
            and job.get('status') in {'ready', 'approved'} and not job.get('deleted_at')
            and (job.get('action_id') == 'apply_resume'
                 or (bool(str(job.get('greeting') or '').strip()) and job.get('greeting_selection') != 'pending')))


def select_jobs(jobs, ids, limit):
    by_id = {str(j['id']): j for j in jobs}
    if ids is not None:
        if len(ids) != len(set(ids)) or not ids or len(ids) > 30:
            raise ValueError('Select 1–30 distinct job IDs')
        if any(id not in by_id or not eligible(by_id[id]) for id in ids):
            raise ValueError('Selected job is missing, unready, unsupported or has an unreviewed/empty greeting; preview again')
        return [by_id[id] for id in ids]
    candidates = [j for j in jobs if eligible(j)]
    return sorted(candidates, key=lambda j: (-float(j.get('score') or 0), str(j['id'])))[:limit]


def fingerprint(jobs):
    value = [{field: j.get(field) for field in BOUND_FIELDS} for j in jobs]
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


@contextmanager
def token_db():
    path = local_path('confirmations.sqlite3')
    private_directory(path.parent)
    fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
    os.close(fd)
    connection = sqlite3.connect(path, timeout=30)
    try:
        connection.execute('CREATE TABLE IF NOT EXISTS approvals (token TEXT PRIMARY KEY, payload TEXT NOT NULL, expires REAL NOT NULL, used INTEGER NOT NULL DEFAULT 0)')
        connection.commit()
        yield connection
    finally:
        connection.close()


def preview(jobs, ids, limit, capabilities=None):
    config = load_config()
    limit = limit if limit is not None else config.delivery.preview_limit
    selected = select_jobs([annotate(j, capabilities) for j in jobs], ids, limit)
    if not selected:
        raise ValueError('No ready applications or reviewed greetings to preview')
    token = secrets.token_urlsafe(32)
    expires = time.time()+config.delivery.confirm_ttl_minutes*60
    payload = {'ids': [str(j['id']) for j in selected], 'hash': fingerprint(selected), 'expires': expires,
               'port': config.bosshunter.port, 'selection': ids, 'limit': limit}
    with token_db() as connection:
        connection.execute('DELETE FROM approvals WHERE expires <= ?', (time.time(),))
        connection.execute('INSERT INTO approvals(token, payload, expires) VALUES (?, ?, ?)',
                           (hashlib.sha256(token.encode()).hexdigest(), json.dumps(payload), expires))
        connection.commit()
    return {'items': [{k: j.get(k) for k in ('id', 'company', 'title', 'salary', 'greeting', 'url', 'source_platform', 'action_id', 'action_label', 'delivery_enabled')} for j in selected],
            'confirm_token': token, 'expires_at': expires,
            'instruction': 'Show the complete batch, platforms, actions and greetings to the user. Disabled platforms require local BossHunter configuration and a fresh preview; never enable automatically. Only call send_greetings after their explicit 确认/发送. This token is single-use; excluded jobs must also remain unchanged.'}


def send(token, exclude_ids, all_jobs, api):
    if not token or len(token) > 200:
        raise ValueError('A valid preview confirmation token is required')
    with token_db() as connection:
        # Serializes consumption across threads, stdio clients and server restarts.
        connection.execute('BEGIN IMMEDIATE')
        row = connection.execute('SELECT payload, used FROM approvals WHERE token=?',
                                 (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
        if not row or row[1]:
            raise ValueError('Unknown or already used confirmation token; preview again')
        payload = json.loads(row[0])
        if time.time() >= payload['expires'] or load_config().bosshunter.port != payload['port']:
            raise ValueError('Confirmation expired or BossHunter endpoint changed; preview again')
        jobs = all_jobs()
        capabilities = api('/api/workbench').get('delivery_capabilities', {})
        selected = select_jobs([annotate(j, capabilities) for j in jobs], payload['selection'], payload['limit'])
        if [str(j['id']) for j in selected] != payload['ids'] or fingerprint(selected) != payload['hash']:
            raise ValueError('Job list or greeting changed; preview again')
        excluded = set(exclude_ids or [])
        if not excluded <= set(payload['ids']):
            raise ValueError('exclude_ids must be a subset of the previewed IDs')
        ids = [id for id in payload['ids'] if id not in excluded]
        if not ids:
            raise ValueError('No jobs remain after exclusions')
        included = [j for j in selected if str(j['id']) in ids]
        if any(not j['delivery_enabled'] for j in included):
            raise ValueError('Platform delivery is disabled in local BossHunter configuration; configure and preview again')
        # Recheck expiry after potentially slow pagination and validation.
        if time.time() >= payload['expires'] or load_config().bosshunter.port != payload['port']:
            raise ValueError('Confirmation expired or endpoint changed during validation; preview again')
        connection.execute('UPDATE approvals SET used=1 WHERE token=?', (hashlib.sha256(token.encode()).hexdigest(),))
        connection.commit()
    # Consume before the effect: even a timeout cannot cause the same token to send twice.
    request = {'job_ids': ids, 'direct_send': True}
    if capabilities or any(j['source_platform'] != 'boss' for j in included):
        request['expected_platforms'] = {str(j['id']): j['source_platform'] for j in included}
        request['expected_actions'] = {str(j['id']): j['action_id'] for j in included}
    result = api('/api/workbench/deliver', request)
    return {'delivery': result, 'job_ids': ids, 'confirmation_consumed': True,
            'next': 'Read delivery_status for progress. Do not retry after uncertain outcomes; verify workbench first.'}
