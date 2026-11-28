"""One daily round: collection, collection summary, immutable delivery batches, summary.

Only this explicit daily opt-in can confirm a batch without an interactive preview.
The run lock covers selection and effects. Reports survive interrupted processes.
"""
from job_agent.observability import run_scope, current_run_id, report_error
import logging
from collections import Counter
import random
from contextlib import contextmanager
from datetime import datetime
from job_agent.service.daily_schedule import same_day, wait_until_active
from job_agent.store.state import read_run
import fcntl
import json
import os
from job_agent.config import job_agent_home, load_config
from job_agent.engine.models import PLATFORMS, Status, Reason
from job_agent.engine.resume_contract import resume_block_reason
from job_agent.platforms.spec import spec_for
from job_agent.store import SQLiteDeliveryStore
from job_agent.service.deliver import preview, confirm, DETAIL_MARK, generation_summary


class DailyAlreadyRunning(RuntimeError):
    """Another daily round holds the lock; a concurrent trigger is expected, not a failure."""


@contextmanager
def run_lock(home):
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd=os.open(home/'daily.lock',os.O_CREAT|os.O_RDWR,0o600)
    try:
        try:
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            raise DailyAlreadyRunning('Daily round already running') from None
        yield
    finally:
        os.close(fd)


def send(config, title, text):
    from job_agent.notify import send_one
    if not config.notify.enabled:
        return []
    return [name for name,channel in config.notify.channels.items()
            if channel.enabled and send_one(channel,title,text,durable=False)['success']]


def save(home, result):
    from job_agent.store import Store
    result['updated_at']=datetime.now().astimezone().isoformat()
    with Store(home/'jobs.sqlite3') as store:
        store.save_daily_report(result)


def queue_summary(store, result, config, title, text, *, topic=None):
    from job_agent.service.deliver import notification_channels
    from job_agent.monitor.receipts import digest
    ident = result['run_id']
    # Freeze the originating run ID; a later heartbeat must not relabel the message.
    text += '\n\n运行编号：'+ident[:8]
    items = [dict(key='daily:'+ident+':'+digest(topic or (title,text))+':'+channel,
                  channel=channel,title=title,text=text) for channel in notification_channels(config)]
    return store.save_daily_report(result, items)


def send_summary(store, result, config, title, text, *, topic=None):
    import time
    from job_agent.store.notifications import notification_sender
    with notification_sender(time.monotonic()+50, store):
        queued = queue_summary(store, result, config, title, text, topic=topic)
        entries = {entry['id']:entry for entry in store.notifications()}
        previous = [channel.split(':',2)[1] for channel,ident in queued.items() if entries[ident]['status']=='sent']
        pending_config = config.model_copy(deep=True)
        for name in previous:
            pending_config.notify.channels[name].enabled = False
        sent = send(pending_config, title, text) if not queued or len(previous)<len(queued) else []
        for channel,ident in queued.items():
            store.notification_result(ident,channel.split(':',2)[1] in sent)
        return previous + sent


def delivery_checkpoint(store, result, config):
    """Called inside the delivery-result transaction, before a process can crash."""
    from copy import deepcopy
    report = deepcopy(result)
    report['status'] = 'interrupted'
    report['delivery'] = {}
    report['delivery_items'] = []
    tokens = {token for values in report.get('batches', {}).values() for token in values}
    jobs = {job for token in tokens for job in store.execution_jobs(token)}
    for record in store.records():
        if record.job not in jobs:
            continue
        platform = record.job.platform
        stats = report['delivery'].setdefault(platform, dict.fromkeys(('sent','failed','unverified','skipped','pending'),0))
        stats[record.status.value] += 1
        if record.reason.value:
            reasons = stats.setdefault('reasons', {})
            reasons[record.reason.value] = reasons.get(record.reason.value,0)+1
        detail = store.get_job(store.identity(record.job)['id']) or {}
        report['delivery_items'].append({**{k:detail.get(k,'') for k in ('company','title','salary','city')},
            'platform':platform,'status':record.status.value,'reason':record.reason.value})
    try:
        from job_agent.service.delivery_report import today_success_counts, next_resume
        from job_agent.store.reporting import hourly_times
        report['today_success'] = today_success_counts(store)
        report['next_resume_at'] = next_resume(config,report['delivery'],datetime.now().astimezone(),
            hourly_times=hourly_times(store.connection,report['delivery'],datetime.now().astimezone()))
        title, text = delivery_message(report)
    except Exception as error:
        title, text = 'job-agent 投递结果', '投递结果已入库，汇总暂不可用，请查看投递状态。'
        store.append_event({'tool':'notification_render','result':{'status':'error','error_type':type(error).__name__}})
    # Keep the live run state while freezing an interruption-safe fallback body.
    report['status'] = result['status']
    queue_summary(store, report, config, title, text, topic='delivery')



