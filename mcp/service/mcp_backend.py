"""Owned MCP facade: no legacy HTTP calls or live effects during preview."""
import hashlib
import json
import time
from config import load_config
from delivery import token_db
from store import SQLiteDeliveryStore
from store.delivery import config_hash
from engine.resume_contract import resume_block_reason
from . import deliver
from . import tasks


def preview(job_ids=None, limit=None):
    config = load_config()
    with SQLiteDeliveryStore(dry_run=False) as store:
        rows = store.list_jobs()
        # failed = 副作用前失败，可由用户重新确认再投（与 store.confirm_batch 一致）。
        blocked = {'sent', 'unverified', 'skipped'}
        attempted = {r['id'] for r in rows if store.history_blocked(r['id'])}
        queued = {(r.job.platform, r.job.job_id) for r in store.records()
                  if r.status.value in blocked}
        from engine.job_ranking import eligible_activity
        eligible = [r for r in rows if r['url'] and r['id'] not in attempted
                    and (r['platform'], r['platform_job_id']) not in queued
                    and r['legacy_status'] in {None, 'pending', 'ready', 'approved', 'scored'}
                    and config.engine.platforms[r['platform']].enabled and eligible_activity(r,config.engine)]
        if job_ids is not None:
            if not job_ids or len(set(job_ids)) != len(job_ids) or len(job_ids)>30:
                raise ValueError('Select 1–30 distinct job IDs')
            by_id = {str(r['id']): r for r in eligible}
            if any(i not in by_id for i in job_ids):
                raise ValueError('Job unavailable or previously attempted; preview again')
            selected = [by_id[i] for i in job_ids]
        else:
            from engine.job_ranking import rank_jobs
            from store.scoring import scores
            selected = rank_jobs(eligible, config.engine, scores(store))[:limit or config.delivery.preview_limit]
        batch = json.loads(deliver.preview(store, config, job_ids=[r['id'] for r in selected]))
    token = batch['token']
    expires = time.time() + config.delivery.confirm_ttl_minutes * 60
    items = [{**r, 'source_platform': r['platform'], 'id': str(r['id']),
              'greeting': batch['greetings'][r['platform']],
              'greeting_version': batch['greeting_versions'][r['platform']],
              'greeting_mode': config.engine.platforms[r['platform']].greeting_mode,
              'attachment_name': config.engine.platforms[r['platform']].attachment_name,
              'action_id': 'greeting' if r['platform']=='boss' else 'apply_resume',
              'delivery_hold': batch['delivery_holds'].get(r['platform']),
              'delivery_enabled': config.engine.live_send_enabled and not config.engine.dry_run
                                  and r['platform'] not in batch['delivery_holds']}
             for r in selected]
    payload = {'backend': 'own', 'ids': [r['id'] for r in items],
               'items_hash': config_hash(selected), 'expires': expires}
    with token_db() as db:
        db.execute('INSERT INTO approvals(token,payload,expires) VALUES(?,?,?)',
                   (hashlib.sha256(token.encode()).hexdigest(),json.dumps(payload),expires))
        db.commit()
    return {'confirm_token': token, 'expires_at': expires, 'items': items,
            'batch': batch, 'instruction': '展示完整预览，等待用户明确确认/发送；token 不是授权。配置变化须重新预览。'}


def send(token, exclude_ids=None):
    config = load_config()
    if config.engine.dry_run or not config.engine.live_send_enabled:
        raise ValueError('Own MCP real delivery disabled; enable explicitly and preview again')
    with token_db() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT payload,used FROM approvals WHERE token=?',
                         (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
        if not row or row[1]:
            raise ValueError('Unknown or consumed confirmation; preview again')
        payload = json.loads(row[0])
        if payload.get('backend') != 'own' or time.time() >= payload['expires']:
            raise ValueError('Confirmation expired or backend changed; preview again')
        with SQLiteDeliveryStore(dry_run=False) as store:
            batch = store.get_batch(token)
            if batch['config_hash'] != config_hash(config.engine.model_dump()) or batch['status'] != 'pending':
                raise ValueError('Configuration changed or batch consumed; preview again')
            rows = {str(r['id']): r for r in store.list_jobs()}
            selected = [rows.get(i) for i in payload['ids']]
            if config_hash(selected) != payload['items_hash']:
                raise ValueError('Jobs changed; preview again')
            paused = store.paused_platforms()
        excluded = set(exclude_ids or [])
        if not excluded <= set(payload['ids']) or excluded == set(payload['ids']):
            raise ValueError('Invalid exclusions')
        for row in selected:
            if str(row['id']) in excluded:
                continue
            platform = row['platform']
            if platform in paused or resume_block_reason(platform,config.engine.platforms[platform]):
                raise ValueError(f'{platform} 平台暂停或正式简历流程未验收；保留该岗位待核验。')
        db.execute('UPDATE approvals SET used=1 WHERE token=?',(hashlib.sha256(token.encode()).hexdigest(),))
        db.commit()
    # Consume before spawning: an uncertain launch must never replay approval.
    result = tasks.start('delivery', token=token, exclude_ids=sorted(excluded), expires=payload['expires'])
    return {'delivery': result, 'confirmation_consumed': True,
            'job_ids': [i for i in payload['ids'] if i not in excluded]}


def status():
    with SQLiteDeliveryStore(dry_run=False) as store:
        records = [{'platform': r.job.platform, 'job_id': r.job.job_id, 'url': r.job.url,
                    'status': r.status.value, 'reason': r.reason.value, 'error_detail': r.error_detail}
                   for r in store.records()]
    return {'backend': 'own', 'task': tasks.status('delivery'), 'records': records}
