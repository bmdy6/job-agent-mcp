"""Few-shot styles are examples, never runtime string substitution."""
from pathlib import Path
import yaml
from job_agent.config import job_agent_home

from job_agent.service.prompt_files import DEFAULT_DIR, substitute

DEFAULTS = yaml.safe_load((DEFAULT_DIR/'styles.yaml').read_text(encoding='utf-8'))['directions']

def select_template(job, settings, profile=None):
    configured = settings.templates_path
    path = Path(configured.replace('$JOB_AGENT_HOME',str(job_agent_home()))).expanduser()
    try:
        with path.open('rb') as stream:
            raw = stream.read(64*1024+1)
        if len(raw) > 64*1024:
            raise ValueError('oversized')
        data = yaml.safe_load(raw)
        if not isinstance(data,dict) or data.get('version') != 1 or not isinstance(data.get('directions'),dict) or not {'rag', 'agent', 'automation'} <= set(data['directions']) <= set(DEFAULTS):
            raise ValueError('schema')
        templates = data['directions']
        for value in templates.values():
            if (not isinstance(value,dict) or set(value) != {'tags','example'}
                    or not isinstance(value['tags'],list) or not value['tags']
                    or not isinstance(value['example'],list) or len(value['example']) not in {3, 4}
                    or any(not isinstance(s,str) or not s.strip() or len(s)>500 for s in value['tags']+value['example'])):
                raise ValueError('schema')
    except FileNotFoundError:
        templates = DEFAULTS
    except (OSError, ValueError, yaml.YAMLError):
        raise ValueError('invalid_greeting_templates') from None
    if profile is None:
        from job_agent.config import load_config
        profile=load_config().profile
    context = (str(job.get('title') or '')+' '+str(job.get('jd') or '')).casefold()
    direction = max(templates, key=lambda name:sum(context.count(t.casefold()) for t in templates[name]['tags']))
    return direction, [substitute(s, profile) for s in templates[direction]['example']]
