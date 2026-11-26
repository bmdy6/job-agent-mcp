#!/usr/bin/env python3
"""Stable launchd entry point. Optional secrets file contains environment values.

No vault-specific dependency: owners can provide a chmod-600 daily.env.json.
Environment wins over that file. Secret values never enter arguments or logs.
"""
import json
import os
from pathlib import Path
import signal
import sys

root=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(root/'mcp'))


def main():
    home=Path(os.environ.get('JOB_AGENT_HOME','~/.job-agent')).expanduser().resolve()
    secret_file=home/'daily.env.json'
    if secret_file.exists():
        if secret_file.stat().st_mode & 0o077:
            raise ValueError('daily.env.json must be private (chmod 600)')
        for name,value in json.loads(secret_file.read_text()).items():
            os.environ.setdefault(name,value)
    def stop(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM,stop)
    from cli import main as cli
    return cli(['daily','--once',*sys.argv[1:]])


if __name__=='__main__':
    raise SystemExit(main())
