"""Control the single user launchd monitor, independently of delivery backend."""
import os
from pathlib import Path
import re
import plistlib
import time
import subprocess
from job_agent.config import job_agent_home, load_config
from job_agent.store.state import read_run

def service_label():
    prefix=load_config().deploy.label_prefix
    return prefix + ('.job-agent-monitor' if prefix!='com.job-agent' else '.monitor')


# Compatibility export; runtime operations always resolve current settings.
LABEL = 'com.job-agent.monitor'


def plist_path():
    return Path.home() / 'Library' / 'LaunchAgents' / (service_label() + '.plist')


def installed():
    try:
        data = plistlib.loads(plist_path().read_bytes())
        configured = data.get('EnvironmentVariables', {}).get('JOB_AGENT_HOME')
        return isinstance(configured, str) and Path(configured).expanduser().resolve() == job_agent_home()
    except (OSError, ValueError, plistlib.InvalidFileException):
        return False


def target():
    return f'gui/{os.getuid()}/{service_label()}'


def command(*args):
    try:
        return subprocess.run(['/bin/launchctl', *args], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError('launchctl unavailable; monitor requires macOS user launchd') from None


def status():
    state = read_run('monitor') or {}
    from job_agent.service.deployment import status as deployment_status
    result = {**state, 'service': service_label(), 'installed': installed(), 'running': False, 'pid': None}
    result['deployment'] = deployment_status()
    if result['installed']:
        proc = command('print', target())
        result['loaded'] = proc.returncode == 0
        match = re.search(r'^\s*pid = (\d+)\s*$', proc.stdout, re.M)
        if proc.returncode == 0 and match:
            result.update(running=True, pid=int(match[1]))
    return result


def start():
    if not installed():
        raise ValueError('Install monitor first: python3 scripts/deploy.py (see docs/deploy.md)')
    current = status()
    if not current.get('loaded'):
        for attempt in range(15):
            proc = command('bootstrap', f'gui/{os.getuid()}', str(plist_path()))
            if proc.returncode == 0:
                break
            if proc.returncode != 5 or status().get('loaded') or attempt == 14:
                raise RuntimeError('Could not bootstrap job-agent monitor')
            time.sleep(1)
    elif current['running']:
        return current
    else:
        proc = command('kickstart', target())
    if proc.returncode:
        raise RuntimeError('Could not start job-agent monitor; inspect launchd and monitor.log')
    return status()


def stop():
    current = status()
    if not current.get('loaded'):
        return {'stopped': False, 'reason': 'No loaded monitor service'}
    if command('bootout', target()).returncode:
        raise RuntimeError('Could not stop job-agent monitor')
    if current.get('pid'):
        deadline = time.monotonic() + 45
        while True:
            try:
                os.kill(current['pid'], 0)
            except ProcessLookupError:
                break
            if time.monotonic() >= deadline:
                return {'stopped': True, 'service': service_label(), 'state': 'stopping'}
            time.sleep(.2)
    return {'stopped': True, 'service': service_label()}
