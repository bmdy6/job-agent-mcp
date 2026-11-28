"""Owned paid scoring. Preview is read-only; only confirmed workers call providers."""
from job_agent.observability import observed, report_error
import hashlib
from http.client import HTTPSConnection
import json
import os
from pathlib import Path
from urllib.parse import urlsplit
import uuid

from job_agent.config import load_config
from job_agent.store import Store
from job_agent.store import scoring as sql
import job_agent.user_data as user_data

from job_agent.config.llm import provider_settings, resolve_model
from job_agent.service.prompt_files import render_prompt



def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True).encode()).hexdigest()


def resolve_key(settings):
    try:
        if settings.key_source=='env':
            value=os.environ.get(settings.api_key_env)
        else:
            vault=json.loads(Path(settings.vault_path).expanduser().read_text(encoding='utf-8'))
            value=vault[settings.vault_group][settings.vault_service][settings.vault_key]
            if isinstance(value,dict): value=value.get('value')
        if not isinstance(value,str) or not value.strip(): raise ValueError('empty credential')
        return value.strip()
    except (OSError,KeyError,TypeError,ValueError):
        raise ValueError('Scoring credential unavailable; check configured key source') from None


def materials(settings):
    try:
        # Scoring uses an explicit UTF-8 main resume; no implicit newest-file selection.
        resume=Path(settings.resume_path).expanduser().read_text(encoding='utf-8')
        if not resume.strip(): raise ValueError('empty resume')
        experiences=[user_data.experience_get(i['id']) for i in user_data.experience_list()]
        result={'resume':resume,'experiences':experiences}
        if len(json.dumps(result,ensure_ascii=False))>settings.max_input_chars:
            raise ValueError('oversize materials')
        return result
    except (OSError,ValueError):
        raise ValueError('Scoring materials missing, empty or too large; configure scoring.resume_path and experience/') from None


def build_request(job, material, settings):
    data={'candidate':material,'job':{k:job.get(k,'') for k in ('title','company','city','salary','jd')}}
    content=json.dumps(data,ensure_ascii=False)
    if len(content)>settings.max_input_chars:
        raise ValueError('Scoring input too large; no automatic truncation')
    return {'model':settings.model,'service_tier':settings.service_tier,
            'max_completion_tokens':settings.max_completion_tokens,'response_format':{'type':'json_object'},
            'messages':[{'role':'system','content':render_prompt('scoring')},{'role':'user','content':content}]}


def parse_result(payload):
    try:
        choice=payload['choices'][0]
        if choice.get('finish_reason')!='stop' or choice['message'].get('refusal'):
            raise ValueError('incomplete or refused')
        result=json.loads(choice['message']['content'])
        if (not isinstance(result,dict) or set(result)!={'score','reason'}
                or type(result['score']) is not int or not 0<=result['score']<=100
                or not isinstance(result['reason'],str) or not result['reason'].strip()
                or len(result['reason'])>6000):
            raise ValueError('invalid scoring result')
        return {'score':result['score'],'reason':result['reason'].strip()}
    except (AttributeError,KeyError,IndexError,TypeError,ValueError):
        raise ValueError('Invalid or incomplete model scoring result') from None


class ProviderRejected(Exception):
    """Known HTTP rejection, with no provider body or credential in the error."""


def request_completion(payload,settings,key):
    parsed=urlsplit(settings.base_url)
    connection=HTTPSConnection(parsed.hostname,parsed.port or 443,timeout=settings.connect_timeout_seconds)
    try:
        connection.connect()
        connection.sock.settimeout(settings.timeout_seconds)
        connection.request('POST',parsed.path.rstrip('/')+'/chat/completions',
            body=json.dumps(payload,ensure_ascii=False).encode(),
            headers={'Authorization':'Bearer '+key,'Content-Type':'application/json'})
        response=connection.getresponse()
        headers={k.lower():v for k,v in response.getheaders() if k.lower().startswith('x-ratelimit-')}
        if response.status!=200:
            raise ProviderRejected('HTTP_'+str(response.status))
        raw=response.read(1024*1024+1)
        if len(raw)>1024*1024: raise ValueError('Provider response too large')
        return json.loads(raw),headers
    finally:
        connection.close()


