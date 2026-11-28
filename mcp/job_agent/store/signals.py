"""Reply classification and invitation persistence; all SQL for #50 lives here."""
from job_agent.store.database import now


def pending_replies(store, limit=20, *, reply_ids=None, platform=None):
    clauses=["direction='inbound'", "classified_at IS NULL"]
    args=[]
    if reply_ids is None:
        clauses.append("classification_source='pending'")
    if platform is not None:
        clauses.append('platform=?'); args.append(platform)
    rows=store.connection.execute('SELECT * FROM replies WHERE '+' AND '.join(clauses)+' ORDER BY id',args).fetchall()
    allowed=None if reply_ids is None else set(reply_ids)
    return [dict(r) for r in rows if allowed is None or r['id'] in allowed][:limit]


def recover_classifications(store):
    # The monitor's outer process lock ensures no other live classifier owns these.
    # A crash after dispatch has unknown cost/outcome: never call the provider again.
    store.connection.execute("""UPDATE replies SET classification='other',confidence=0,
        classification_source='fallback',classification_error='worker_interrupted',classified_at=?
        WHERE classification_source='processing' AND classified_at IS NULL""",(now(),))


def claim_classification(store, reply_id):
    return store.connection.execute("""UPDATE replies SET classification_source='processing'
        WHERE id=? AND direction='inbound' AND classified_at IS NULL
        AND classification_source!='processing'""",(reply_id,)).rowcount==1


def save_classification(store, reply_id, result):
    with store.transaction():
        stamp=now()
        changed=store.connection.execute('''UPDATE replies SET classification=?,confidence=?,
            classification_source=?,classification_model=?,classified_at=?,classification_error=?
            WHERE id=? AND direction='inbound' AND classified_at IS NULL''',
            (result['category'],result['confidence'],result['source'],result.get('model',''),
             stamp,':'.join(filter(None,(result.get('error',''),result.get('failure_kind','')))),reply_id)).rowcount
        if changed and result['category']=='interview':
            store.connection.execute('INSERT OR IGNORE INTO interview_invitations(reply_id,time_text,form,created_at) VALUES(?,?,?,?)',
                                     (reply_id,result['time_text'],result['form'],stamp))
        return bool(changed)


def invitations(store):
    return [dict(r) for r in store.connection.execute('''SELECT i.*,r.job_id,r.body,r.confidence,
        r.occurred_at,r.chat_url,j.platform,j.company,j.title,j.url FROM interview_invitations i
        JOIN replies r ON r.id=i.reply_id JOIN jobs j ON j.id=r.job_id
        WHERE j.deleted_at IS NULL ORDER BY r.occurred_at,i.reply_id''')]


def cohorts(store):
    """First successful delivery per job; no guessing versions for missing text.

    Replies before that delivery and synthetic application-status rows do not
    measure greeting response. A job belongs to one cohort even after a re-send.
    """
    from job_agent.platforms.greeting import greeting_version
    from datetime import datetime, timezone
    def stamp(value):
        try:
            dt=datetime.fromisoformat(value.replace('Z','+00:00'))
            return dt.replace(tzinfo=timezone.utc).timestamp() if dt.tzinfo is None else dt.timestamp()
        except (ValueError,TypeError,AttributeError,OverflowError): return None
    first={}
    jobs={j['id']:j for j in store.list_jobs()}
    for d in store.list_deliveries():
        when=stamp(d['created_at'])
        if d['status']!='sent' or d['job_id'] not in jobs or when is None: continue
        prior=first.get(d['job_id'])
        if prior is None or (when,d['id'])<(prior[0],prior[1]['id']): first[d['job_id']]=(when,d)
    replies={}
    for r in store.list_replies(): replies.setdefault(r['job_id'],[]).append(r)
    result=[]
    for job_id,(when,d) in first.items():
        qualifying=[r for r in replies.get(job_id,[]) if r['direction']=='inbound'
                    and r['id_origin']=='platform' and not r['is_excerpt']
                    and (stamp(r['occurred_at']) is not None and stamp(r['occurred_at'])>=when)]
        result.append(dict(job_id=job_id,platform=jobs[job_id]['platform'],
            greeting_version=greeting_version(d['greeting']) if d['greeting'] else 'unknown',
            applied=True,replied=bool(qualifying),interview_invited=any(r['classification']=='interview' for r in qualifying)))
    return result


def wilson(successes,total):
    if total==0: return None
    z=1.959963984540054
    p=successes/total
    denom=1+z*z/total
    center=(p+z*z/(2*total))/denom
    half=z*((p*(1-p)/total+z*z/(4*total*total))**.5)/denom
    return [max(0,center-half),min(1,center+half)]


def summarize(items,min_samples):
    applied=len(items)
    replied=sum(i['replied'] for i in items)
    invited=sum(i['interview_invited'] for i in items)
    return dict(applied=applied,replied=replied,interview_invited=invited,
        reply_rate=replied/applied if applied else None,
        interview_rate=invited/applied if applied else None,
        reply_to_interview_rate=invited/replied if replied else None,
        reply_ci95=wilson(replied,applied),interview_ci95=wilson(invited,applied),
        assessment='样本不足' if applied<min_samples else '观察数据，不能归因于招呼语')


def greeting_statistics(store,*,min_samples=20):
    rows=cohorts(store)
    groups={}; platform_groups={}
    for r in rows:
        groups.setdefault(r['greeting_version'],[]).append(r)
        platform_groups.setdefault((r['platform'],r['greeting_version']),[]).append(r)
    return dict(total=summarize(rows,min_samples),
        groups=[dict(greeting_version=k,**summarize(v,min_samples)) for k,v in sorted(groups.items())],
        platform_groups=[dict(platform=p,greeting_version=k,**summarize(v,min_samples)) for (p,k),v in sorted(platform_groups.items())],
        min_samples=min_samples,interval_method='Wilson 95%',
        advice='样本不足，继续积累；暂不判断招呼语版本优劣。' if not groups or any(len(v)<min_samples for v in groups.values()) else '结合平台、岗位和观察时长比较区间；当前数据不能证明招呼语的因果效果。',
        semantics='每个岗位归于首次成功投递的正文哈希（复用 #59）；缺失正文为 unknown。只计投递后完整原站 HR 回复；邀约不代表已接受。全时段观察，未校正平台、岗位、观察时长或重复投递。')
