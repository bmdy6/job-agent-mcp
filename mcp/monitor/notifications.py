"""Persist per-channel, per-chunk receipts using watch's existing receipt log."""
import fcntl
import json
import os
import time
from datetime import datetime, timezone
from service.tasks import read, write
from config import job_agent_home
import notify
from .repository import sent_jobs
from .receipts import digest, read_receipts


def poll_replies(store, config, *, wait_for_classification=False):
    result = {'sent': 0, 'failed': 0, 'skipped': 0, 'suppressed': 0}
    channels = {name: c for name, c in config.notify.channels.items() if c.enabled and c.type == 'telegram'}
    if not config.notify.enabled or not channels:
        return result
    home = job_agent_home()
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = os.open(home/'notified.lock', os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {**result, 'busy': True}
        policy_path = home / 'monitor-policy.json'
        policy = read(policy_path)
        if policy is None:
            policy = {'activated_at': time.time(), 'backfill_hours': config.engine.monitor.backfill_hours}
            policy['cutoff'] = policy['activated_at'] - policy['backfill_hours'] * 3600
            write(policy_path, policy)
        cutoff = policy['cutoff']
        fd = os.open(home/'notified.jsonl', os.O_CREAT | os.O_RDWR | os.O_APPEND, 0o600)
        with os.fdopen(fd, 'a+', encoding='utf-8') as stream:
            receipts, _ = read_receipts(stream)
            jobs = sent_jobs(store)
            from store.signals import invitations
            invitation_rows = {i['reply_id']:i for i in invitations(store)}
            state_path = home/'monitor-state.json'
            links = json.loads(state_path.read_text()).get('_links', {}) if state_path.exists() else {}
            seen = set()
            for reply in store.list_replies():
                job = jobs.get(reply['job_id'])
                if not job or reply['direction'] != 'inbound':
                    continue
                identity = (job['platform'], reply['message_id'])
                if identity in seen:
                    continue
                seen.add(identity)
                try:
                    stamp = datetime.fromisoformat(reply['occurred_at'].replace('Z', '+00:00'))
                    if stamp.tzinfo is None:
                        raise ValueError('Unknown timezone')
                    eligible = stamp.timestamp() >= cutoff
                except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
                    eligible = False
                if not eligible:
                    result['suppressed'] += 1
                    continue
                if wait_for_classification and config.engine.monitor.classification_enabled and reply.get('classification_source') in {'pending','processing'}:
                    result['skipped'] += 1
                    continue
                key = 'reply:' + digest(*identity)
                body = reply['body']
                is_status = job['platform'] == '51job' and reply.get('id_origin') == 'application_status'
                invitation = reply.get('classification') == 'interview'
                label = '51job 状态变化，没有正文' if is_status else '面试邀约（待确认）' if invitation else 'HR 回复'
                title = f"{label} · {job['company']} · {job['title']}"
                link = reply.get('chat_url') or links.get(json.dumps(list(identity))) or job['url'] or ''
                text_prefix = (f"平台：{job['platform']}\n公司：{job['company'][:100]}\n"
                               f"岗位：{job['title'][:100]}\n" + ("状态记录：\n" if is_status else "HR 回复：\n"))
                if invitation:
                    item = invitation_rows.get(reply['id'], {})
                    text_prefix += f"时间原文：{item.get('time_text') or '未说明'}\n形式：{item.get('form','unknown')}\n需本人决定是否接受。\n"
                overhead = title[:100] + '\n\n' + text_prefix + ('\n\n' + link if link else '')
                budget = 4096 - len(overhead.encode('utf-16-le')) // 2
                chunks, chunk, units = [], [], 0
                for character in body:
                    size = 2 if ord(character) > 0xffff else 1
                    if units + size > 1400:
                        chunks.append(''.join(chunk))
                        chunk, units = [], 0
                    chunk.append(character)
                    units += size
                chunks.append(''.join(chunk))
                for index, chunk in enumerate(chunks):
                    if len(chunk.encode('utf-16-le')) // 2 > budget:
                        result['failed'] += 1
                        continue
                    part = key + ':' + str(index)
                    for name, channel in channels.items():
                        if (part, name) in receipts:
                            result['skipped'] += 1
                            continue
                        outcome = notify.send_one(channel, title, text_prefix + chunk, link or None)
                        if not outcome['success']:
                            result['failed'] += 1
                            continue
                        stream.write(json.dumps({'event': part, 'job': key, 'channel': name})+'\n')
                        stream.flush()
                        os.fsync(stream.fileno())
                        receipts.add((part, name))
                        result['sent'] += 1
        return result
    finally:
        os.close(lock)
