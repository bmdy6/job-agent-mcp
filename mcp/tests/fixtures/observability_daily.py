"""Offline daily smoke. All browser, provider and Telegram effects are fixtures.

Run: uv run --project mcp python mcp/tests/fixtures/observability_daily.py OUT_DIR
"""
from contextlib import ExitStack
import json
import logging
import os
from pathlib import Path
import sys
from unittest.mock import patch

from job_agent.browser.base import FakeBrowser
from job_agent.collect.fake import FakeSource
from job_agent.collect.runner import Collector
from job_agent.config import Settings
from job_agent.engine.models import Status, Reason
from job_agent.engine.queue import DeliveryEngine
from job_agent.logsetup import setup_logging
from job_agent.observability import current_run_id
from job_agent.service.daily import daily_once
from job_agent.service import scoring
from job_agent.store import Store
from job_agent.store.collection import CollectionRepository
from job_agent.store import scoring as sql


def smoke(folder):
    folder=Path(folder).resolve()
    folder.mkdir(parents=True,exist_ok=True)
    os.environ['JOB_AGENT_HOME']=str(folder)
    config=Settings(daily={'enabled':True,'deliver_enabled':True},
        engine={'dry_run':False,'live_send_enabled':True,'platforms':{'boss':{
            'enabled':True,'greeting':'离线回归','greeting_max_length':50,'interval_seconds':0}}},
        collect={'keywords':['Python'],'cities':['苏州'],'interval_seconds':[8,8],
                 'platforms':{'boss':{'enabled':True,'daily_page_limit':1}}},
        notify={'enabled':True,'channels':{'tg':{'type':'telegram','enabled':True,'chat_id':'offline-fixture'}}})
    setup_logging('daily',home=folder)
    effects=[]
    def collect():
        with Store() as store:
            now=[1_795_824_000.]
            def advance(seconds): now[0]+=seconds
            collector=Collector(FakeBrowser(),[FakeSource('boss')],CollectionRepository(store),config.collect,
                                clock=lambda:now[0],sleep=advance)
            result=collector.run()
            jobs=store.list_jobs()
            ident=current_run_id()
            with store.transaction():
                sql.reserve(store,ident,jobs,{'resume':'离线简历','experiences':[]},config.scoring,scoring.digest(config.scoring.model_dump()))
        # Existing daily doesn't automatically buy scores. Only this fixture
        # explicitly inserts and executes a reserved scoring run, with zero HTTP.
        scoring.execute_run(ident)
        return result
    def deliver(engine,job,settings,space):
        engine.store.update(job,Status.UNVERIFIED,Reason.INTERRUPTED,1_795_824_000)
        engine.store.update(job,Status.SENT,Reason.NONE)
        logging.getLogger('fixture').info('delivery fixture completed run_id=%s',current_run_id())
    def telegram(url,payload,headers=None):
        effects.append(payload['text'])
        logging.getLogger('fixture').info('notification fixture completed run_id=%s',current_run_id())
        return {'ok':True}
    response={'choices':[{'finish_reason':'stop','message':{'content':'{"score":82,"reason":"离线夹具匹配"}'}}],
              'usage':{'prompt_tokens':10,'completion_tokens':5}}
    with ExitStack() as stack:
        for target,value in [('job_agent.service.scoring.load_config',config),('job_agent.notify.secret','offline-fixture'),
                             ('job_agent.service.scoring.resolve_key','offline-fixture')]:
            stack.enter_context(patch(target,return_value=value))
        stack.enter_context(patch.object(scoring,'request_completion',return_value=(response,{'x-ratelimit-remaining-tokens':'1000000'})))
        stack.enter_context(patch('job_agent.notify.post',side_effect=telegram))
        stack.enter_context(patch('job_agent.service.safety.PlatformSafety.require'))
        stack.enter_context(patch('job_agent.engine.rate.RateLimiter.acquire'))
        stack.enter_context(patch.object(DeliveryEngine,'_deliver',deliver))
        stack.enter_context(patch('job_agent.engine.spaces.TaskSpaces.shutdown'))
        result=daily_once(config=config,collect_fn=collect,max_per_platform=1)
    with Store() as store:
        audits=[json.loads(r['error_detail'])['run_id'] for r in store.list_deliveries()]
        scores=[r[0] for r in store.connection.execute('SELECT run_id FROM job_scores')]
    ident=result['run_id']
    assert result['status']=='completed',result['status']
    assert audits and set(audits)=={ident},audits
    assert scores==[ident],scores
    assert len(effects)==2 and all(t.endswith('运行编号：'+ident[:8]) for t in effects)
    evidence=dict(status=result['status'],run_id=ident,audit_rows=len(audits),scored_jobs=len(scores),
                  mocked_notifications=len(effects),log=str(folder/'logs/daily.log'))
    (folder/'evidence.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2))
    return evidence


if __name__=='__main__':
    print(json.dumps(smoke(sys.argv[1]),ensure_ascii=False))
