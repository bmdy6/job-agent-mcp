#!/usr/bin/env python3
"""Read-only home inventory; generate an archive proposal, never execute it.

Unknown files, symlinks and runtime/evidence/backup directories stay in use.
Only logs/ and bin/ are expanded; no private file bodies or databases are read.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import stat

TEMP_SCRIPTS = {'wk.sh', 'dep.sh', 'night-deploy.sh', 'apply-smallco.sh'}
LABELS = {'in_use': '在用（未知项保守保留）', 'archive': '可以归档', 'obsolete': '可以删除（本次仍只归档）'}


def fingerprint(path):
    info = path.lstat()
    return dict(dev=info.st_dev, ino=info.st_ino, size=info.st_size,
                mtime_ns=info.st_mtime_ns, mode=info.st_mode)


def classify(relative, mode):
    name = Path(relative).name
    if not stat.S_ISREG(mode):
        return 'in_use', '目录/软链接保留；不跟随软链接'
    if relative == 'logs/import-bosshunter.log' or re.fullmatch(r'jobs\.sqlite3\.collect-int-.+\.bak', relative):
        return 'obsolete', '已退役 BossHunter/旧采集迁移残留；仅移入归档，不删除'
    if relative in TEMP_SCRIPTS:
        return 'archive', '一次性脚本；复用操作已由 settings/deploy.py 替代'
    if '/' not in relative and (re.match(r'(?:config\.yaml|daily\.yaml)(?:\.(?:bak|backup|pre)[.-])', name)
                               or re.fullmatch(r'config\.before-live-.+\.yaml', name)
                               or name == 'config.yaml.smallco-pending'
                               or re.match(r'daily-last-run\.json\.bak-', name)):
        return 'archive', '旧配置/状态备份或未应用的暂存版本，归档前由审查者确认'
    if '/' not in relative and (re.fullmatch(r'(?:manual|deploy)-.+\.log', name) or name == 'apply-smallco.log'):
        return 'archive', '一次性手动/部署日志'
    if relative.startswith('bin/') and re.search(r'\.bak(?:[.-]|$)', name):
        return 'archive', '旧启动脚本备份'
    return 'in_use', '运行数据/日志/锁/证据，或用途未证实；保守保留'


def inventory(home):
    home = Path(home).expanduser().resolve()
    if not home.is_dir():
        raise ValueError('Inventory home must already exist')
    rows = []
    def visit(directory):
        for path in sorted(directory.iterdir()):
            relative = path.relative_to(home).as_posix()
            info = fingerprint(path)
            category, reason = classify(relative, info['mode'])
            rows.append(dict(path=relative, category=category, reason=reason, fingerprint=info))
            if relative in {'logs', 'bin'} and stat.S_ISDIR(info['mode']):
                visit(path)
    visit(home)
    return dict(home=str(home), scanned_at=datetime.now(timezone.utc).isoformat(), entries=rows)


def markdown(report):
    out = ['# 生产目录只读清单', '', '扫描时间：'+report['scanned_at'], '',
           '仅按文件名和 lstat 元数据分类，未读配置、日志正文或数据库。目录按一项汇总；仅展开 logs/、bin/。',
           '归档脚本默认预览，需 Claude 审核后显式 --execute；两个候选类别均移入 backups/archive-日期/，不删除。', '']
    for category, label in LABELS.items():
        rows = [r for r in report['entries'] if r['category'] == category]
        out += ['## '+label+'（'+str(len(rows))+' 项）', '', '| 相对路径 | 原因 |', '| --- | --- |']
        out += ['| `'+r['path'].replace('|', '\\|')+'` | '+r['reason']+' |' for r in rows]
        out.append('')
    return '\n'.join(out)


def archive_script(report, date=None):
    date = date or datetime.now().strftime('%Y%m%d-%H%M%S')
    if not re.fullmatch(r'\d{8}(?:-\d{6})?', date):
        raise ValueError('Invalid archive date')
    proposal = dict(home=report['home'], archive='archive-'+date,
                    files=[r for r in report['entries'] if r['category'] in {'archive', 'obsolete'}])
    # Self-contained generated artifact: no imports from a moving checkout.
    return '''#!/usr/bin/env python3
"""Generated archive proposal. Default: dry run. Claude must review before --execute."""
import argparse
import json
import os
from pathlib import Path
import stat

PROPOSAL = json.loads(''' + repr(json.dumps(proposal, ensure_ascii=False)) + ''')

def fingerprint(path):
    s = path.lstat()
    return dict(dev=s.st_dev, ino=s.st_ino, size=s.st_size, mtime_ns=s.st_mtime_ns, mode=s.st_mode)

def real_parents(path, home):
    for parent in (path, *path.parents):
        if parent == home:
            break
        if parent.is_symlink():
            raise ValueError('Symlink refused; regenerate inventory')

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    home = Path(PROPOSAL['home'])
    if home.is_symlink() or home.resolve() != home:
        raise ValueError('Home changed; regenerate inventory')
    backup = home/'backups'
    target = backup/PROPOSAL['archive']
    real_parents(target, home)
    if target.exists():
        raise ValueError('Archive already exists; never overwrite')
    # Check every candidate before moving even the first file.
    for row in PROPOSAL['files']:
        rel = Path(row['path'])
        if rel.is_absolute() or '..' in rel.parts:
            raise ValueError('Invalid relative path')
        source = home/rel
        real_parents(source, home)
        if not stat.S_ISREG(source.lstat().st_mode) or fingerprint(source) != row['fingerprint']:
            raise ValueError('Candidate changed; regenerate inventory')
    if not args.execute:
        print(json.dumps(dict(executed=False, archive=str(target), files=[r['path'] for r in PROPOSAL['files']])))
        return
    backup.mkdir(mode=0o700, exist_ok=True)
    target.mkdir(mode=0o700, exist_ok=False)
    for row in PROPOSAL['files']:
        source, dest = home/row['path'], target/row['path']
        real_parents(source, home)
        if fingerprint(source) != row['fingerprint']:
            raise ValueError('Candidate changed during archive; previous moves remain archived')
        dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if dest.exists() or dest.is_symlink():
            raise ValueError('Destination exists; never overwrite')
        # Same filesystem: link exclusively, then unlink the original name.
        # If interrupted between them, both names preserve the original bytes.
        os.link(source, dest, follow_symlinks=False)
        source.unlink()
    print(json.dumps(dict(executed=True, archive=str(target), moved=len(PROPOSAL['files']))))

if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError) as error:
        raise SystemExit('Archive stopped: '+str(error))
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, default=Path(os.environ.get('JOB_AGENT_HOME', '~/.job-agent')).expanduser())
    parser.add_argument('--report', type=Path, help='Write Markdown outside the scanned home; default stdout')
    parser.add_argument('--archive-script', type=Path, help='Generate an unexecuted Python archive script outside home')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()
    home = args.home.expanduser().resolve()
    for output in (args.report, args.archive_script):
        if output and (output.expanduser().resolve().is_relative_to(home)
                       or output.expanduser().resolve().is_relative_to((Path.home()/'.job-agent').resolve())):
            parser.error('Outputs must stay outside production and the scanned home')
    report = inventory(home)
    rendered = json.dumps(report, ensure_ascii=False, indent=2) if args.json else markdown(report)
    if args.report:
        args.report.expanduser().write_text(rendered+'\n', encoding='utf-8')
    else:
        print(rendered)
    if args.archive_script:
        args.archive_script.expanduser().write_text(archive_script(report), encoding='utf-8')


if __name__ == '__main__':
    main()
