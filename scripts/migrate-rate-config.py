#!/usr/bin/env python3
"""Preview conservative #46 migration; --apply backs up before atomic replacement."""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import yaml

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'mcp'))
from config import Settings


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
    parser.add_argument('--config',type=Path,default=Path.home()/'.job-agent/config.yaml')
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
    backup=args.config.with_name(args.config.name+'.backup-'+datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
    shutil.copy2(args.config,backup)
    os.chmod(backup,0o600)
    if backup.read_bytes()!=original:raise RuntimeError('Config changed during backup')
    fd,temp=tempfile.mkstemp(prefix='.rate-migrate-',dir=args.config.parent)
    try:
        with os.fdopen(fd,'w') as stream:
            yaml.safe_dump(updated,stream,allow_unicode=True,sort_keys=False)
            stream.flush();os.fsync(stream.fileno())
        if args.config.read_bytes()!=original:raise RuntimeError('Config changed during migration')
        os.replace(temp,args.config)
        if yaml.safe_load(args.config.read_bytes())!=updated:raise RuntimeError('Config readback mismatch')
    finally:
        if os.path.exists(temp):os.unlink(temp)
    print(json.dumps({'changed':True,'backup':str(backup)},ensure_ascii=False))


if __name__=='__main__':
    try:main()
    except Exception as error:
        print('migration failed error_type='+type(error).__name__,file=sys.stderr)
        raise SystemExit(1)
