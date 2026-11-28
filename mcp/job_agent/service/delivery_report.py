"""One bounded, shared vocabulary for manual and daily delivery notifications."""
from datetime import datetime, timedelta
from job_agent.platforms.spec import PLATFORM_LABELS

DETAIL_MARK = '—— 明细 ——'
STATUS_LABELS = {'sent':'成功','unverified':'不明','failed':'失败','skipped':'跳过','pending':'待续'}
STATUS_MARKS = {'sent':'✅','unverified':'⚠️','failed':'❌'}
REASON_LABELS = {
    'followup_ineligible': '已不符合跟进条件',
    'platform_busy':'平台正忙，稍后继续', 'disabled':'平台未启用',
    'daily_limit':'今日额度已用完', 'hourly_limit':'本小时额度已用完', 'outside_hours':'不在投递时段',
    'cooldown':'近期已投同一公司/HR/岗位', 'job_unavailable':'岗位已关闭',
    'job_closed':'岗位已下架，不占额度', 'freshness_unknown':'岗位新鲜度尚未确认，等待复查',
    'account_type':'账号类型不符', 'session_unknown':'登录状态未知', 'already_applied':'原站显示已投过',
    'login_required':'未登录', 'risk_control':'原站风控', 'task_space':'浏览器空间不可用',
    'attachment':'附件不匹配', 'precheck_error':'发送前检查失败，未发送',
    'not_implemented':'平台未支持', 'resume_flow_unverified':'正式简历流程尚未验收',
    'resume_attachment_required':'缺少明确配置附件', 'interrupted':'执行中断，结果未知',
    'receipt_unknown':'已执行，未拿到原站回执', 'application_confirmed_greeting_unknown':'原站已确认申请，招呼语未确认',
    'effect_error':'执行中出错，结果未知', 'not_started':'发送前拦下，未发送',
    'risk_control_paused':'原站风控暂停，需本人处理', 'login_paused':'登录暂停，需本人处理',
    'collection_error_or_previous_batch_stop':'采集或此前批次异常，本轮暂停',
    'unverified_cooldown':'结果不明冷却中', 'unverified_daily_stop':'今日不明结果达上限，明天继续',
    'delivery_disabled':'本轮未启用投递','platform_disabled':'平台未启用',
    'no_eligible_candidates':'没有符合条件的候选', 'no_confirmed_receipt':'尚未取得成功回执',
    'collection_timeout':'采集超时', 'delivery_pending':'有岗位待续', 'stopped':'已停止',
    'collection_busy_or_stopped':'采集正忙或已停止', 'collection_error':'采集异常',
    'company_excluded':'公司在排除名单', 'salary_floor_too_high':'起薪高于上限',
    'title_excluded':'岗位名在排除名单','jd_excluded':'JD 含排除词','known_cooldown':'近期已投过',
    'attempted_pending':'待处理快照已有尝试标记', 'legacy_filtered':'历史筛除',
}
LEGACY_REASONS = {
    '原站明确显示岗位不存在/已下架':'job_unavailable', '当日额度已用完':'daily_limit',
    '原站显示已投递/已沟通':'already_applied',
    '原站申请已成功，招呼语回执未确认（不重投）':'application_confirmed_greeting_unknown',
}
PENDING_REASONS = {'platform_busy','daily_limit','hourly_limit','outside_hours','disabled',
                   'resume_flow_unverified','resume_attachment_required','freshness_unknown'}
SKIP_REASONS = {'cooldown','job_unavailable','job_closed','already_applied','not_started','legacy_filtered'}


def reason_label(code):
    return REASON_LABELS.get(LEGACY_REASONS.get(code,code),'其他原因（见 dashboard）')


def clean(value):
    return ' '.join(str(value or '').split())


def job_line(item):
    status=item.get('status')
    if status not in STATUS_MARKS:
        return None
    head='｜'.join(clean(item.get(k)) or '未提供' for k in ('company','title','salary','city'))
    reason=reason_label(item['reason']) if item.get('reason') else ('原因未记录（见 dashboard）' if status=='failed' else '')
    return f'{STATUS_MARKS[status]} {head} — {STATUS_LABELS[status]}' + (f'：{reason}' if reason else '')


def utf16(text):
    return len(text.encode('utf-16-le'))//2


def bounded_details(title, lines, details):
    """Keep whole detail rows and reserve the exact omitted-row footer."""
    base='\n'.join(lines)
    kept=[]
    for line in details:
        candidate=kept+[line]
        omitted=len(details)-len(candidate)
        suffix=f'\n其余 {omitted} 条见 dashboard' if omitted else ''
        if utf16(title+'\n\n'+base+'\n'+'\n'.join(candidate)+suffix)>4050:
            break
        kept=candidate
    omitted=len(details)-len(kept)
    text=base+('\n'+'\n'.join(kept) if kept else '')
    if omitted:
        text+=f'\n其余 {omitted} 条见 dashboard'
    return text


