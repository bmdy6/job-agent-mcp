"""Collection entry points for the owned MCP and CLI tools."""
import fcntl
import os
from pathlib import Path
import subprocess
import sys
from job_agent.config import job_agent_home, load_config
from job_agent.collect.runtime import select_platforms
from job_agent.store import Store
from job_agent.store.collection import CollectionRepository


def _running(path):
    fd = os.open(str(path)+'.collect.lock', os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return False
        except BlockingIOError:
            return True
    finally:
        os.close(fd)


def collection_status():
    home = job_agent_home()
    path = home/'jobs.sqlite3'
    if not path.exists():
        return {'running': False, 'pages_today': {}, 'cursors': [], 'errors': {}, 'filtered': 0}
    config = load_config()
    with Store(path) as store:
        repo = CollectionRepository(store, timezone=config.collect.timezone)
        return {**repo.status(), 'running': _running(path)}


def check_collection(platforms=None):
    """Configuration/status check only; does not open platform pages."""
    config = load_config()
    selected = select_platforms(config.collect, platforms)
    messages = []
    if not selected:
        messages.append('No enabled collection platforms')
    if not config.collect.keywords or not config.collect.cities:
        messages.append('Collection keywords and cities are required')
    state = collection_status()
    if state['running']:
        messages.append('Collection is already running')
    return {**state, 'ok': not messages, 'messages': messages, 'platforms': selected}


def start_collection(platforms=None):
    config = load_config()
    selected = select_platforms(config.collect, platforms)
    if not config.collect.keywords or not config.collect.cities:
        return {'started': False, 'reason': 'Collection keywords and cities are required'}
    if not selected:
        return {'started': False, 'reason': 'No enabled collection platforms'}
    home = job_agent_home()
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    if _running(home/'jobs.sqlite3'):
        return {'started': False, 'running': True}
    args = [sys.executable, '-m', 'job_agent.cli', 'collect', '--once']
    for platform in selected:
        args.extend(['--platform', platform])
    fd = os.open(home/'collect.log', os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
    try:
        process = subprocess.Popen(args, cwd=Path(__file__).resolve().parents[2],
                                   stdin=subprocess.DEVNULL, stdout=fd, stderr=fd,
                                   start_new_session=True)
    finally:
        os.close(fd)
    return {'started': True, 'pid': process.pid, 'platforms': selected}