# Both only mean collection ended early today; saved jobs stay deliverable.
NON_BLOCKING_COLLECTION_ERRORS = frozenset({'collection_timeout', 'daily_limit'})


def delivery_message(report):
    from job_agent.service.delivery_report import render, next_resume
    if 'today_success' not in report or 'next_resume_at' not in report:
        from job_agent.store.reporting import legacy_context
        context=legacy_context(report)
        if context:
            report={**report}
            report.setdefault('today_success',context['today_success'])
            if 'next_resume_at' not in report:
                report['next_resume_at']=next_resume(load_config(),report.get('delivery',{}),context['when'],
                                                   hourly_times=context['hourly_times'])
    from job_agent.service.llm_usage import summary, summary_text
    extra_lines=summary_text(summary(load_config())).splitlines()
    if report.get('greeting_generation'):
        extra_lines.append('招呼语生成：' + '、'.join(f'{k} {v}' for k, v in sorted(report['greeting_generation'].items())))
    title, text = render(report.get('delivery', {}), state='failed' if report.get('errors') else report.get('status', 'completed'),
                  today_success=report.get('today_success'), next_resume_at=report.get('next_resume_at'),
                  items=report.get('delivery_items', []), legacy_details=report.get('delivery_details', []),extra_lines=extra_lines)
    if report.get('followup'):
        section=report['followup']
        text += '\n\n跟进：' + ('仅预览，未发送' if section['mode']=='preview' else '已关闭' if section['mode']=='disabled' else '自动跟进')
        for item in section['items']:
            text += f"\n{item['company']}｜{item['title']}｜{item['status']}"
    return title, text


def summarize(report, config):
    report['summary']={}
    for p,options in config.engine.platforms.items():
        collection=report.get('collection',{}).get('platforms',{}).get(p,{})
        stats=report.get('delivery',{}).get(p,{})
        excluded=report.get('planning_exclusions',{}).get(p,{})
        sent=stats.get('sent',0)
        reason=(stats.get('stop_reason') or stats.get('execution_error') or report.get('error') or
                ('delivery_disabled' if not config.daily.deliver_enabled else
                 'platform_disabled' if not options.enabled else
                 'no_eligible_candidates' if not report.get('candidate_counts',{}).get(p,0) else
                 'no_confirmed_receipt')) if not sent else ''
        report['summary'][p]=dict(collection_status=collection.get('error') or
            ('limited' if collection.get('limited') else 'completed' if collection else
             'not_started' if config.daily.collect_enabled and config.collect.platforms.get(p) and config.collect.platforms[p].enabled else 'disabled'),
            collected_pages=collection.get('pages',0),last_page=collection.get('last_page'),
            eligible_candidates=report.get('candidate_counts',{}).get(p,0),
            cooldown_excluded=excluded.get('known_cooldown',0),actual_deliveries=sent,zero_reason=reason)


def unverified_today(store, now=None):
    """Per platform: (final unverified outcomes since local midnight, latest attempt timestamp)."""
    now = datetime.now().astimezone() if now is None else now
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    rows = store.connection.execute("""SELECT j.platform, COUNT(*), MAX(q.attempted_at) FROM delivery_queue q
        JOIN jobs j ON j.id=q.job_id WHERE q.status='unverified' AND q.attempted_at>=? GROUP BY j.platform""", (midnight,))
    return {r[0]: (r[1], r[2]) for r in rows}


