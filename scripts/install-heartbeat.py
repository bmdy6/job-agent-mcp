#!/usr/bin/env python3
"""Install a LaunchAgent that runs the heartbeat every N seconds from runtime/current.

It points at the stable runtime/current symlink, so deploys and rollbacks need no change here.
Use --dry-run to print the plist without installing it.
"""
import argparse
import os
from pathlib import Path
import plistlib
import subprocess

LABEL = 'com.bmdy6.job-agent-heartbeat'


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--home', type=Path, default=Path(os.environ.get('JOB_AGENT_HOME', '~/.job-agent')).expanduser())
    p.add_argument('--interval', type=int, default=300)
    p.add_argument('--agents-dir', type=Path, default=Path('~/Library/LaunchAgents').expanduser())
    p.add_argument('--dry-run', action='store_true')
    args = p.parse_args()
    if args.interval < 60:
        p.error('--interval must be at least 60 seconds')
    current = args.home / 'runtime' / 'current'
    plist = {
        'Label': LABEL,
        'ProgramArguments': [str(current / '.venv/bin/python'), str(current / 'scripts/heartbeat-launch.py')],
        'StartInterval': args.interval,
        'RunAtLoad': True,
        'EnvironmentVariables': {'JOB_AGENT_HOME': str(args.home)},
        'StandardOutPath': str(args.home / 'heartbeat.log'),
        'StandardErrorPath': str(args.home / 'heartbeat.log'),
    }
    data = plistlib.dumps(plist)
    if args.dry_run:
        print(data.decode())
        return 0
    target = args.agents_dir / f'{LABEL}.plist'
    target.write_bytes(data)
    domain = f'gui/{os.getuid()}'
    subprocess.run(['launchctl', 'bootout', f'{domain}/{LABEL}'], capture_output=True)
    subprocess.run(['launchctl', 'bootstrap', domain, str(target)], check=True)
    print(f'installed {target}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
