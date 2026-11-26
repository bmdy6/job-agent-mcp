#!/usr/bin/env python3
"""Install a committed revision in an immutable runtime, then replace legacy watch."""
import argparse
from datetime import datetime
import io
import os
from pathlib import Path
import plistlib
import shutil
import re
import subprocess
import tarfile
import time


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def bootstrap(domain, plist):
    """launchd can briefly reject bootstrap after bootout has returned."""
    for attempt in range(15):
        result = subprocess.run(['/bin/launchctl', 'bootstrap', domain, str(plist)], capture_output=True)
        if result.returncode == 0:
            return
        absent = subprocess.run(['/bin/launchctl', 'print', domain+'/com.bmdy6.job-agent-monitor'], capture_output=True).returncode != 0
        if result.returncode != 5 or not absent or attempt == 14:
            raise RuntimeError('Monitor bootstrap failed; inspect launchctl before retrying installation')
        time.sleep(1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--backup-dir', required=True, type=Path)
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    run('git', '-C', str(repo), 'diff', '--quiet', 'HEAD', '--')
    sha = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', '--short=12', 'HEAD'], text=True).strip()
    branch = subprocess.check_output(['git', '-C', str(repo), 'branch', '--show-current'], text=True).strip()
    home = Path(os.environ.get('JOB_AGENT_HOME', '~/.job-agent')).expanduser().resolve()
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    backup = args.backup_dir.expanduser().resolve() / ('monitor-' + datetime.now().strftime('%Y%m%d-%H%M%S'))
    backup.mkdir(parents=True, mode=0o700)
    agents = Path.home() / 'Library/LaunchAgents'
    agents.mkdir(parents=True, exist_ok=True)
    label = 'com.bmdy6.job-agent-monitor'
    plist = agents / (label + '.plist')
    old_plist = agents / 'com.bmdy6.job-agent-watch.plist'
    for source in (old_plist, home/'bin/watch.sh', plist, home/'config.yaml'):
        if source.exists():
            destination = backup / source.name
            shutil.copy2(source, destination)
            destination.chmod(0o600)
    runtime = home / 'runtime' / (('main-' if branch == 'main' else 'monitor-') + sha)
    if not runtime.exists():
        runtime.mkdir(parents=True, mode=0o700)
        archive = subprocess.check_output(['git', '-C', str(repo), 'archive', 'HEAD'])
        with tarfile.open(fileobj=io.BytesIO(archive)) as stream:
            stream.extractall(runtime, filter='data')
        run('uv', 'venv', str(runtime/'.venv'))
        run('uv', 'pip', 'install', '--python', str(runtime/'.venv/bin/python'), str(runtime/'mcp'))
    # Validate only monitor-owned settings. Never rewrite shared configuration:
    # collection/delivery may use newer fields or update it during installation.
    python = str(runtime/'.venv/bin/python')
    code = 'from monitor.config import load_settings; load_settings()'
    env = {**os.environ, 'JOB_AGENT_HOME': str(home)}
    run(python, '-c', code, env=env)
    # Stop the only prior notification service, after its plist and wrapper are safely backed up.
    domain = f'gui/{os.getuid()}'
    for service in ('com.bmdy6.job-agent-watch', label):
        target = domain + '/' + service
        current = subprocess.run(['/bin/launchctl', 'print', target], capture_output=True, text=True)
        if current.returncode == 0:
            match = re.search(r'^\s*pid = (\d+)\s*$', current.stdout, re.M)
            run('/bin/launchctl', 'bootout', target)
            if match:
                deadline = time.monotonic() + 45
                while True:
                    try:
                        os.kill(int(match[1]), 0)
                    except ProcessLookupError:
                        break
                    if time.monotonic() >= deadline:
                        raise RuntimeError('Previous monitor/watch process has not exited; installation stopped')
                    time.sleep(.2)
    old_plist.unlink(missing_ok=True)
    data = plistlib.loads((runtime/'examples/job-agent-monitor.plist').read_bytes())
    data['ProgramArguments'] = [python, str(runtime/'scripts/monitor-launch.py')]
    data['EnvironmentVariables']['JOB_AGENT_HOME'] = str(home)
    for key in ('StandardOutPath', 'StandardErrorPath'):
        data[key] = str(home/'monitor.log')
    plist.write_bytes(plistlib.dumps(data))
    plist.chmod(0o600)
    run('/usr/bin/plutil', '-lint', str(plist))
    bootstrap(domain, plist)
    print('runtime=' + str(runtime))
    print('backup=' + str(backup))
    print('service=' + label)


if __name__ == '__main__':
    main()
