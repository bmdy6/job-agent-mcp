"""Format user-only delivery summaries from read-only BossHunter snapshots."""
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json

PLATFORMS = {'boss': 'BOSS', 'liepin': '猎聘', 'zhilian': '智联', '51job': '51job'}


def event_key(port, task_id):
    # Separate namespace from HR replies; stable across watch process restarts.
    return hashlib.sha256(json.dumps([port, 'delivery_summary', str(task_id)],
                                     separators=(',', ':')).encode()).hexdigest()


def task_from(workbench):
    tasks = [t for t in (workbench.get('task'), workbench.get('last_task'))
             if isinstance(t, dict) and t.get('id') and t.get('mode') == 'deliver'
             and t.get('status') in {'completed', 'failed'}]
    return max(tasks, key=lambda t: str(t.get('created_at') or '')) if tasks else None


def timestamp(value, *, history=False):
    try:
        result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        # SQLite CURRENT_TIMESTAMP is UTC; naive workbench datetime.now is local.
        if result.tzinfo is None:
            result = result.replace(tzinfo=timezone.utc) if history else result.astimezone()
        return result
    except (ValueError, TypeError):
        return None


def failure_reason(detail):
    # Never include raw upstream errors: they can contain URLs, tokens or traces.
    detail = str(detail or '').lower()
    if 'attachment' in detail or '附件' in detail:
        return '附件确认不了，等修复后检查原站再补投'
    if any(code in detail for code in ('application_control_unverified', 'application_unverified',
                                       'delivery_unverified', 'send_unverified', 'submit_unverified')):
        return '没确认投上，先检查原站投递记录'
    if any(code in detail for code in ('confirmed_action_changed', 'confirmed_platform_changed', 'approval_changed')):
        return '动作变了，需要重新确认后再投'
    if any(code in detail for code in ('job_page_unavailable', 'job_unavailable', 'job_closed', '下架', '已关闭')):
        return '岗位已下架，跳过即可'
    if any(code in detail for code in ('captcha', 'rate_limit', 'blocked', 'risk_lock', 'consecutive_errors')):
        return '平台风控或频率限制，暂停后再检查'
    if any(code in detail for code in ('login', 'session_expired', '登录')):
        return '登录状态失效，需要重新登录'
    return '其他原因，需查看工作台失败记录'


def outcomes(task, history, jobs):
    start, end = timestamp(task.get('created_at')), timestamp(task.get('updated_at'))
    if not start or not end or end < start:
        return []
    by_id = {str(j['id']): j for j in jobs}
    latest = {}
    for entry in history:
        if entry.get('action') not in {'sent', 'error'} or not entry.get('job_id'):
            continue
        when = timestamp(entry.get('created_at'), history=True)
        if not when or not start <= when <= end:
            continue
        jid = str(entry['job_id'])
        identity = str(entry.get('id', ''))
        order = (when, int(identity) if identity.isdigit() else 0, identity)
        if jid in latest and latest[jid][0] >= order:
            continue
        job = by_id.get(jid, {})
        platform = entry.get('source_platform') or job.get('source_platform') or 'boss'
        company = ' '.join(str(entry.get('company') or job.get('company') or '未知公司').split())[:60]
        latest[jid] = (order, platform, company, entry['action'], failure_reason(entry.get('detail')))
    return [row[1:] for row in latest.values()]


def company_list(companies):
    counts = Counter(companies)
    names, size = [], 0
    for name, count in counts.items():
        label = name + (f'（{count} 个岗位）' if count > 1 else '')
        if size + len(label) > 360:
            names.append(f'…另 {len(counts)-len(names)} 家')
            break
        names.append(label)
        size += len(label) + 1
    return '、'.join(names)


def notification(task, history, jobs):
    rows = outcomes(task, history, jobs)
    successes, failures = defaultdict(list), Counter()
    for platform, company, action, reason in rows:
        if action == 'sent':
            successes[platform].append(company)
        else:
            failures[platform, reason] += 1
    observed_success = sum(map(len, successes.values()))
    observed_failed = sum(failures.values())
    metrics = task.get('metrics') or {}
    def count(name, fallback):
        value = metrics.get(name)
        return value if type(value) is int and value >= 0 else fallback
    success = count('send_success', observed_success)
    failed = count('send_failed', observed_failed)
    deferred = count('send_deferred', 0)
    total = count('send_requested', success + failed + deferred)
    end = timestamp(task.get('updated_at'))
    date = end.astimezone().strftime('%m-%d %H:%M') if end else '时间未知'
    label = '投递任务失败' if task.get('status') == 'failed' else '投递完成'
    known_total = type(metrics.get('send_requested')) is int and metrics['send_requested'] >= 0
    title = f'📮 {label}（{date}）成功 {success} / 失败 {failed}（' + (f'共 {total}' if known_total else '总数未提供') + '）'
    lines = []
    # Show only detail whose count agrees with task metrics; mismatches must not
    # turn unrelated/ambiguous history into confirmed company-level claims.
    if observed_success <= success:
        for platform, companies in sorted(successes.items()):
            lines.append(f'✅ {PLATFORMS.get(platform, "其他平台")} {len(companies)}：{company_list(companies)}')
    else:
        observed_success = 0
    if observed_failed <= failed:
        for (platform, reason), n in sorted(failures.items()):
            lines.append(f'❌ {PLATFORMS.get(platform, "其他平台")} {n}：{reason}')
    else:
        observed_failed = 0
    unexecuted = max(deferred, total - success - failed)
    if unexecuted:
        lines.append(f'⏸ 未执行 {unexecuted}：额度、暂停或跳过的岗位，未计入成功/失败')
    if success > observed_success:
        lines.append(f'ℹ️ 成功明细缺少 {success-observed_success} 条，以工作台任务统计为准')
    if failed > observed_failed:
        lines.append(f'ℹ️ 失败明细缺少 {failed-observed_failed} 条，请到工作台查看')
    if task.get('status') == 'failed':
        lines.append('⚠️ 任务异常结束：'+failure_reason(task.get('error'))+'；未确认的结果不要直接重投')
    return title, '\n'.join(lines) or '本轮没有可确认的投递结果，请查看工作台。'
