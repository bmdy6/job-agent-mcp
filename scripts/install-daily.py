#!/usr/bin/env python3
"""Legacy plist helper; executable installation is retired in favor of deploy.py."""
from pathlib import Path
import subprocess

LABEL='com.job-agent.daily'


def plist(runtime, python, home, hour, minute, cap=None, collection_pages=None):
    args=[str(python),str(Path(runtime)/'scripts/daily-launch.py')]
    if cap is not None:
        args += ['--max-per-platform',str(cap)]
    if collection_pages is not None:
        args += ['--collection-page-limit',str(collection_pages)]
    return dict(Label=LABEL, ProgramArguments=args,
                EnvironmentVariables={'JOB_AGENT_HOME':str(home)},
                WorkingDirectory=str(runtime),
                StartCalendarInterval={'Hour':hour,'Minute':minute},
                RunAtLoad=True, StartInterval=1800,
                ProcessType='Background',
                StandardOutPath=str(home/'daily.log'),StandardErrorPath=str(home/'daily.log'))


def main():
    raise SystemExit('Retired installer: use python3 scripts/deploy.py; review docs/deploy.md first')


if __name__=='__main__':
    main()
