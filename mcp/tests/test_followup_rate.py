"""Real live-engine pacing/lock branches, synthetic browser and no send transport."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from datetime import datetime,timezone
from job_agent.config import Settings
from job_agent.store import SQLiteDeliveryStore
from job_agent.engine.models import Job,Status,Reason
from job_agent.engine.queue import DeliveryEngine
from job_agent.platforms.boss.apply import BossAdapter
from job_agent.platforms.preflight import AdapterPreflight
from job_agent.browser.base import FakeBrowser
from job_agent.engine.spaces import TaskSpaces
from job_agent.store.automation import AutomationRepository
from job_agent.engine.rate import RateLimiter


class FollowupRateTests(unittest.TestCase):
    def test_live_shared_pacing_hours_and_platform_lock(self):
        for mode in ('paced','hourly','outside','locked'):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as home,patch.dict('os.environ',JOB_AGENT_HOME=home):
                at=[1800000000.0]
                cfg=Settings(followup={'enabled':True,'auto':True,'after_days':[1,1]},engine={
                    'dry_run':False,'live_send_enabled':True,'timezone':'UTC','platforms':{'boss':{
                        'enabled':True,'greeting':'你好','greeting_max_length':1000,'daily_limit':10,
                        'rate':{'interval_seconds':[30,30],'hourly_limit':1 if mode=='hourly' else 20,
                            'allowed_hours':['00:00','00:01'] if mode=='outside' else ['00:00','23:59'],'timezone':'UTC'}}}})
                with SQLiteDeliveryStore(Path(home)/'jobs.sqlite3',dry_run=False) as db:
                    jid=db.upsert_job(platform='boss',platform_job_id='one',url='https://example.invalid/one',company='公司',title='岗位')
                    db.record_delivery(jid,status='sent',created_at=datetime.fromtimestamp(at[0]-2*86400,timezone.utc).isoformat())
                    greetings={'boss':'你好','boss:one':cfg.followup.messages[0],'_kind':'followup','_followup':cfg.followup.model_dump(mode='json')}
                    token=db.create_batch([Job('boss','one','https://example.invalid/one')],greetings,'',cfg.engine.model_dump())
                    with patch('job_agent.store.delivery.time.time',return_value=at[0]):db.confirm_batch(token,config=cfg.engine.model_dump())
                    repo=AutomationRepository(db)
                    RateLimiter(repo,clock=lambda:at[0]-10).acquire('boss','delivery',cfg.engine.platforms['boss'].rate.model_copy(update={'allowed_hours':('00:00','23:59')}))
                    browser=FakeBrowser();adapters={'boss':BossAdapter(browser,{})}
                    engine=DeliveryEngine(db,cfg.engine,adapters,AdapterPreflight(browser,adapters),TaskSpaces(browser),
                        safety=SimpleNamespace(require=lambda _:None),clock=lambda:at[0],sleep=lambda secs:at.__setitem__(0,at[0]+secs))
                    def effect(job,*args):
                        db.update(job,Status.UNVERIFIED,Reason.INTERRUPTED,at[0]);db.update(job,Status.SENT,Reason.NONE)
                    with patch('job_agent.engine.freshness.check',return_value=True),patch.object(engine,'_deliver',side_effect=effect) as deliver:
                        if mode=='locked':
                            from job_agent.browser.platform_lock import platform_lock
                            with platform_lock('boss'):engine.run(batch_token=token)
                        else:engine.run(batch_token=token)
                    if mode=='paced':
                        deliver.assert_called_once();self.assertGreaterEqual(at[0],1800000020)
                        self.assertEqual(len(repo.operations('boss','delivery')),2)
                    else:
                        deliver.assert_not_called()
                        self.assertEqual(db.records()[0].reason.value,{'hourly':'hourly_limit','outside':'outside_hours','locked':'platform_busy'}[mode])