def render(stats, *, state='completed', today_success=None, next_resume_at=None, items=(), legacy_details=(), dry_run=False, extra_lines=()):
    pending=sum(s.get('pending',0) for s in stats.values())
    errors=state in {'failed','interrupted'} or any(s.get('execution_error') or s.get('failed') for s in stats.values())
    label='出错' if errors else '推迟，等待下次触发继续' if pending or state=='deferred' else '完成'
    if not errors and (pending or state=='deferred') and next_resume_at:
        when=datetime.fromisoformat(next_resume_at)
        label=f'推迟到 {when:%m-%d %H:%M} 继续'
    total='、'.join(f'{PLATFORM_LABELS.get(p,"其他平台")} {n}' for p,n in (today_success or {}).items())
    title=('DRY-RUN 模拟（未投递）' if dry_run else f'📮 本轮{label}')+'｜今日累计成功：'+(total or ('0' if today_success is not None else '未记录'))
    lines=[];empty=[]
    for p,s in stats.items():
        if not any(s.get(k,0) for k in STATUS_LABELS) and not s.get('execution_error'):
            holds=list(dict.fromkeys(reason_label(s[k]) for k in ('stop_reason','resume_block_reason') if s.get(k)))
            label=PLATFORM_LABELS.get(p,'其他平台')
            empty.append(label+('（'+'；'.join(holds)+'）' if holds else ''))
            continue
        parts=[]
        for status in ('sent','unverified','skipped','pending','failed'):
            n=s.get(status,0)
            if status=='failed' and not n:continue
            text=f'{STATUS_LABELS[status]} {n}'
            reasons=s.get('reasons_by_status',{}).get(status)
            if reasons is None:
                reasons={k:v for k,v in s.get('reasons',{}).items() if
                         LEGACY_REASONS.get(k,k) in (PENDING_REASONS if status=='pending' else SKIP_REASONS if status=='skipped' else set())}
            if n and reasons:
                top=sorted(reasons.items(),key=lambda kv:-kv[1])[:2]
                text+='（'+'、'.join(f'{reason_label(k)} {v}' for k,v in top)+'）'
            parts.append(text)
        holds=list(dict.fromkeys(reason_label(s[k]) for k in ('stop_reason','resume_block_reason') if s.get(k)))
        if holds:parts.append('暂停：'+'、'.join(holds))
        lines.append(PLATFORM_LABELS.get(p,'其他平台')+'：'+'、'.join(parts))
    if empty:lines.append('本轮无动作：'+'、'.join(empty))
    if not dry_run:
        lines.append('口径：BOSS 成功指沟通回执，不等于简历送达；猎聘指正式简历回执；51job 指定附件回执已确认，未发送招呼语。')
    lines.extend(extra_lines)
    lines.append(DETAIL_MARK)
    details=[line for item in items if (line:=job_line(item))]
    if not items:
        details=[line for line in legacy_details if line.startswith(('✅ ','⚠️ ','❌ '))]
    return title,bounded_details(title,lines,details)


def next_resume(config, stats, now, *, hourly_times=None):
    """Earliest policy-eligible time; pending receipts themselves are never replayed."""
    from zoneinfo import ZoneInfo
    candidates=[]
    for p,s in stats.items():
        if not s.get('pending') or p not in config.engine.platforms:continue
        cfg=config.engine.platforms[p]
        if not cfg.enabled or s.get('stop_reason'):continue
        policy=cfg.rate_policy()
        if policy.hourly_limit==0 or policy.daily_limit==0:continue
        current=now.astimezone(ZoneInfo(policy.timezone))
        reasons={LEGACY_REASONS.get(k,k) for k in s.get('reasons',{})}
        if 'daily_limit' in reasons:current=(current+timedelta(days=1)).replace(hour=0,minute=0,second=0,microsecond=0)
        if 'hourly_limit' in reasons:
            times=sorted(t for t in (hourly_times or {}).get(p,[]) if now.timestamp()-3600<t<=now.timestamp())
            due=(times[len(times)-policy.hourly_limit]+3600) if len(times)>=policy.hourly_limit else now.timestamp()
            current=max(current,datetime.fromtimestamp(due,current.tzinfo))
        # Shared policy's allowed() handles timezone and overnight windows.
        for _ in range(2881):
            if policy.allowed(current.timestamp()):break
            current=(current+timedelta(minutes=1)).replace(second=0,microsecond=0)
        if policy.allowed(current.timestamp()):candidates.append(current)
    return min(candidates).isoformat() if candidates else None


def today_success_counts(store, now=None):
    """Distinct confirmed jobs today, including earlier rounds; never count unknowns."""
    from job_agent.store.reporting import success_counts
    return success_counts(store.connection,now or datetime.now().astimezone())
