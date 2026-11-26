#!/usr/bin/env python3
"""launchd entry for the heartbeat: load Telegram secrets like the monitor launcher, run once."""
import importlib.util
import os
from pathlib import Path
import sys


def main():
    runtime = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location('monitor_launch', runtime / 'scripts' / 'monitor-launch.py')
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    # Reuse the monitor launcher's vault lookup, but exec the heartbeat command instead.
    real_execve = os.execve
    def execve(executable, argv, env):
        real_execve(executable, [executable, 'heartbeat', '--once'], env)
    launcher.os.execve = execve
    launcher.main()


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('heartbeat launcher failed error_type=' + type(error).__name__, file=sys.stderr)
        sys.exit(1)
