"""QA-guided drafts for user notifications. No HR transport or browser imports."""
import json
from pathlib import Path
import re
import uuid
import yaml
from job_agent.config import job_agent_home
from job_agent.config.llm import provider_settings, resolve_model
from job_agent.service import scoring, llm_usage
from job_agent.service.prompt_files import render_prompt
from job_agent.service.greeting_generation import _numbers

ALIASES = {
    '期望薪资':r'薪资|薪酬|期望.{0,5}(?:工资|待遇)|工资.{0,5}(?:要求|期望|多少)',
    '离职原因':r'离职|离开.{0,8}(?:公司|工作)|为什么.{0,8}(?:换|跳槽)',
    '到岗时间':r'到岗|入职|什么时候.{0,5}(?:来|上班)',
    '现居城市':r'现居|目前.{0,5}(?:城市|哪里|在哪)|现在.{0,5}(?:哪里|哪|住)|所在城市',
    '是否接受外包':r'外包|外派|派遣',
    '能否加班':r'加班|996|大小周|双休|单休',
}


def load_answers(config):
    path = Path(config.qa.path.replace('$JOB_AGENT_HOME',str(job_agent_home()))).expanduser()
    try:
        with path.open(encoding='utf-8') as stream:
            text = stream.read(65537)
        if len(text)>65536:
            return {}
        # Match settings YAML restrictions: no aliases, merges or duplicate keys.
        from job_agent.config.yaml_patch import document
        document(text)
        answers = yaml.safe_load(text)
        if (not isinstance(answers,dict) or len(answers)>100
                or any(not isinstance(k,str) or not k.strip() or len(k)>80
                    or not isinstance(v,str) or not v.strip() or len(v)>1000 for k,v in answers.items())):
            return {}
        return answers
    except (OSError,ValueError,yaml.YAMLError):
        return {}


def suggest(store, reply, config, *, job=None, category=None):
    if not config.notify.draft_reply:
        return None
    key = 'reply-draft:'+str(reply['id'])
    prior = store.connection.execute('SELECT summary FROM events WHERE source_key=?',(key,)).fetchone()
    if prior:
        cached = json.loads(prior[0])
        return cached if cached.get('text') else None
    answers = load_answers(config)
    matches = {k:v for k,v in answers.items() if re.search(ALIASES.get(k,re.escape(k)),reply['body'])}
    if not matches and category not in config.notify.draft_reply_categories:
        return None
    from job_agent.service.greeting_facts import confirmed_texts
    experience = list(dict.fromkeys([*config.greetings.generate.facts, *confirmed_texts()]))
    standard = ' '.join(matches.values())
    # Numbers may only come from the user's own material or the HR message.
    grounded = ' '.join([standard, *answers.values(), *experience, reply['body']])
    out = {'categories':list(matches),'text':standard,'source':'standard' if matches else 'none'}
    model = resolve_model(config,'draft_reply')
    try:
        provider = provider_settings(config,'draft_reply')
        profile = {k:v for k,v in config.profile.model_dump().items() if k in ('name','role','availability') and v}
        content = json.dumps({'message':reply['body'],'standard_answers':matches,
            'other_answers':{k:v for k,v in answers.items() if k not in matches},
            'experience':experience,'profile':profile,
            'job':{'company':(job or {}).get('company',''),'title':(job or {}).get('title','')}},ensure_ascii=False)
        if len(content)>config.scoring.max_input_chars:
            raise ValueError('input_too_large')
        payload = {'model':model.model,'service_tier':model.service_tier,
            'max_completion_tokens':provider.max_completion_tokens,'response_format':{'type':'json_object'},
            'messages':[{'role':'system','content':render_prompt('draft_reply',config)}, {'role':'user','content':content}]}
        credential = scoring.resolve_key(provider)
        call_id,reason = llm_usage.reserve(config,'draft_reply',uuid.uuid4().hex)
        if not call_id:
            raise ValueError('budget_exhausted')
        try:
            response,_ = scoring.request_completion(payload,provider,credential)
        except BaseException:
            llm_usage.finish(call_id,None,model,'provider_error')
            raise
        llm_usage.finish(call_id,response.get('usage') if isinstance(response,dict) else None,model)
        choice = response['choices'][0]
        if choice.get('finish_reason','stop')!='stop' or choice['message'].get('refusal'):
            raise ValueError('invalid_response')
        text = json.loads(choice['message']['content'])['text']
        if (not isinstance(text,str) or not text.strip() or len(text)>1000 or _numbers(text)-_numbers(grounded)
                or re.search(r'https?://|@|1[3-9]\d{9}',text)):
            raise ValueError('invalid_draft')
        out.update(text=text.strip(),source='generated')
    except Exception:
        pass  # Exact user-authored standard answers remain useful without a model.
    store.append_event(out,source_key=key)
    return out if out['text'] else None
