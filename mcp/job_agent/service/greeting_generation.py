"""Paid API generation in planning; canonical copy is cached before rendering."""
import json
import re
import time
import uuid

from job_agent.platforms.greeting import split_hook, lint_greeting, prepare_greeting, _numeric_achievements
from job_agent.service import scoring
from job_agent.service.greeting_facts import select_anecdote
from job_agent.service.greeting_cache import GreetingCache
from job_agent.service.greeting_templates import select_template

from job_agent.config.llm import provider_settings, resolve_model
from job_agent.service.prompt_files import render_prompt
from job_agent.service import llm_usage


def _numbers(value):
    """Normalize Chinese quantities without treating conversational 一下 as a claim."""
    digits = dict(zip('零〇一二三四五六七八九两', (0,0,1,2,3,4,5,6,7,8,9,2)))
    units = {'十':10, '百':100, '千':1000, '万':10000, '亿':100000000}
    result = set()
    for token in _numeric_achievements(value):
        suffix = '%' if '%' in token else ''
        token = token.replace('%', '')
        if re.match(r'^[0-9]', token):
            multiplier = 10000 if token.endswith('万') else 100000000 if token.endswith('亿') else 1
            result.add((float(token.rstrip('万亿')) * multiplier, suffix))
        else:
            total = section = number = 0
            for ch in token:
                if ch in digits:
                    number = digits[ch]
                elif units[ch] < 10000:
                    section += (number or 1) * units[ch]; number = 0
                else:
                    section = (section + number or 1) * units[ch]
                    total += section; section = number = 0
            result.add((total + section + number, suffix))
    return result


def job_key(job):
    return job['platform'] + ':' + str(job['platform_job_id'])


def _lint(text, settings=None, **kwargs):
    options = {} if settings is None else dict(blacklist=settings.blacklist,
        jargon_terms=settings.jargon_terms, max_sentence_length=settings.max_sentence_length,
        similarity_threshold=settings.similarity_threshold, max_sentence_pattern=settings.max_sentence_pattern, hook_prefixes=settings.hook_prefixes, hook_suffixes=settings.hook_suffixes)
    return lint_greeting(text, **options, **kwargs)


def validate_result(result, facts, *, company_context='', blacklist=None, settings=None,
                    previous_entries=(), anecdote=None, closing_line=None, profile=None):
    if not isinstance(result, dict) or not isinstance(result.get('text'), str):
        raise ValueError('invalid_response')
    text = result['text'].strip()
    fact = result.get('fact')
    paragraphs = text.split('\n\n')
    if not facts or fact not in facts or len(paragraphs) not in {3, 4}:
        raise ValueError('fact_not_grounded')
    experience = result.get('experience')
    if (not isinstance(experience, str) or not experience
            or split_hook(paragraphs[1])[1] != experience):
        raise ValueError('fact_not_grounded')
    sources = [fact]
    story_id = result.get('anecdote_id')
    story_text = result.get('anecdote_text', '')
    if story_id:
        if not anecdote or story_id != anecdote['id']:
            raise ValueError('fact_not_grounded')
        sources.append(anecdote['text'])
    if len(paragraphs) == 4:
        if not story_id or not story_text or paragraphs[2] != story_text:
            raise ValueError('fact_not_grounded')
    elif story_text or (story_id and fact != anecdote['text']):
        raise ValueError('fact_not_grounded')
    if anecdote and fact == anecdote['text'] and (story_id != anecdote['id'] or len(paragraphs) != 3):
        raise ValueError('fact_not_grounded')
    if _numbers(text) - _numbers(''.join(sources)):
        raise ValueError('number_outside_fact')
    if any(other not in sources and other in text for other in facts):
        raise ValueError('multiple_facts')
    hook = result.get('company_hook', '')
    if not isinstance(hook, str) or not 1 <= len(hook) <= 15:
        raise ValueError('invalid_response')
    options = dict(company_context=company_context, company_hook=hook,
                   candidate_facts=sources, previous_entries=previous_entries, profile=profile)
    lint = _lint(text, settings, **options) if settings else lint_greeting(text, blacklist=blacklist, **options)
    if closing_line is not None and paragraphs[-1] != closing_line:
        lint['issues'].append({'code': 'closing'})
        lint['ok'] = False
    if not lint['ok']:
        raise ValueError('lint:' + ','.join(i['code'] for i in lint['issues']))
    return text