def _daily_once(*, result, previous, config=None, max_per_platform=None, collect_fn=None, confirm_fn=None, collection_page_limit=None, scheduled=False):
    from job_agent.service.automation import hold
    if deferred := hold('daily'):
        result.update(deferred)
        return result
    config=config or load_config()
    home=job_agent_home()
    if not config.daily.enabled:
        return {'disabled':True}
    if config.daily.deliver_enabled and (config.engine.dry_run or not config.engine.live_send_enabled):
        raise ValueError('Daily delivery requires explicit live opt-in')
    if config.collect.task_space_name in {config.engine.task_space_name,config.engine.monitor.task_space_name}:
        raise ValueError('Collection, delivery and monitor spaces must be separate')
    if max_per_platform is not None and (type(max_per_platform) is not int or not 1<=max_per_platform<=3):
        raise ValueError('Acceptance cap must be 1..3 jobs/platform')
    if collect_fn is None:
        from job_agent.collect.runtime import collect_once
        collect_fn=collect_once
    confirm_fn=confirm_fn or confirm
    with run_scope('daily'):
        if scheduled:
            wait_until_active(config)
            if deferred := hold('daily'):
                result.update(deferred)
                return result
        result['resumed']=same_day(previous) and previous.get('status') != 'completed'
        result['started_at']=(previous['started_at'] if result['resumed'] else datetime.now().astimezone().isoformat())
        result['last_progress_at']=previous.get('last_progress_at',result['started_at']) if result['resumed'] else result['started_at']
        result['collection_complete']=bool(result['resumed'] and previous.get('collection_complete'))
        if result['collection_complete']:
            result['collection']=previous.get('collection',{})
        result['current_step']='collection'
        logging.getLogger(__name__).info('daily stage=collection run_id=%s',current_run_id())
        with SQLiteDeliveryStore(home/'jobs.sqlite3',dry_run=False) as store:
            if config.daily.deliver_enabled and config.notify.delivery_summary:
                store.result_notification_hook = lambda: delivery_checkpoint(store, result, config)
            cutoff=store.connection.execute('SELECT COALESCE(MAX(id),0) FROM jobs').fetchone()[0]
            if result['resumed']:
                cutoff=previous.get('old_cutoff_id',cutoff)
            result['old_cutoff_id']=cutoff
            save(home,result)
            try:
                if config.daily.collect_enabled and not result['collection_complete']:
                    result['collection']=collect_fn(**({'page_limit':collection_page_limit} if collection_page_limit is not None else {}))
                    if deferred := hold('daily'):
                        result.update(deferred)
                        return result
                    result['collection_complete']=not (result['collection'].get('busy') or result['collection'].get('stopped') or any(
                        s.get('error') and s['error'] not in NON_BLOCKING_COLLECTION_ERRORS for s in result['collection'].get('platforms',{}).values()))
                    if not result['collection'].get('failed') and not result['collection'].get('busy'):
                        result['last_progress_at']=datetime.now().astimezone().isoformat()
                    lines=[]
                    for p,s in result['collection'].get('platforms',{}).items():
                        lines.append(f"{p}：新增 {s.get('new',0)} / 刷新 {s.get('refreshed',0)} / 过滤 {s.get('filtered',0)} / JD {s.get('details',0)} / 页面 {s.get('pages',0)}" +
                                     (f" / 停止：{s['error']}" if s.get('error') else ''))
                    try:
                        result['notifications']['collection']=send_summary(store,result,config,'job-agent 采集结果','\n'.join(lines) or '本轮没有启用的采集平台')
                    except Exception as e:
                        report_error(e,stage='notification',config=config,alert=False)
                        result['notifications']['collection_error']=type(e).__name__
                save(home,result)
                if deferred := hold('daily'):
                    result.update(deferred)
                    return result
                result['current_step']='delivery'
                logging.getLogger(__name__).info('daily stage=delivery run_id=%s',current_run_id())
                save(home,result)
                paused=store.paused_platforms()
                # A collection timeout only means collection stopped early: jobs already saved
                # (and older candidates) stay deliverable. Risk/login/space errors still block.
                blocked={p for p,s in result['collection'].get('platforms',{}).items()
                         if s.get('error') and s.get('error') not in NON_BLOCKING_COLLECTION_ERRORS} | paused.keys()
                resume_blocks={p:reason.value for p,cfg in config.engine.platforms.items() if cfg.enabled
                               and (reason:=resume_block_reason(p,cfg))}
                blocked.update(resume_blocks)
                if result['collection'].get('busy') or result['collection'].get('stopped'):
                    blocked=set(PLATFORMS)
                # Unknown outcomes: cool the platform down, then the periodic trigger resumes it;
                # after unverified_daily_stop of them today, stop that platform until tomorrow.
                cooling={}
                now_ts=datetime.now().astimezone().timestamp()
                for p,(count,latest) in (unverified_today(store).items() if config.engine.stop_on_unverified else ()):
                    if p in blocked: continue
                    if count>=config.daily.unverified_daily_stop:
                        cooling[p]='unverified_daily_stop'
                    elif latest and now_ts-latest<config.daily.unverified_cooldown_minutes*60:
                        cooling[p]='unverified_cooldown'
                blocked.update(cooling)
                result['candidate_counts']={}
                if config.daily.deliver_enabled:
                    result['selected']=[]
                    result['planning_exclusions']={}
                    consumed=Counter()
                    result['delivery']={p:{**dict.fromkeys(('sent','failed','unverified','skipped','pending'),0),'reasons':{}} for p in PLATFORMS}
                    for p in blocked:
                        stats=result['delivery'][p]
                        stats['stop_reason']=('risk_control_paused' if p in paused else cooling[p] if p in cooling
                                              else resume_blocks.get(p,'collection_error_or_previous_batch_stop'))
                        if p in resume_blocks:
                            stats['resume_block_reason']=resume_blocks[p]
                    for stage in ('old','new'):
                        selected=store.daily_candidates(config.engine,cutoff=cutoff,max_per_platform=max_per_platform,blocked=blocked,
                            planning_exclusions=result['planning_exclusions'] if stage=='old' else None,
                            candidate_counts=result['candidate_counts'] if stage=='old' else None)
                        if stage=='new' and any(j['id']<=cutoff for j in selected):
                            break
                        selected=[j for j in selected if (j['id']<=cutoff)==(stage=='old')]
                        # Engine owns pacing and randomized platform rounds. Keep compatible
                        # attachments in one batch so a cooling platform yields to another.
                        groups={}
                        for job in selected:
                            p=job['platform']
                            if p in blocked: continue
                            if max_per_platform is not None and consumed[p]>=max_per_platform: continue
                            consumed[p]+=1
                            key=config.engine.platforms[p].attachment_name
                            groups.setdefault(key,[]).append(job['id'])
                        batches=list(groups.values())
                        random.shuffle(batches)
                        for ids in batches:
                            if deferred := hold('daily'):
                                result.update(deferred)
                                return result
                            result['selected'].extend(ids)
                            token=json.loads(preview(store,config,job_ids=ids,daily=True))['token']
                            generation = generation_summary(store.get_batch(token)['greetings'])
                            counts = Counter(result.get('greeting_generation', {}))
                            counts.update(generation)
                            result['greeting_generation'] = dict(counts)
                            platforms={j.platform for j in store.batch_jobs(token)}
                            result['current_step']='delivery:'+','.join(sorted(platforms))
                            for p in platforms:
                                result.setdefault('batches',{}).setdefault(p,[]).append(token)
                            save(home,result)
                            try:
                                text=confirm_fn(store,config,token,daily=True)
                                if isinstance(text,str) and DETAIL_MARK in text:
                                    result.setdefault('delivery_details',[]).extend(line for line in text.split(DETAIL_MARK,1)[1].strip().splitlines() if 'http://' not in line and 'https://' not in line)
                            except Exception as e:
                                details=report_error(e,platform=','.join(sorted(platforms)),stage='delivery',config=config)
                                result.update(details)
                                code=getattr(e,'code','')
                                result.setdefault('errors',[]).append(dict(platform=','.join(sorted(platforms)),error=code or type(e).__name__))
                                for p in platforms: result['delivery'][p]['execution_error']=type(e).__name__
                                blocked.update(PLATFORMS if code in {'space_unavailable','task_space'} else platforms)
                            for p in platforms:
                                stats=result['delivery'][p]
                                records=[r for r in store.records() if r.job in store.execution_jobs(token) and r.job.platform==p]
                                for r in records:
                                    identity=store.identity(r.job)
                                    result.setdefault('jobs',[]).append(dict(id=identity['id'],source_platform=p,company=identity['company']))
                                    detail = store.get_job(identity['id']) or {}
                                    result.setdefault('delivery_items',[]).append({**{k:detail.get(k,'') for k in ('company','title','salary','city')},
                                        'platform':p,'job_id':identity['id'],'status':r.status.value,'reason':r.reason.value})
                                    if r.reason.value:
                                        by_status=stats.setdefault('reasons_by_status',{}).setdefault(r.status.value,{})
                                        by_status[r.reason.value]=by_status.get(r.reason.value,0)+1
                                    if r.status in {Status.SENT,Status.FAILED,Status.UNVERIFIED}:
                                        result.setdefault('history',[]).append(dict(id=len(result.get('history',[]))+1,job_id=identity['id'],
                                            source_platform=p,company=identity['company'],action='sent' if r.status==Status.SENT else 'error',
                                            detail=r.reason.value,created_at=datetime.now().astimezone().isoformat()))
                                counts=Counter(r.status.value for r in records)
                                for k in ('sent','failed','unverified','skipped','pending'):
                                    stats[k]+=counts[k]
                                if spec_for(p).supports_application_receipt:
                                    stats['applications_confirmed']=stats.get('applications_confirmed',0)+sum(
                                        r.status==Status.SENT or r.reason==Reason.APPLICATION_CONFIRMED for r in records)
                                reasons=Counter(stats['reasons'])
                                reasons.update(r.reason.value for r in records if r.reason.value)
                                stats['reasons']=dict(reasons)
                                hard_stop=any(r.reason.value in {'risk_control','login_required','task_space'} for r in records)
                                unknown=[r for r in records if r.status==Status.UNVERIFIED and r.reason.value not in {'risk_control','login_required','task_space'}]
                                if unknown and not hard_stop and not config.engine.stop_on_unverified:
                                    # Skip-only policy: the unknown job is never retried, the platform keeps going.
                                    stats['unverified_skipped']=stats.get('unverified_skipped',0)+len(unknown)
                                elif hard_stop or unknown:
                                    blocked.add(p)
                                    hard=any(r.reason.value in {'risk_control','login_required','task_space'} for r in records)
                                    if not hard:
                                        cooling.setdefault(p,'unverified_cooldown')
                                        stats['stop_reason']='unverified_cooldown'
                                    msg=(f'{p} 已停止；风控、登录或空间错误，请检查原站。' if hard else
                                         f'{p} 有 1 条结果不明，暂停 {config.daily.unverified_cooldown_minutes} 分钟后自动续投其余岗位；'
                                         f'当天累计 {config.daily.unverified_daily_stop} 条不明则停到明天。')
                                    try:
                                        result['notifications'][p+'_alert']=send_summary(store,result,config,'job-agent 投递告警',msg+'不会自动重投未确认岗位。')
                                    except Exception as e:
                                        report_error(e,stage='notification',config=config,alert=False)
                                        result['notifications'][p+'_alert_error']=type(e).__name__
                                if any(r.reason.value=='task_space' for r in records):
                                    blocked.update(PLATFORMS)
                                save(home,result)
                    summarize(result,config)
                if config.followup.enabled:
                    from job_agent.service.followup import run as run_followups, notify_report
                    result['current_step']='followup'
                    if config.followup.auto and not config.daily.deliver_enabled:
                        result['followup']={'mode':'disabled','items':[], 'reason':'delivery_disabled'}
                    else:
                        followups=run_followups(store,config,blocked=blocked)
                        result['notifications']['followup']=notify_report(store,config,followups)
                        result['followup']={**followups,'items':[{k:v for k,v in item.items() if k!='message'} for item in followups['items']]}
                summarize(result,config)
                retry_errors=[s.get('error') for s in result['collection'].get('platforms',{}).values()
                              if s.get('error') and s.get('error') not in NON_BLOCKING_COLLECTION_ERRORS
                              and s.get('error') not in {'risk_control','risk_control_paused','login_required','login_paused'}]
                if result['collection'].get('busy') or result['collection'].get('stopped'):
                    retry_errors.append('collection_busy_or_stopped')
                retry_pending=any((r.status==Status.FAILED and r.reason==Reason.PRECHECK_ERROR) or (r.status==Status.SKIPPED and r.reason==Reason.NOT_STARTED) or r.status==Status.PENDING and r.reason.value not in
                    {'daily_limit','risk_control','disabled','resume_flow_unverified','resume_attachment_required'}
                    for r in store.records() if store.identity(r.job).get('id') in result.get('selected',[]))
                if config.daily.deliver_enabled and any(v=='unverified_cooldown' for v in cooling.values()):
                    # Not finished: the cooled platform resumes on the next periodic trigger.
                    retry_pending=True
                result['reason_code']=(retry_errors[0] if retry_errors else 'delivery_pending' if retry_pending else
                                       result['errors'][0]['error'] if result.get('errors') else 'ok')
                result['status']='failed' if result.get('errors') or retry_errors else 'deferred' if retry_pending else 'completed'
                result['current_step']='complete' if result['status']=='completed' else result['current_step']
                from job_agent.service.delivery_report import today_success_counts, next_resume
                from job_agent.store.reporting import hourly_times
                result['today_success']=today_success_counts(store)
                hourly=hourly_times(store.connection,result.get('delivery',{}),datetime.now().astimezone())
                result['next_resume_at']=next_resume(config,result.get('delivery',{}),datetime.now().astimezone(),hourly_times=hourly)
                if config.daily.deliver_enabled and config.notify.delivery_summary:
                    try:
                        title,text=delivery_message(result)
                        result['notifications']['delivery']=send_summary(store,result,config,title,text,topic='delivery')
                    except Exception as e:
                        report_error(e,stage='notification',config=config,alert=False)
                        result['notifications']['delivery_error']=type(e).__name__
                return result
            except BaseException as e:
                result['status']='interrupted' if isinstance(e,(KeyboardInterrupt,SystemExit)) else 'failed'
                result['error']=type(e).__name__
                result['reason_code']=getattr(e,'code',type(e).__name__)
                raise
            finally:
                summarize(result,config)
                result['finished_at']=datetime.now().astimezone().isoformat()
                save(home,result)