def validate_ids(ids,settings):
    if (not isinstance(ids,list) or not 1<=len(ids)<=settings.max_jobs
            or any(not isinstance(i,str) or not i.isascii() or not i.isdigit() or int(i)<1 for i in ids)
            or len({int(i) for i in ids})!=len(ids)):
        raise ValueError('Select 1–'+str(settings.max_jobs)+' distinct owned job IDs')
    return [int(i) for i in ids]


def selection(store,ids):
    claimed=sql.claimed(store); jobs=[]; skipped=[]
    for ident in ids:
        row=store.get_job(ident)
        if row is None: raise ValueError('Own job not found: '+str(ident))
        reason=sql.skip_reason(row,claimed)
        if reason: skipped.append({'job_id':str(ident),'reason':reason})
        else: jobs.append(row)
    return jobs,skipped


def pending_count(store):
    claimed=sql.claimed(store)
    return sum(sql.skip_reason(j,claimed) is None for j in store.list_jobs())


def preview_scoring(job_ids):
    from job_agent.service.read_tools import snapshot
    settings=provider_settings(load_config(),'scoring')
    ids=validate_ids(job_ids,settings)
    with snapshot() as store:
        jobs,skipped=selection(store,ids)
        pending=pending_count(store)
    return {'backend':'own','estimated_requests':len(jobs),'pending_count':pending,
            'job_ids':[str(j['id']) for j in jobs],'skipped':skipped,
            'model':settings.model,'service_tier':settings.service_tier,
            'max_completion_tokens':settings.max_completion_tokens,
            'instruction':'每个岗位最多一次付费请求，无自动重试。先展示预计请求数，用户授权后 start_scoring(confirm=true)。'}


def start_scoring(job_ids,confirm=False):
    if confirm is not True: raise ValueError('启动评分需要用户授权，先预览请求数，授权后传入 confirm=true。')
    config=load_config()
    settings=provider_settings(config,'scoring'); ids=validate_ids(job_ids,settings)
    material=materials(settings); resolve_key(settings)  # fail before reserving/spawning
    from job_agent.service import tasks
    tasks.status('scoring')  # Confirmed write path reconciles a dead worker, never replays requests.
    run_id=uuid.uuid4().hex
    with Store() as store:
        with store.transaction():
            jobs,skipped=selection(store,ids)
            if not jobs: raise ValueError('No eligible jobs; preview again')
            for job in jobs: build_request(job,material,settings)
            sql.reserve(store,run_id,jobs,material,settings,digest(settings.model_dump()))
        result=sql.public_run(store,run_id)
    try:
        task=tasks.start('scoring',run_id=run_id)
    except Exception:
        abandon_run(run_id,'worker_start_failed')
        raise RuntimeError('Scoring worker launch failed; inspect scoring_runs before any retry') from None
    return {'backend':'own','estimated_requests':len(jobs),'skipped':skipped,'run':result,'task':task}


def remaining_tokens(headers):
    try: return max(0,int(headers['x-ratelimit-remaining-tokens']))
    except (KeyError,TypeError,ValueError): return None


def request_budget(payload):
    # UTF-8 byte count upper bound, deliberately conservative; not a tokenizer estimate.
    return len(json.dumps(payload['messages'],ensure_ascii=False).encode())+payload['max_completion_tokens']+1024