def _reason(exc):
    # Never persist provider content, parsed fields, private paths or credentials.
    safe = {'facts_empty', 'input_too_large', 'invalid_response', 'fact_not_grounded',
            'multiple_facts', 'number_outside_fact', 'platform_limit', 'invalid_greeting_facts',
            'invalid_greeting_templates', 'daily_budget_exhausted'}
    lint_codes = {'empty', 'too_long', 'too_short', 'paragraph_format', 'ai_cliche',
                  'numeric_list', 'ungrounded_company', 'long_sentence', 'jargon_overload', 'homogeneous',
                  'identity', 'experience_missing', 'closing', 'sentence_pattern', 'choppy'}
    value = str(exc)
    if type(exc) is ValueError and (value in safe or
            (value.startswith('lint:') and set(value[5:].split(',')) <= lint_codes)):
        return value
    return type(exc).__name__


def _render(job, config, text):
    pcfg = config.engine.platforms[job['platform']]
    text = prepare_greeting(job['platform'], text, pcfg.greeting_newline_separator,
                            paragraph_separator=pcfg.greeting_paragraph_separator)
    if len(text.encode('utf-16-le')) // 2 > pcfg.greeting_max_length:
        raise ValueError('platform_limit')
    return text


def _fallback(settings, reason, attempts=0, config=None):
    text = render_prompt('fallback', config)
    lint = _lint(text, settings, profile=config.profile if config else None)
    # A custom blacklist may exclude the generic copy; never send failed lint.
    return (text if lint['ok'] else ''), {
        'source': 'fallback' if lint['ok'] else 'blocked', 'reason': reason,
        'attempts': attempts, 'lint': lint}


def generate(job, config, *, run_id=None, preview_only=False):
    """preview_only permits offline drafts for platform_default; never site writes."""
    pcfg = config.engine.platforms[job['platform']]
    settings = config.greetings.generate
    fixed = prepare_greeting(job['platform'], pcfg.greeting, pcfg.greeting_newline_separator,
                             paragraph_separator=pcfg.greeting_paragraph_separator)
    if not settings.enabled:
        return fixed, {'source': 'fixed', 'reason': 'disabled'}
    if pcfg.greeting_mode != 'send_message' and not (preview_only and pcfg.greeting_mode == 'platform_default'):
        return fixed, {'source': 'fixed', 'reason': 'mode_has_no_per_job_composer'}
    run_id = run_id or uuid.uuid4().hex
    try:
        with GreetingCache() as cache:
            key = cache.key(job)
            cached = cache.claim(key)
            if cached is not None:
                text, trace = cached
                if not text:
                    text, trace = _fallback(settings, trace['reason'], config=config)
                else:
                    batch_lint = _lint(text, settings, previous_entries=cache.recent_entries(
                        trace.get('direction'), job_key(job), run_id=run_id),
                        company_hook=trace.get('company_hook', ''), profile=config.profile)
                    if not batch_lint['ok']:
                        codes = {i['code'] for i in batch_lint['issues']}
                        if codes <= {'homogeneous', 'sentence_pattern'}:
                            # A new batch may combine individually valid old drafts.
                            # Only content conflicts authorize a new bounded generation.
                            text, trace = _generate(job, config, cache, key, run_id)
                            cache.save(key, text, trace)
                            return _render(job, config, text), {**trace, 'cache_hit': False, 'batch_regenerated': True}
                        text, trace = _fallback(settings, 'cached_copy_failed_lint', config=config)
                    else:
                        cache.join_batch(key, text, trace, run_id)
                return _render(job, config, text), {**trace, 'cache_hit': True}
            text, trace = _generate(job, config, cache, key, run_id)
            cache.save(key, text, trace)
            return _render(job, config, text), {**trace, 'cache_hit': False}
    except Exception as exc:
        text, trace = _fallback(settings, _reason(exc), config=config)
        try:
            return _render(job, config, text), trace
        except ValueError:
            return '', {**trace, 'source': 'blocked', 'reason': 'platform_limit'}


