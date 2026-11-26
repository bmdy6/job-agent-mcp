"""One daily round: collection, collection summary, immutable delivery batches, summary.

Only this explicit daily opt-in can confirm a batch without an interactive preview.
The run lock covers selection and effects. Reports survive interrupted processes.
"""
from collections import Counter
import random
from contextlib import contextmanager
from datetime import datetime
import fcntl
import json
import os
from config import job_agent_home, load_config
from engine.models import PLATFORMS, Status, Reason
from engine.resume_contract import resume_block_reason
from store import SQLiteDeliveryStore
from service.deliver import preview, confirm, DETAIL_MARK
from notify import TelegramAdapter
from delivery_summary import notification


@contextmanager
def run_lock(home):
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd=os.open(home/'daily.lock',os.O_CREAT|os.O_RDWR,0o600)
    try:
        try:
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Daily round already running') from None
        yield
    finally:
        os.close(fd)


def send(config, title, text):
    if not config.notify.enabled:
        return []
    sent=[]
    for name, channel in config.notify.channels.items():
        if channel.enabled and channel.type=='telegram':
            TelegramAdapter(channel).send(title,text)
            sent.append(name)
    return sent


def save(home, result):
    path=home/'daily-last-run.json'
    temp=home/'daily-last-run.tmp'
    fd=os.open(temp,os.O_CREAT|os.O_WRONLY|os.O_TRUNC,0o600)
    with os.fdopen(fd,'w') as f:
        json.dump(result,f,ensure_ascii=False,indent=2)
    os.replace(temp,path)


NON_BLOCKING_COLLECTION_ERRORS = frozenset({'collection_timeout'})


def delivery_message(report):
    stats=report['delivery']
    success=sum(s['sent'] for s in stats.values())
    failed=sum(s['failed']+s['unverified'] for s in stats.values())
    pending=sum(s['pending'] for s in stats.values())
    skipped=sum(s['skipped'] for s in stats.values())
    # Reuse delivery_summary formatting; explicit additional lines preserve skip reasons.
    task=dict(id=report['started_at'],mode='deliver',status='failed' if report.get('errors') else 'completed',
              created_at=report['started_at'],updated_at=datetime.now().astimezone().isoformat(),
              metrics=dict(send_success=success,send_failed=failed,send_deferred=pending+skipped,
                           send_requested=success+failed+pending+skipped))
    title,text=notification(task,report.get('history',[]),report.get('jobs',[]))
    lines=[text, '统计口径：BOSS 的成功是沟通回执，不等于简历送达；猎聘只有正式简历回执才可计成功；51job 成功需配置附件及其回执。']
    hold_labels = {'risk_control_paused':'原站风控暂停，需本人在原站处理',
                   'resume_flow_unverified':'正式简历流程尚未验收，保留待处理',
                   'resume_attachment_required':'缺少明确配置附件，保留待处理',
                   'collection_error_or_previous_batch_stop':'采集或此前批次异常，本轮暂停'}
    for p,s in stats.items():
        lines.append(f"{p}：成功 {s['sent']} / 失败 {s['failed']} / 不明 {s['unverified']} / 跳过 {s['skipped']} / 待续 {s['pending']}")
        lines.extend(f'  {reason}：{count}' for reason,count in s['reasons'].items())
        for hold in dict.fromkeys(k for k in (s.get('stop_reason'), s.get('resume_block_reason')) if k):
            lines.append(f'  暂停原因：{hold_labels.get(hold,hold)}')
        if p=='zhilian' and 'applications_confirmed' in s:
            lines.append(f"  原站已确认申请：{s['applications_confirmed']}（与完整消息回执分开）")
        planning_labels={'known_cooldown':'本地记录已命中冷静期',
                         'attempted_pending':'待处理快照已有尝试标记'}
        for reason,count in report.get('planning_exclusions',{}).get(p,{}).items():
            lines.append(f"  规划排除（不占本轮选择）：{planning_labels.get(reason,reason)} {count}")
        if s.get('execution_error'):
            lines.append(f"  执行中断：{s['execution_error']}，已停止本轮后续投递")
    zero_labels={'delivery_disabled':'本轮关闭投递','platform_disabled':'本轮关闭该平台',
                 'no_eligible_candidates':'没有符合条件的候选（含冷却期排除）',
                 'no_confirmed_receipt':'候选未取得成功回执，见跳过、待续或不明原因',
                 'risk_control_paused':'原站风控暂停','collection_error_or_previous_batch_stop':'采集或此前批次异常'}
    for p,s in report.get('summary',{}).items():
        lines.append(f"{p}：采集 {s['collection_status']} / 已采 {s['collected_pages']} 页 / 可投候选 {s['eligible_candidates']} / 冷却期排除 {s['cooldown_excluded']} / 实际投递 {s['actual_deliveries']}")
        if s['collection_status']=='collection_timeout':
            lines.append(f"  采到第 {s['last_page'] or s['collected_pages']} 页，因超时停止")
        if s['zero_reason']:
            lines.append(f"  0 条投递原因：{zero_labels.get(s['zero_reason'],s['zero_reason'])}")
    if report.get('delivery_details'):
        lines.append('明细：')
        lines.extend(l for l in report['delivery_details'] if not l.startswith('口径：'))
    return title,'\n'.join(lines)


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