@observed('scoring', id_argument='run_id')
def execute_run(run_id,should_stop=lambda:False):
    config=load_config()
    settings=provider_settings(config,'scoring')
    from job_agent.service import llm_usage
    with Store() as store:
        run=sql.run(store,run_id)
        if not sql.begin_run(store,run_id): return sql.public_run(store,run_id)
        try:
            if run['config_hash']!=digest(settings.model_dump()):
                raise ValueError('scoring_config_changed')
            key=resolve_key(settings); material=json.loads(run['materials_json'])
            remaining=None; attempted=False
            for item in sql.items(store,run_id):
                if item['status']!='pending': continue
                job=json.loads(item['job_json']); ident=item['job_id']
                payload=build_request(job,material,settings)
                error=None
                if should_stop(): error='stopped'
                elif attempted and ((remaining is None and settings.require_ratelimit_header)
                        or (remaining is not None and remaining<request_budget(payload))): error='rate_limit_budget'
                current=store.get_job(ident)
                if (not current or current['deleted_at'] or sql.imported_score(current)
                        or any(current[k]!=job[k] for k in ('title','company','city','salary','jd'))): error='job_changed'
                if error:
                    sql.update_item(store,run_id,ident,'blocked',error=error); continue
                # Atomic claim transition prevents a duplicate worker from paying twice.
                if not sql.begin_item(store,run_id,ident): continue
                call_id,budget_reason=llm_usage.reserve(config,'scoring',run_id)
                if not call_id:
                    sql.update_item(store,run_id,ident,'blocked',error=budget_reason); continue
                attempted=True
                try:
                    response,headers=request_completion(payload,settings,key)
                except ProviderRejected as exc:
                    value=llm_usage.finish(call_id,None,resolve_model(config,'scoring'),'provider_error')
                    report_error(exc,platform=job['platform'],stage='scoring')
                    sql.update_item(store,run_id,ident,'failed',error=str(exc),usage=value)
                    remaining=0; continue  # includes 429; no retry
                except Exception as exc:
                    value=llm_usage.finish(call_id,None,resolve_model(config,'scoring'),'provider_unknown')
                    report_error(exc,platform=job['platform'],stage='scoring')
                    sql.update_item(store,run_id,ident,'unverified',error='provider_outcome_unknown',usage=value)
                    remaining=0; continue
                usage=llm_usage.finish(call_id,response.get('usage') if isinstance(response,dict) else None,resolve_model(config,'scoring'))
                usage['run_id']=run_id
                usage['call_id']=call_id
                remaining=remaining_tokens(headers)
                try:
                    result=parse_result(response)
                except ValueError as exc:
                    report_error(exc,platform=job['platform'],stage='scoring_parse')
                    sql.update_item(store,run_id,ident,'failed',error='invalid_model_result',remaining=remaining,usage=usage); continue
                tier=response.get('service_tier') or settings.service_tier
                if not isinstance(tier,str) or tier not in {'priority','auto','default','flex','scale','fast'}: tier='unknown'
                sql.save_score(store,run_id,ident,result,settings,digest(payload),remaining,usage,tier,job)
        finally:
            # A worker crash after dispatch is uncertain and never automatically resubmitted.
            sql.settle(store,run_id)
        return sql.public_run(store,run_id)


def scoring_runs():
    from job_agent.service.read_tools import snapshot
    with snapshot() as store:
        ids=sql.recent_ids(store)
        return {'backend':'own','runs':[sql.public_run(store,i) for i in ids],'pending_count':pending_count(store)}


def score_trace(job_id):
    from job_agent.service.read_tools import snapshot
    with snapshot() as store:
        return sql.trace(store,job_id)


def setup_checks():
    settings=provider_settings(load_config(),'scoring'); checks={}
    for name,check in [('scoring_credentials',lambda:resolve_key(settings)),('resume',lambda:materials(settings))]:
        try:
            check(); checks[name]={'ok':True}
        except ValueError:
            checks[name]={'ok':False,'reason':'Configure scoring key source or scoring.resume_path'}
    return checks


def abandon_run(run_id,error='worker_interrupted'):
    with Store() as store:
        sql.settle(store,run_id,error)
