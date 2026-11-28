"""Bounded local prompt loading; only known profile tokens are substituted."""
import json
from pathlib import Path
import re
from job_agent.config import job_agent_home

DEFAULT_DIR = Path(__file__).resolve().parents[1]/'prompts'


def vocabulary():
    return json.loads((DEFAULT_DIR/'vocabulary.json').read_text(encoding='utf-8'))


def substitute(text, profile):
    values=profile.model_dump()
    values['availability_clause']='，'+profile.availability if profile.availability else ''
    values['graduation_clause']='，'+profile.graduation if profile.graduation else ''
    return re.sub(r'\{(name|role|availability|graduation|availability_clause|graduation_clause)\}',
                  lambda m:values[m[1]],text)


def render_prompt(name, config=None):
    if config is None:
        from job_agent.config import load_config
        config=load_config()
    configured=getattr(config.prompts,name)
    if configured:
        path=Path(configured.replace('$JOB_AGENT_HOME',str(job_agent_home()))).expanduser()
    else:
        override=job_agent_home()/'prompts'/(name+'.md')
        path=override if override.exists() else DEFAULT_DIR/(name+'.md')
    with path.open('r',encoding='utf-8') as stream:
        text=stream.read(65537)
    if not text.strip() or len(text)>65536:
        raise ValueError('invalid_prompt_file')
    return substitute(text.strip(),config.profile)