def daily_once(*, config=None, max_per_platform=None, collect_fn=None, confirm_fn=None, collection_page_limit=None):
    config=config or load_config()
    home=job_agent_home()
    if not config.daily.enabled:
        return {'disabled':True}
    if config.engine.backend!='own':
        raise ValueError('Daily workflow requires own backend')
    if config.daily.deliver_enabled and (config.engine.dry_run or not config.engine.live_send_enabled):
        raise ValueError('Daily delivery requires explicit live opt-in')
    if config.collect.task_space_name in {config.engine.task_space_name,config.engine.monitor.task_space_name}:
        raise ValueError('Collection, delivery and monitor spaces must be separate')
    if max_per_platform is not None and (type(max_per_platform) is not int or not 1<=max_per_platform<=3):
        raise ValueError('Acceptance cap must be 1..3 jobs/platform')
    if collect_fn is None:
        from collect.runtime import collect_once
        collect_fn=collect_once
    confirm_fn=confirm_fn or confirm
    result=dict(started_at=datetime.now().astimezone().isoformat(),status='running',
                collection={},delivery={},notifications={})
    with run_lock(home), SQLiteDeliveryStore(home/'jobs.sqlite3',dry_run=False) as store:
        cutoff=store.connection.execute('SELECT COALESCE(MAX(id),0) FROM jobs').fetchone()[0]
        result['old_cutoff_id']=cutoff
        save(home,result)
        try:
            if config.daily.collect_enabled:
                result['collection']=collect_fn(**({'page_limit':collection_page_limit} if collection_page_limit is not None else {}))
                lines=[]
                for p,s in result['collection'].get('platforms',{}).items():
                    lines.append(f"{p}：新增 {s.get('new',0)} / 刷新 {s.get('refreshed',0)} / 过滤 {s.get('filtered',0)} / JD {s.get('details',0)} / 页面 {s.get('pages',0)}" +
                                 (f" / 停止：{s['error']}" if s.get('error') else ''))
                try:
                    result['notifications']['collection']=send(config,'job-agent 采集结果','\n'.join(lines) or '本轮没有启用的采集平台')
                except Exception as e:
                    result['notifications']['collection_error']=type(e).__name__
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
            result['candidate_counts']={}
            if config.daily.deliver_enabled:
                result['selected']=[]
                result['planning_exclusions']={}
                consumed=Counter()
                result['delivery']={p:{**dict.fromkeys(('sent','failed','unverified','skipped','pending'),0),'reasons':{}} for p in PLATFORMS}
                for p in blocked:
                    stats=result['delivery'][p]
                    stats['stop_reason']='risk_control_paused' if p in paused else resume_blocks.get(p,'collection_error_or_previous_batch_stop')
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
                        result['selected'].extend(ids)
                        token=json.loads(preview(store,config,job_ids=ids,daily=True))['token']
                        platforms={j.platform for j in store.batch_jobs(token)}
                        for p in platforms:
                            result.setdefault('batches',{}).setdefault(p,[]).append(token)
                        save(home,result)
                        try:
                            text=confirm_fn(store,config,token,daily=True)
                            if isinstance(text,str) and DETAIL_MARK in text:
                                result.setdefault('delivery_details',[]).extend(line for line in text.split(DETAIL_MARK,1)[1].strip().splitlines() if 'http://' not in line and 'https://' not in line)
                        except Exception as e:
                            result.setdefault('errors',[]).append(dict(platform=','.join(sorted(platforms)),error=type(e).__name__))
                            for p in platforms: result['delivery'][p]['execution_error']=type(e).__name__
                            blocked.update(PLATFORMS)
                        for p in platforms:
                            stats=result['delivery'][p]
                            records=[r for r in store.records() if r.job in store.execution_jobs(token) and r.job.platform==p]
                            for r in records:
                                identity=store.identity(r.job)
                                result.setdefault('jobs',[]).append(dict(id=identity['id'],source_platform=p,company=identity['company']))
                                if r.status in {Status.SENT,Status.FAILED,Status.UNVERIFIED}:
                                    result.setdefault('history',[]).append(dict(id=len(result.get('history',[]))+1,job_id=identity['id'],
                                        source_platform=p,company=identity['company'],action='sent' if r.status==Status.SENT else 'error',
                                        detail=r.reason.value,created_at=datetime.now().astimezone().isoformat()))
                            counts=Counter(r.status.value for r in records)
                            for k in ('sent','failed','unverified','skipped','pending'):
                                stats[k]+=counts[k]
                            if p=='zhilian':
                                stats['applications_confirmed']=stats.get('applications_confirmed',0)+sum(
                                    r.status==Status.SENT or r.reason==Reason.APPLICATION_CONFIRMED for r in records)
                            labels={'application_confirmed_greeting_unknown':'原站申请已成功，招呼语回执未确认（不重投）',
                                    'cooldown':f'{config.engine.cooldown.days} 天内已投同一公司同一 HR 同一岗位（无 HR 按公司+岗位）',
                                    'already_applied':'原站显示已投递/已沟通','job_unavailable':'原站明确显示岗位不存在/已下架','daily_limit':'当日额度已用完'}
                            reasons=Counter(stats['reasons'])
                            reasons.update(labels.get(r.reason.value,r.reason.value) for r in records if r.reason.value)
                            stats['reasons']=dict(reasons)
                            if any(r.status==Status.UNVERIFIED or r.reason.value in {'risk_control','login_required','task_space'} for r in records):
                                blocked.add(p)
                                try:
                                    result['notifications'][p+'_alert']=send(config,'job-agent 投递告警',f'{p} 已停止；有未确认回执、风控、登录或空间错误，请检查原站。不会自动重投未确认岗位。')
                                except Exception as e:
                                    result['notifications'][p+'_alert_error']=type(e).__name__
                            if any(r.reason.value=='task_space' for r in records):
                                blocked.update(PLATFORMS)
                            save(home,result)
                summarize(result,config)
                if config.notify.delivery_summary:
                    try:
                        title,text=delivery_message(result)
                        result['notifications']['delivery']=send(config,title,text)
                    except Exception as e:
                        result['notifications']['delivery_error']=type(e).__name__
            summarize(result,config)
            result['status']='failed' if result.get('errors') else 'completed'
            return result
        except BaseException as e:
            result['status']='interrupted' if isinstance(e,(KeyboardInterrupt,SystemExit)) else 'failed'
            result['error']=type(e).__name__
            raise
        finally:
            summarize(result,config)
            result['finished_at']=datetime.now().astimezone().isoformat()
            save(home,result)
