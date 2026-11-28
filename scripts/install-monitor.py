#!/usr/bin/env python3
"""Legacy bootstrap helper; executable installation is retired in favor of deploy.py."""
import subprocess
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
    raise SystemExit('Retired installer: use python3 scripts/deploy.py; review docs/deploy.md first')


if __name__ == '__main__':
    main()
