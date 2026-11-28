"""Private shared dispatch ledger. No prompts, messages, credentials or URLs."""
from contextlib import closing, contextmanager
from datetime import datetime, timedelta
from decimal import Decimal
import json
import os
import sqlite3
import uuid
from zoneinfo import ZoneInfo
from job_agent.config import job_agent_home
from job_agent.config.llm import resolve_model

PURPOSES=('scoring','greetings','reply_classification','draft_reply')
UNKNOWN='费用未知（未配置价格）'


def metrics(usage, model):
    usage=usage if isinstance(usage,dict) else {}
    def count(key):
        value=usage.get(key)
        return value if type(value) is int and 0<=value<=10**12 else None
    prompt,output,total=(count(k) for k in ('prompt_tokens','completion_tokens','total_tokens'))
    if total is None and prompt is not None and output is not None:total=prompt+output
    details=usage.get('prompt_tokens_details')
    cached=details.get('cached_tokens',0) if isinstance(details,dict) else 0
    valid_cached=type(cached) is int and prompt is not None and 0<=cached<=prompt
    cost=None
    if model.pricing and prompt is not None and output is not None and valid_cached:
        price=model.pricing
        cost=float((Decimal(prompt-cached)*Decimal(str(price.input_per_million))+
                    Decimal(cached)*Decimal(str(price.cached_input_per_million))+
                    Decimal(output)*Decimal(str(price.output_per_million)))/Decimal(1000000))
    return dict(prompt_tokens=prompt,completion_tokens=output,total_tokens=total,
                cached_input_tokens=cached if valid_cached else None,cost=cost,
                currency=model.pricing.currency.upper() if model.pricing else None,
                unknown_reason=None if cost is not None else 'pricing_missing' if not model.pricing else 'usage_missing',
                pricing=model.pricing.model_dump() if model.pricing else None)


@contextmanager
def ledger():
    home=job_agent_home();home.mkdir(parents=True,exist_ok=True,mode=0o700)
    path=home/'llm-usage.sqlite3'
    fd=os.open(path,os.O_CREAT|os.O_RDWR,0o600);os.close(fd)
    with closing(sqlite3.connect(path,timeout=10,isolation_level=None)) as db:
        db.row_factory=sqlite3.Row
        db.execute('''CREATE TABLE IF NOT EXISTS calls (
            id TEXT PRIMARY KEY,run_id TEXT NOT NULL,purpose TEXT NOT NULL,day TEXT NOT NULL,
            model TEXT NOT NULL,status TEXT NOT NULL,usage_json TEXT NOT NULL,
            cost REAL,currency TEXT)''')
        db.execute('CREATE INDEX IF NOT EXISTS usage_day ON calls(day,purpose)')
        db.execute('CREATE TABLE IF NOT EXISTS alerts(day TEXT PRIMARY KEY)')
        yield db


def day_for(config, now=None):
    now=now or datetime.now(ZoneInfo(config.engine.timezone))
    return now.astimezone(ZoneInfo(config.engine.timezone)).date()


def reserve(config,purpose,run_id, *, historical_calls=0):
    model=resolve_model(config,purpose);policy=getattr(config.llm,purpose)
    legacy=config.greetings.generate if purpose=='greetings' else config.scoring
    cap=policy.daily_calls
    if purpose not in {'reply_classification','draft_reply'}:
        old=getattr(legacy,'daily_budget_calls',None)
        cap=min(v for v in (cap,old) if v is not None) if cap is not None or old is not None else None
    local_cost=policy.daily_cost
    if local_cost is None and purpose not in {'reply_classification','draft_reply'}:local_cost=legacy.daily_cost
    day=day_for(config).isoformat();reason='';alert=False
    with ledger() as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            rows=db.execute('SELECT * FROM calls WHERE day=?',(day,)).fetchall()
            own=[r for r in rows if r['purpose']==purpose]
            if (config.llm.budget.daily_calls is not None and len(rows)>=config.llm.budget.daily_calls) or (cap is not None and max(len(own),historical_calls)>=cap):
                reason='daily_budget_exhausted'
            # Unknown pricing deliberately uses only count caps. Never mix currencies.
            if not reason and model.pricing:
                currency=model.pricing.currency.upper()
                for budget,subset in ((config.llm.budget,rows),(policy,own)):
                    limit=config.llm.budget.daily_cost if budget is config.llm.budget else local_cost
                    if limit is None:continue
                    target=(budget.currency or currency).upper()
                    if target!=currency or any(r['currency'] and r['currency']!=target for r in subset):
                        reason='budget_currency_mismatch';break
                    if sum(r['cost'] or 0 for r in subset)>=limit:
                        reason='daily_cost_exhausted';break
                    if any(r['status']=='in_progress' for r in subset):
                        reason='budget_call_in_progress';break
                    if any(r['cost'] is None and r['currency'] for r in subset):
                        reason='budget_cost_unknown';break
            ident=None
            if not reason:
                ident=uuid.uuid4().hex
                value=metrics(None,model)
                db.execute('INSERT INTO calls VALUES(?,?,?,?,?,?,?,?,?)',
                    (ident,run_id,purpose,day,model.model,'in_progress',json.dumps(value),None,value['currency']))
            elif reason in {'daily_budget_exhausted','daily_cost_exhausted','budget_currency_mismatch','budget_cost_unknown'}:
                alert=bool(db.execute('INSERT OR IGNORE INTO alerts VALUES(?)',(day,)).rowcount)
            db.commit()
        except BaseException:
            db.rollback();raise
    if alert:
        # Durable queue only: the normal notification drain sends the alert. A
        # provider outage must not turn a budget check into a second network call.
        try:
            from job_agent.notify import send_one
            if config.notify.enabled:
                for channel in config.notify.channels.values():
                    if channel.enabled and channel.type=='telegram':
                        send_one(channel,'job-agent 模型预算告警','今日模型预算已耗尽、费用未知或币种配置不一致，相关调用已停止。',queue_only=True)
        except Exception:
            pass  # The durable daily claim still prevents repeated alert dispatch.
    return ident,reason


