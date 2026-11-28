"""Compare the complete public projection with the actual pre-v7 code."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[2]
BASE='0e89441'


class StateProjectionTest(unittest.TestCase):
    def test_v6_v7_public_outputs_match(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);base=root/'base';base.mkdir();home=root/'home';home.mkdir()
            # Export the real v6 code, without touching another agent's checkout.
            import io,tarfile
            archive=subprocess.check_output(['git','-C',str(ROOT),'archive',BASE])
            with tarfile.open(fileobj=io.BytesIO(archive)) as stream:
                stream.extractall(base,filter='data')
            oldenv={**os.environ,'JOB_AGENT_HOME':str(home),'PYTHONPATH':str(base/'mcp')}
            seed='''from job_agent.store import Store
from pathlib import Path
from job_agent.config import job_agent_home
import json
h=job_agent_home()
(h/'config.yaml').write_text('engine:\\n  platforms:\\n    boss: {enabled: true, greeting_max_length: 1000}\\n')
with Store() as s:
 j=s.upsert_job(platform='boss',platform_job_id='synthetic',company='Synthetic company',title='Synthetic job')
 s.record_delivery(j,status='sent',greeting='synthetic',created_at='2026-11-27T00:00:00+00:00')
 s.add_reply(j,message_id='synthetic',direction='inbound',body='synthetic',occurred_at='2026-11-28T00:00:00+00:00')
diag={'boss':{'count':0,'stage':'complete','reason_code':'ok','last_success_at':1795867000}}
for name,data in {'monitor-state.json':diag,'monitor-runtime.json':dict(state='running',started_at=1795866000,last_success_at=1795867000,last_poll_at=1795867000,platforms=diag,result={'failed':0,'platforms':{'boss':{'status':'ok'}}}),'heartbeat-state.json':{},'monitor-policy.json':{'cutoff':0,'activated_at':1,'backfill_hours':24}}.items():
 (h/name).write_text(json.dumps(data))
(h/'own-tasks').mkdir()
(h/'own-tasks'/'delivery.json').write_text(json.dumps({'id':'synthetic','status':'completed','pid':123}))
'''
            subprocess.run([sys.executable,'-c',seed],cwd=base,env=oldenv,check=True,capture_output=True)
            probe=(Path(__file__).with_name('state_projection_probe.py')).read_text()
            before=json.loads(subprocess.check_output([sys.executable,'-c',probe],cwd=base,env=oldenv))
            newenv={**oldenv,'PYTHONPATH':str(ROOT/'mcp')}
            subprocess.run([sys.executable,'-c','from job_agent.store import Store; Store(migrate=True).close()'],cwd=ROOT,env=newenv,check=True,capture_output=True)
            after=json.loads(subprocess.check_output([sys.executable,'-c',probe],cwd=ROOT,env=newenv))
            # Compare the same configuration contract: #104 adds greeting
            # defaults after this fixed v6 baseline. Normalize only config via
            # today's validated parser; all runtime/business projections stay exact.
            from job_agent.config import Settings
            for projection in (before,after):
                projection['status']['job_agent'] = Settings.model_validate(projection['status']['job_agent']).model_dump(mode='json')
            # #115 intentionally adds accounting and neutralizes the default
            # monitor label. Compare unchanged business projections exactly.
            self.assertEqual(before['status']['monitor']['service'],'com.bmdy6.job-agent-monitor')
            self.assertEqual(after['status']['monitor']['service'],'com.job-agent.monitor')
            before['status']['monitor']['service']=after['status']['monitor']['service']
            for section in ('funnel_stats','dashboard'):
                self.assertIn('llm_usage',after[section])
                after[section].pop('llm_usage')
            self.assertIn('llm_usage',after['dashboard']['funnel'])
            after['dashboard']['funnel'].pop('llm_usage')
            import re
            after['dashboard_html']=re.sub(r'<section><h2>模型调用与费用</h2>.*?</section>','',after['dashboard_html'])
            for key in before:
                self.assertEqual(before[key],after[key],key)
            # Optional local evidence contains synthetic data only.
            evidence=os.environ.get('JOB_AGENT_PROJECTION_EVIDENCE')
            if evidence:
                target=Path(evidence);target.mkdir(parents=True,exist_ok=True)
                for name,data in [('before',before),('after',after)]:
                    (target/(name+'.json')).write_text(json.dumps(data,ensure_ascii=False,indent=2))