def _generate(job, config, cache, cache_key, run_id, *, remaining_attempts=None):
    settings = config.greetings.generate
    attempt = 0
    try:
        anecdote = select_anecdote(job)
        facts = list(settings.facts)
        if anecdote and anecdote['text'] not in facts:
            facts.append(anecdote['text'])
        if not facts:
            raise ValueError('facts_empty')
        direction, example = select_template(job, settings, config.profile)
        previous = cache.recent_entries(direction, job_key(job), run_id=run_id)
        counts = {line: sum(p.get('closing') == line for p in previous) for line in settings.closing_lines}
        closing = min(counts, key=counts.get)
        data = {k: job.get(k) or '' for k in ('jd', 'company', 'title', 'company_intro')}
        data.update(facts=settings.facts, blacklist=settings.blacklist, jargon_terms=settings.jargon_terms,
                    direction=direction, few_shot=example, hook_prefixes=settings.hook_prefixes, hook_suffixes=settings.hook_suffixes,
                    previous_entries=previous, closing_line=closing,
                    similarity_threshold=settings.similarity_threshold, max_sentence_pattern=settings.max_sentence_pattern)
        if anecdote:
            data['anecdote'] = {k: anecdote[k] for k in ('id', 'text', 'tags', 'when', 'source')}
        context = '\n'.join(str(job.get(k) or '') for k in ('jd', 'company_intro'))
        provider = provider_settings(config,'greetings')
        model = resolve_model(config,'greetings')
        credential = scoring.resolve_key(provider)
        for attempt in range(1, (remaining_attempts if remaining_attempts is not None else settings.max_retries + 1) + 1):
            content = json.dumps(data, ensure_ascii=False)
            if len(content) > config.scoring.max_input_chars:
                raise ValueError('input_too_large')
            payload = {'model': model.model, 'service_tier': model.service_tier,
                       'response_format': {'type': 'json_object'},
                       'messages': [{'role': 'system', 'content': render_prompt('greeting', config)}, {'role': 'user', 'content': content}]}
            call_id,budget_reason=llm_usage.reserve(config,'greetings',run_id,
                historical_calls=cache.calls_today(config.engine.timezone))
            if not call_id:
                return _fallback(settings,budget_reason,attempt-1,config)
            ident = cache.reserve_call(cache_key, run_id, model.model, settings.daily_budget_calls, config.engine.timezone)
            if not ident:
                llm_usage.finish(call_id,None,model,'budget_blocked')
                return _fallback(settings,'daily_budget_exhausted',attempt-1,config)
            started = time.monotonic()
            usage = None
            try:
                response, _ = scoring.request_completion(payload, provider, credential)
            except BaseException as exc:
                value=llm_usage.finish(call_id,None,model,'provider_error')
                cache.finish_call(ident, 'provider_error', round((time.monotonic()-started)*1000), usage=value,error=_reason(exc))
                raise
            elapsed = round((time.monotonic()-started)*1000)
            value=llm_usage.finish(call_id,response.get('usage') if isinstance(response,dict) else None,model)
            value.update(run_id=run_id,call_id=call_id)
            try:
                result = None
                usage = value
                choice = response['choices'][0]
                if choice.get('finish_reason', 'stop') != 'stop' or choice['message'].get('refusal'):
                    raise ValueError('invalid_response')
                result = json.loads(choice['message']['content'])
                text = validate_result(result, facts, company_context=context, settings=settings, previous_entries=previous,
                                       anecdote=anecdote, closing_line=closing, profile=config.profile)
                trace = {'source': 'generated', 'reason': '', 'attempts': attempt, 'run_id': run_id,
                         'direction': direction, 'job_key': job_key(job), 'company_hook': result['company_hook'],
                         'fact': result['fact'], 'experience': result['experience'],
                         'lint': _lint(text, settings, company_context=context,
                            company_hook=result['company_hook'], candidate_facts=[result['fact']], profile=config.profile)}
                if anecdote and result.get('anecdote_id') == anecdote['id']:
                    trace['anecdote_id'] = anecdote['id']
                cache.finish_call(ident, 'generated', elapsed, usage)
                return text, trace
            except (ValueError, KeyError, TypeError, IndexError, AttributeError) as exc:
                reason = _reason(exc)
                cache.finish_call(ident, 'lint_failed', elapsed, usage, reason)
                data['retry_reason'] = reason
                rejected = result.get('text') if isinstance(result, dict) else None
                data['rejected_second_paragraph'] = rejected.split('\n\n')[1:2] if isinstance(rejected, str) else []
        return _fallback(settings, reason, attempt, config)
    except Exception as exc:
        return _fallback(settings, _reason(exc), attempt, config)