def finish(ident,usage,model,status='completed'):
    value=metrics(usage,model)
    with ledger() as db:
        row=db.execute('SELECT run_id FROM calls WHERE id=?',(ident,)).fetchone()
        value.update(call_id=ident,run_id=row['run_id'] if row else '')
        db.execute('UPDATE calls SET status=?,usage_json=?,cost=?,currency=? WHERE id=?',
                   (status,json.dumps(value),value['cost'],value['currency'],ident))
    return value


def historical_rows(config, known_ids, start, end):
    """Read old ledgers without migration or repricing; new calls dedupe by ID."""
    rows=[]
    def add(purpose,day,usage):
        if start<=day<=end and usage.get('call_id') not in known_ids:
            value={**metrics(usage,resolve_model(config,purpose).model_copy(update={'pricing':None})),**usage}
            rows.append(dict(purpose=purpose,day=day,usage_json=json.dumps(value),
                             cost=value.get('cost'),currency=value.get('currency')))
    path=job_agent_home()/'greeting-generation.sqlite3'
    if path.exists():
        with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as db:
            db.row_factory=sqlite3.Row
            columns={r[1] for r in db.execute('PRAGMA table_info(calls)')}
            if {'day','prompt_tokens','completion_tokens','total_tokens'}<=columns:
                for row in db.execute("SELECT * FROM calls WHERE day>=? AND day<=? AND status!='budget_blocked'",(start,end)):
                    usage=json.loads(row['usage_json']) if 'usage_json' in columns else {}
                    if not usage:usage={k:row[k] for k in ('prompt_tokens','completion_tokens','total_tokens')}
                    add('greetings',row['day'],usage)
    path=job_agent_home()/'jobs.sqlite3'
    if path.exists():
        with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as db:
            tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if {'scoring_runs','scoring_items'}<=tables:
                for created,usage in db.execute("""SELECT r.created_at,i.usage_json FROM scoring_items i
                    JOIN scoring_runs r ON r.id=i.run_id WHERE i.status IN ('running','completed','failed','unverified')"""):
                    day=datetime.fromisoformat(created).astimezone(ZoneInfo(config.engine.timezone)).date().isoformat()
                    add('scoring',day,json.loads(usage))
    return rows


def summary(config, *, now=None):
    today=day_for(config,now);start=today-timedelta(days=6)
    path=job_agent_home()/'llm-usage.sqlite3';rows=[]
    if path.exists():
        with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as db:
            db.row_factory=sqlite3.Row
            rows=[dict(r) for r in db.execute('SELECT * FROM calls WHERE day>=? AND day<=?',(start.isoformat(),today.isoformat()))]
    rows.extend(historical_rows(config,{r['id'] for r in rows},start.isoformat(),today.isoformat()))
    result={}
    for window,cutoff in (('today',today),('last_7_days',start)):
        result[window]={}
        for purpose in PURPOSES:
            selected=[r for r in rows if r['purpose']==purpose and r['day']>=cutoff.isoformat()]
            values=[json.loads(r['usage_json']) for r in selected];costs={}
            for row in selected:
                if row['cost'] is not None:costs[row['currency']]=costs.get(row['currency'],0)+row['cost']
            unknown=sum(r['cost'] is None for r in selected)
            text='、'.join(f'{value:.6f} {currency}' for currency,value in sorted(costs.items()))
            if unknown or (not selected and not resolve_model(config,purpose).pricing):
                label=UNKNOWN if any(v.get('unknown_reason')=='pricing_missing' for v in values) or not values else '费用未知（usage 缺失）'
                text=(text+'；' if text else '')+label
            result[window][purpose]=dict(calls=len(selected),
                **{k:sum(v.get(k) or 0 for v in values) for k in ('prompt_tokens','completion_tokens','total_tokens','cached_input_tokens')},
                costs=costs,unknown_cost_calls=unknown,cost_text=text or '0',
                unknown_usage_calls=sum(v.get('prompt_tokens') is None or v.get('completion_tokens') is None for v in values))
    return result


def summary_text(report):
    labels={'scoring':'评分','greetings':'招呼语','reply_classification':'回复分类','draft_reply':'回复建议'}
    return '\n'.join(('当天' if window=='today' else '最近7天')+'模型：'+'；'.join(
        f'{labels[p]} {r["calls"]}次 / 输入{r["prompt_tokens"]} 缓存{r["cached_input_tokens"]} 输出{r["completion_tokens"]} token / {r["cost_text"]}'
        for p,r in values.items()) for window,values in report.items())
