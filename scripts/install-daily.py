#!/usr/bin/env python3
"""Install a calendar-only LaunchAgent; no RunAtLoad and no hidden immediate send."""
import argparse
import os
from pathlib import Path
import plistlib
import subprocess
import sys

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
                ProcessType='Background',
                StandardOutPath=str(home/'daily.log'),StandardErrorPath=str(home/'daily.log'))


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--runtime',type=Path,required=True,help='Stable installation, not a disposable worktree')
    p.add_argument('--python',type=Path,default=Path(sys.executable))
    p.add_argument('--hour',type=int)
    p.add_argument('--minute',type=int)
    p.add_argument('--max-per-platform',type=int,choices=(1,2,3),help='Temporary real acceptance cap')
    p.add_argument('--collection-page-limit',type=int,choices=range(1,21))
    p.add_argument('--uninstall',action='store_true')
    args=p.parse_args()
    home=Path(os.environ.get('JOB_AGENT_HOME','~/.job-agent')).expanduser().resolve()
    path=Path.home()/'Library/LaunchAgents'/f'{LABEL}.plist'
    domain=f'gui/{os.getuid()}'
    if args.uninstall:
        subprocess.run(['launchctl','bootout',domain+'/'+LABEL],check=False,capture_output=True)
        path.unlink(missing_ok=True)
        return
    import yaml
    config=yaml.safe_load((home/'config.yaml').read_text()) or {}
    extra_path=home/'daily.yaml'
    extra=yaml.safe_load(extra_path.read_text()) if extra_path.exists() else {}
    cfg={**(extra or {}).get('daily',{}),**config.get('daily',{})}
    hour=args.hour if args.hour is not None else cfg.get('hour',8)
    minute=args.minute if args.minute is not None else cfg.get('minute',0)
    if not 0<=hour<=23 or not 0<=minute<=59:
        p.error('Invalid hour/minute')
    runtime=args.runtime.expanduser().resolve(); python=args.python.expanduser().absolute()
    if not python.is_file() or not (runtime/'scripts/daily-launch.py').is_file():
        p.error('Runtime/python missing')
    home.mkdir(parents=True,exist_ok=True,mode=0o700)
    for name in ('daily.log',):
        fd=os.open(home/name,os.O_CREAT|os.O_APPEND|os.O_WRONLY,0o600);os.close(fd)
    path.parent.mkdir(parents=True,exist_ok=True)
    data=plist(runtime,python,home,hour,minute,args.max_per_platform,args.collection_page_limit)
    path.write_bytes(plistlib.dumps(data)); path.chmod(0o600)
    subprocess.run(['plutil','-lint',str(path)],check=True)
    subprocess.run(['launchctl','bootout',domain+'/'+LABEL],check=False,capture_output=True)
    subprocess.run(['launchctl','bootstrap',domain,str(path)],check=True)
    print(f'{LABEL}: {hour:02d}:{minute:02d}; {path}')


if __name__=='__main__':
    main()