def daily_once(*, config=None, max_per_platform=None, collect_fn=None, confirm_fn=None, collection_page_limit=None, scheduled=False):
    """Own state under the daily lock, including config validation and store startup."""
    from job_agent.service.automation import hold
    if deferred := hold('daily'):
        return deferred
    with run_scope('daily') as run_id:
        home=job_agent_home()
        try:
            with run_lock(home):
                result=dict(started_at=datetime.now().astimezone().isoformat(),status='running',
                            run_id=run_id,reason_code='running',current_step='startup',
                            collection={},delivery={},notifications={})
                started=False
                try:
                    previous=read_run('daily', home) or {}
                    if previous.get('last_failure'):
                        result['last_failure']=previous['last_failure']
                    if previous.get('status') in {'failed','interrupted'}:
                        result['last_failure']={k:previous[k] for k in ('run_id','reason_code','error_type','error_message','platform','stage','frames','finished_at') if k in previous}
                    if previous.get('status')=='completed' and same_day(previous):
                        return {**previous,'already_completed':True}
                    save(home,result)
                    started=True
                    output=_daily_once(result=result,previous=previous,config=config,
                        max_per_platform=max_per_platform,collect_fn=collect_fn,confirm_fn=confirm_fn,
                        collection_page_limit=collection_page_limit,scheduled=scheduled)
                    if output.get('status')=='failed':
                        result['last_failure']={k:result[k] for k in ('run_id','reason_code','error_type','error_message','platform','stage','frames') if k in result}
                        result['last_failure']['finished_at']=datetime.now().astimezone().isoformat()
                    if output.get('disabled'):
                        result.update(status='deferred',reason_code='daily_disabled')
                        output.update(status='deferred',reason_code='daily_disabled',run_id=run_id)
                    return output
                except BaseException as error:
                    step=result.get('current_step','startup')
                    platform=step.split(':',1)[1] if ':' in step else 'all'
                    details=report_error(error,platform=platform,stage=step.split(':',1)[0],config=config)
                    result.update(details,status='interrupted' if isinstance(error,(KeyboardInterrupt,SystemExit)) else 'failed',error=details['error_type'])
                    result['last_failure']={**details,'finished_at':datetime.now().astimezone().isoformat()}
                    raise
                finally:
                    if started or result['status'] in {'failed','interrupted'}:
                        result['finished_at']=datetime.now().astimezone().isoformat()
                        save(home,result)
        except DailyAlreadyRunning:
            raise
        except Exception as error:
            report_error(error,stage='daily')
            raise
