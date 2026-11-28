"""Persist per-channel, per-chunk receipts in the owned notification outbox."""
import json
import time
from datetime import datetime, timezone
import job_agent.notify as notify
from job_agent.monitor.repository import sent_jobs
from job_agent.monitor.receipts import digest


def poll_replies(store, config, *, wait_for_classification=False):
    result = {'sent': 0, 'failed': 0, 'skipped': 0, 'suppressed': 0}
    channels = {name: c for name, c in config.notify.channels.items() if c.enabled and c.type == 'telegram'}
    if not config.notify.enabled or not channels:
        return result
    from job_agent.store.notifications import notification_sender
    with notification_sender(time.monotonic()+50, store):
        policy = store.monitor_policy({'activated_at': time.time(),
            'backfill_hours': config.engine.monitor.backfill_hours,
            'cutoff': time.time()-config.engine.monitor.backfill_hours*3600})
        jobs = sent_jobs(store)
        from job_agent.store.signals import invitations
        invitation_rows = {i['reply_id']:i for i in invitations(store)}
        links = store.monitor_state().get('_links', {})
        seen = set()
        for reply in store.list_replies():
            job = jobs.get(reply['job_id'])
            if not job or reply['direction'] != 'inbound':
                continue
            options = config.engine.monitor.for_platform(job['platform'])
            if not config.engine.monitor.enabled(job['platform']):
                result['suppressed'] += 1
                continue
            identity = (job['platform'], reply['message_id'])
            if identity in seen:
                continue
            seen.add(identity)
            try:
                stamp = datetime.fromisoformat(reply['occurred_at'].replace('Z', '+00:00'))
                if stamp.tzinfo is None:
                    raise ValueError('Unknown timezone')
                eligible = stamp.timestamp() >= policy['activated_at'] - options.backfill_hours * 3600
            except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
                eligible = False
            if not eligible:
                result['suppressed'] += 1
                continue
            if wait_for_classification and config.engine.monitor.classification_enabled and reply.get('classification_source') in {'pending','processing'}:
                result['skipped'] += 1
                continue
            is_status = job['platform'] == '51job' and reply.get('id_origin') == 'application_status'
            status_category = {'邀面试':'interview', '不合适':'rejection', '不匹配':'rejection'}.get(
                reply['message_id'].rsplit(':', 1)[-1]) if is_status else None
            category = status_category or reply.get('classification')
            if not category:
                from job_agent.service.reply_classification import rule
                category = (rule(reply['body']) or {}).get('category', 'other')
            if not options.notify.allows(category):
                result['suppressed'] += 1
                continue
            key = 'reply:' + digest(*identity)
            body = reply['body']
            if not is_status:
                from job_agent.service.draft_reply import suggest
                draft = suggest(store,reply,config,job=job,category=category)
                if draft:
                    body += '\n\n建议回复（未发送）：\n' + draft['text']
            invitation = category == 'interview'
            label = '51job 状态变化，没有正文' if is_status else '面试邀约（待确认）' if invitation else 'HR 回复'
            title = f"{label} · {job['company']} · {job['title']}"
            link = reply.get('chat_url') or links.get(json.dumps(list(identity))) or job['url'] or ''
            from zoneinfo import ZoneInfo
            when = stamp.astimezone(ZoneInfo(config.engine.timezone)).strftime('%m-%d %H:%M')
            text_prefix = (f"平台：{job['platform']}\n公司：{job['company'][:100]}\n"
                           f"岗位：{job['title'][:100]}\n"
                           + (f"观测时间：{when}\n状态记录：\n" if is_status else f"回复时间：{when}\nHR 回复：\n"))
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
                    receipt_key = 'reply:'+part+':'+name
                    if store.notification_sent(receipt_key):
                        result['skipped'] += 1
                        continue
                    ident = store.enqueue_notification(receipt_key, name, title, text_prefix+chunk, link or None, kind='reply')
                    outcome = notify.send_one(channel, title, text_prefix + chunk, link or None, durable=False)
                    store.notification_result(ident, outcome['success'], outcome.get('error_code',''))
                    if not outcome['success']:
                        result['failed'] += 1
                        continue
                    result['sent'] += 1
    return result
