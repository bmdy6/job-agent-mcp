#!/usr/bin/env python3
"""Run external automation (including resume sync) under the shared platform lock."""
import argparse
from pathlib import Path
import subprocess
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'mcp'))
from job_agent.browser.platform_lock import platform_lock, PlatformBusy


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('platform', choices=('boss','zhilian','liepin','51job'))
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not command:
        parser.error('command required')
    try:
        with platform_lock(args.platform) as fd:
            # Child retains ownership if the wrapper is interrupted or killed.
            return subprocess.call(command, pass_fds=(fd,))
    except PlatformBusy:
        print('platform_busy', file=sys.stderr)
        return 75


if __name__ == '__main__':
    sys.exit(main())
