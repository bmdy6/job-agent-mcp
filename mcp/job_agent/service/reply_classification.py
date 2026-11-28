"""Rules first, bounded optional model fallback using the scoring provider/config.

Never sends or accepts invitations. Model errors are terminal observable fallbacks,
not retries. Historical replay supplies explicit reply IDs to an isolated store.
"""
import json
import re
from pathlib import Path
from job_agent.service.scoring import request_completion, resolve_key, ProviderRejected
from job_agent.store import signals as sql

CATEGORIES={'interview','resume_request','rejection','question','other'}
FORMS={'online','onsite','phone','unknown'}

from job_agent.config.llm import provider_settings, resolve_model
from job_agent.service.prompt_files import render_prompt
from job_agent.service import llm_usage


def evidence(body):
    form='online' if re.search(r'线上|视频|腾讯会议|飞书会议|远程面试',body) else 'onsite' if re.search(r'线下|现场|来公司|到公司',body) else 'phone' if re.search(r'电话|通话',body) else 'unknown'
    # Preserve source wording only; do not fabricate a timestamp from relative text.
    times=re.findall(r'(?:\d{4}年)?\d{1,2}月\d{1,2}[日号]|\d{4}-\d{1,2}-\d{1,2}|(?:本周|下周|周|星期)[一二三四五六日天]|今天|明天|后天|今晚|上午|下午|晚上|中午|\d{1,2}[:：]\d{2}|[零一二两三四五六七八九十\d]{1,3}[点时](?:半|\d{1,2}分)?',body)
    return ' '.join(times),form


def result(category,confidence,body,*,source='rule',model='',error=''):
    time_text,form=evidence(body)
    return dict(category=category,confidence=confidence,source=source,model=model,error=error,
                time_text=time_text if category=='interview' else '',form=form if category=='interview' else 'unknown')


def rule(body):
    if re.fullmatch(r'\s*(?:您好|你好)(?:啊)?[，,！!。\s]*(?:(?:可以)?聊一聊[~～！!。\s]*)?',body):
        return result('other',.99,body)
    if re.search(r'不[太]?匹配|不太合适|不是很合适|不合适|不符合|暂不考虑|暂不安排面试|不[再]?安排面试|取消面试|婉拒|暂时不招|已招满|祝[您你].*(?:工作|前程)|经验不足|(?:简历|经历|经验).{0,30}(?:招聘要求|岗位要求).{0,8}(?:出入|差距)',body):
        return result('rejection',.95,body)
    if (re.search(r'面试|面谈',body) and re.search(r'邀请|邀约|方便|安排|参加|有空|过来|来公司|到公司|可以.*[吗么？?]',body)
            and not re.search(r'面试流程|负责面试|暂不|不安排|无需面试|合适.{0,8}(?:面试|约面)|(?:评估|筛选|审核|通过).{0,8}(?:后|再)',body)):
        return result('interview',.95,body)
    if re.search(r'(?:发|提供|发送|投递|上传|要).{0,12}简历|简历.{0,8}(?:发|提供|给)',body):
        return result('resume_request',.95,body)
    return None


def parse_result(response,body,settings):
    try:
        choice=response['choices'][0]
        if choice.get('finish_reason')!='stop' or choice['message'].get('refusal'): raise ValueError()
        value=json.loads(choice['message']['content'])
        if not isinstance(value,dict) or set(value)!={'category','confidence','time_text','form'}: raise ValueError()
        if value['category'] not in CATEGORIES or value['form'] not in FORMS: raise ValueError()
        if type(value['confidence']) not in (int,float) or not 0<=value['confidence']<=1: raise ValueError()
        if not isinstance(value['time_text'],str) or len(value['time_text'])>200 or (value['time_text'] and value['time_text'] not in body): raise ValueError()
        _,observed_form=evidence(body)
        if value['form']!='unknown' and value['form']!=observed_form: raise ValueError()
        if value['category']=='interview' and value['confidence']<.8: raise ValueError()
        out=result(value['category'],value['confidence'],body,source='model',model=settings.model)
        if value['category']=='interview': out.update(time_text=value['time_text'],form=value['form'])
        elif value['time_text'] or value['form']!='unknown': raise ValueError()
        return out
    except (KeyError,IndexError,TypeError,ValueError,AttributeError):
        raise ValueError('Invalid classification result') from None


def classify(body,settings,*,use_model=False,config=None,run_id=None):
    found=rule(body)
    if found: return found
    if not use_model:
        return result('question' if re.search(r'[?？]|请问|是否|几年|什么时候|[吗么呢][？?！!。]?$',body) else 'other',.7,body)
    if not body.strip() or len(body)>settings.max_input_chars:
        return result('other',0,body,source='fallback',error='invalid_input')
    if config is None:
        from job_agent.config import load_config
        config=load_config()
    settings=provider_settings(config,'reply_classification')
    import uuid
    run_id=run_id or uuid.uuid4().hex
    payload={'model':settings.model,'service_tier':settings.service_tier,
             'max_completion_tokens':settings.max_completion_tokens,
             'response_format':{'type':'json_object'},
             'messages':[{'role':'system','content':render_prompt('reply_classification',config)},{'role':'user','content':json.dumps({'message':body},ensure_ascii=False)}]}
    call_id,reason=llm_usage.reserve(config,'reply_classification',run_id)
    if not call_id:return result('other',0,body,source='fallback',error=reason)
    try:
        response,_=request_completion(payload,settings,resolve_key(settings))
    except Exception as exc:
        llm_usage.finish(call_id,None,resolve_model(config,'reply_classification'),'provider_error')
        out=result('other',0,body,source='fallback',model=settings.model,error='provider_unavailable')
        out['failure_kind'] = str(exc) if isinstance(exc,ProviderRejected) and re.fullmatch(r'HTTP_\d{3}',str(exc)) else type(exc).__name__ if isinstance(exc,(TimeoutError,ConnectionError,OSError)) else 'unavailable'
        return out
    llm_usage.finish(call_id,response.get('usage') if isinstance(response,dict) else None,resolve_model(config,'reply_classification'))
    try: return parse_result(response,body,settings)
    except ValueError:
        return result('other',0,body,source='fallback',model=settings.model,error='invalid_model_result')


def classify_pending(store,config,*,reply_ids=None,model_limit=None,platform=None):
    options=config.engine.monitor
    if not options.classification_enabled: return {'classified':0,'model_calls':0}
    sql.recover_classifications(store)
    cap=options.classification_model_limit if model_limit is None else min(options.classification_model_limit,max(0,model_limit))
    import uuid
    run_id=uuid.uuid4().hex
    count=calls=0
    for reply in sql.pending_replies(store,len(reply_ids) if reply_ids is not None else options.classification_batch_limit,reply_ids=reply_ids,platform=platform):
        if not sql.claim_classification(store,reply['id']): continue
        # Status-only/excerpt/legacy synthetic events are not HR message evidence.
        if reply['id_origin']!='platform' or reply['is_excerpt']:
            out=result('other',0,reply['body'],source='excluded')
        else:
            use_model=options.classification_model_enabled and calls<cap
            needs_model=use_model and rule(reply['body']) is None
            if needs_model: calls+=1
            out=classify(reply['body'],config.scoring,use_model=use_model,config=config,run_id=run_id)
        count+=sql.save_classification(store,reply['id'],out)
    return {'classified':count,'model_calls':calls}
