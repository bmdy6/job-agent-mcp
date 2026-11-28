"""Offline comparison against actual 9db783d code, with optional private snapshots.

JOB_AGENT_COMPAT_SNAPSHOT may point to a previously copied private directory.
The test never reads production home and never prints candidate text or config.
"""
import ast
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
import yaml

ROOT=Path(__file__).resolve().parents[2]
BASE='9db783d'
PROBE='''import json
from unittest.mock import patch
from job_agent.config import load_config
from job_agent.service.greeting_generation import generate
from job_agent.service.scoring import build_request
from job_agent.platforms.greeting import prepare_greeting
cfg=load_config()
job=dict(platform='boss',platform_job_id='synthetic',title='RAG开发',jd='知识问答')
with patch('job_agent.service.scoring.resolve_key',return_value='fixture'), patch('job_agent.service.scoring.request_completion',side_effect=RuntimeError('offline fixture')) as call:
 text,trace=generate(job,cfg,run_id='fixture')
 payload=call.call_args.args[0] if call.called else None
score=build_request(job,{'resume':'synthetic','experiences':[]},cfg.scoring)
print(json.dumps(dict(payload=payload,identity=text.split('\\n\\n')[0],text=text,score=score,
 rendered={p:prepare_greeting(p,text,paragraph_separator='') for p in cfg.engine.platforms}),ensure_ascii=False))
'''


def original_constants(base):
    constants={}
    for file,keys in (('greeting_generation.py',{'SYSTEM','GENERIC_FALLBACK'}),('greeting_templates.py',{'DEFAULTS'}),('scoring.py',{'SYSTEM'})):
        tree=ast.parse((base/'mcp/job_agent/service'/file).read_text())
        for node in tree.body:
            if isinstance(node,ast.Assign) and isinstance(node.targets[0],ast.Name) and node.targets[0].id in keys:
                key=node.targets[0].id
                value='\n\n'.join(ast.literal_eval(node.value.args[0])) if key=='GENERIC_FALLBACK' else ast.literal_eval(node.value)
                constants['scoring' if file=='scoring.py' else key]=value
    return constants


class ProductionCompatibilityTest(unittest.TestCase):
    def test_payload_identity_fallback_and_rendering_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            work=Path(directory);base=work/'base';base.mkdir();home=work/'home';home.mkdir()
            archive=subprocess.check_output(['git','-C',str(ROOT),'archive',BASE])
            with tarfile.open(fileobj=io.BytesIO(archive)) as stream:stream.extractall(base,filter='data')
            constants=original_constants(base)
            snapshot=os.environ.get('JOB_AGENT_COMPAT_SNAPSHOT')
            if snapshot:
                source=Path(snapshot).resolve()
                self.assertNotEqual(source,(Path.home()/'.job-agent').resolve())
                for name in ('config.yaml','daily.yaml','greeting-templates.yaml','greeting-facts.yaml'):
                    if (source/name).is_file():shutil.copyfile(source/name,home/name)
            else:
                (home/'config.yaml').write_text(yaml.safe_dump({'greetings':{'generate':{'enabled':True,'facts':['之前做过软件开发。']}},
                    'engine':{'platforms':{'boss':{'greeting_max_length':1000}}}}))
            env={**os.environ,'JOB_AGENT_HOME':str(home),'PYTHONPATH':str(base/'mcp')}
            before=json.loads(subprocess.check_output([sys.executable,'-c',PROBE],env=env,cwd=base))
            self.assertIsNotNone(before['payload'])
            # Derive the exact old identity, including punctuation, from the
            # versioned baseline. Personal values stay confined to test fixtures.
            identity=constants['GENERIC_FALLBACK'].split('\n\n')[0]
            name,role,availability=identity.removeprefix('你好，我是').removesuffix('。').split('，',2)
            (home/'prompts').mkdir()
            for name_,value in (('greeting',constants['SYSTEM']),('fallback',constants['GENERIC_FALLBACK']),('scoring',constants['scoring'])):
                (home/'prompts'/(name_+'.md')).write_text(value)
            if not (home/'greeting-templates.yaml').exists():
                (home/'greeting-templates.yaml').write_text(yaml.safe_dump({'version':1,'directions':constants['DEFAULTS']},allow_unicode=True))
            changes={'profile.name':name,'profile.role':role,'profile.availability':availability,
                'deploy.label_prefix':'com.bmdy6','prompts.greeting':'$JOB_AGENT_HOME/prompts/greeting.md',
                'prompts.fallback':'$JOB_AGENT_HOME/prompts/fallback.md','prompts.scoring':'$JOB_AGENT_HOME/prompts/scoring.md'}
            env['PYTHONPATH']=str(ROOT/'mcp')
            command=[sys.executable,'-m','job_agent.cli','settings','set']
            for key,value in changes.items():command.extend([key,json.dumps(value,ensure_ascii=False)])
            command.extend(['--confirm','--reason','offline compatibility fixture'])
            result=subprocess.run(command,env=env,cwd=ROOT,capture_output=True)
            self.assertEqual(result.returncode,0,'Migration CLI failed (private details suppressed)')
            cached=json.loads(subprocess.check_output([sys.executable,'-c',PROBE],env=env,cwd=ROOT))
            self.assertTrue(cached['text']==before['text'],'cached text differs (private details suppressed)')
            self.assertIsNone(cached['payload'])
            (home/'greeting-generation.sqlite3').unlink()  # temporary fixture only: compare a fresh dispatch
            after=json.loads(subprocess.check_output([sys.executable,'-c',PROBE],env=env,cwd=ROOT))
            for key in before:self.assertTrue(before[key]==after[key],key+' differs (private details suppressed)')
            from unittest.mock import patch
            with patch_home(home):
                from job_agent.service.monitor import service_label,plist_path
                self.assertEqual(service_label(),'com.bmdy6.job-agent-monitor')
                self.assertEqual(plist_path().name,'com.bmdy6.job-agent-monitor.plist')


def patch_home(home):
    from unittest.mock import patch
    return patch.dict(os.environ,JOB_AGENT_HOME=str(home))
