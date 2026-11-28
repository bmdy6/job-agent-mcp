#!/usr/bin/env python3
"""Preview legacy rate defaults; --apply delegates to the audited settings writer."""
import argparse
import json
import os
from pathlib import Path
import sys
import yaml

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'mcp'))
from job_agent.config import Settings, job_agent_home
from job_agent.service.settings import update_settings


def migrate(data):
    import copy
    result=copy.deepcopy(data)
    for section in ('engine','collect'):
        options=result.get(section,{})
        for platform,values in options.get('platforms',{}).items():
            if section=='engine':
                values.setdefault('expected_account_type','any')
                if platform in {'boss','zhilian'}:
                    values.setdefault('daily_limit',{'boss':140,'zhilian':90}[platform])
            rate=values.setdefault('rate',{})
            defaults=dict(interval_seconds=[45,120],hourly_limit=20,rest_every=[8,15],rest_seconds=[300,900],
                          allowed_hours=['09:00','21:00'],timezone='Asia/Shanghai')
            for name,value in defaults.items():rate.setdefault(name,value)
    result.setdefault('collect',{}).setdefault('platform_timeout_seconds',600)
    Settings.model_validate(result)
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,default=job_agent_home()/'config.yaml')
    parser.add_argument('--apply',action='store_true',help='Only after all full-config readers are upgraded; backs up first')
    args=parser.parse_args()
    original=args.config.read_bytes()
    data=yaml.safe_load(original) or {}
    updated=migrate(data)
    if not args.apply:
        print(json.dumps({'apply':False,'platforms':{section:{p:v.get('rate') for p,v in updated.get(section,{}).get('platforms',{}).items()} for section in ('engine','collect')},
                          'platform_timeout_seconds':updated['collect']['platform_timeout_seconds']},ensure_ascii=False))
        return
    if updated==data:
        print(json.dumps({'changed':False}));return
    if args.config.name != 'config.yaml' or args.config.is_symlink():
        raise ValueError('Migration target must be a regular config.yaml')
    def differences(before, after, prefix=''):
        result = {}
        for name, value in after.items():
            path = prefix+'.'+name if prefix else name
            old = before.get(name) if isinstance(before, dict) else None
            if isinstance(value, dict):
                result.update(differences(old or {}, value, path))
            elif value != old:
                result[path] = value
        return result
    changes = differences(data, updated)
    previous_home = os.environ.get('JOB_AGENT_HOME')
    try:
        os.environ['JOB_AGENT_HOME'] = str(args.config.absolute().parent)
        result = update_settings(changes, confirm=True, source='cli:migrate-rate', reason='legacy rate defaults migration')
    finally:
        if previous_home is None:
            os.environ.pop('JOB_AGENT_HOME', None)
        else:
            os.environ['JOB_AGENT_HOME'] = previous_home
    print(json.dumps({'changed': result['applied'], 'history_id': result.get('history_id')}, ensure_ascii=False))



if __name__=='__main__':
    try:main()
    except Exception as error:
        print('migration failed error_type='+type(error).__name__,file=sys.stderr)
        raise SystemExit(1)
