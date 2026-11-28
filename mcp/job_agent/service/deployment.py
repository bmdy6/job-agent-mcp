"""Read-only deployment identity shared by setup_check, status and future TG /status."""
import json
from pathlib import Path
from job_agent.config import job_agent_home


def _manifest(runtime):
    try:
        data = json.loads((runtime/'deployment.json').read_text())
        return {'commit': data['commit'], 'schema_version': data['schema_version']}
    except (OSError, ValueError, KeyError, TypeError):
        return {'commit': None, 'schema_version': None}


def status():
    current = job_agent_home()/'runtime/current'
    executing = Path(__file__).resolve().parents[2]
    # Installed wheels live under <runtime>/.venv/lib/pythonX/site-packages/job_agent/service/.
    for parent in Path(__file__).resolve().parents:
        if (parent/'deployment.json').is_file():
            executing = parent
            break
    return {**_manifest(current), 'runtime': str(current.resolve()) if current.exists() else None,
            'executing_commit': _manifest(executing)['commit']}
