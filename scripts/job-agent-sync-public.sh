#!/bin/zsh
# Export private main; process handoffs never enter the public snapshot.
# --export-only builds a new destination without fetch, commits or publication.
set -eu
set -o pipefail
SRC=${JOB_AGENT_PRIVATE_REPO:-${0:A:h:h}}
DST=${JOB_AGENT_PUBLIC_REPO:-$HOME/项目/Personal/bmdy6/job-agent-mcp}
MODE=${1:-sync}
[[ $MODE == sync || $MODE == --export-only ]] || { print -u2 'usage: job-agent-sync-public.sh [--export-only]'; exit 2; }
[[ ${SRC:A} != ${DST:A} && ${DST:A} != / && ${DST:A} != $HOME ]] || { print -u2 'unsafe export destination'; exit 2; }
[[ ${SRC:A}/ != ${DST:A}/* && ${DST:A}/ != ${SRC:A}/* ]] || { print -u2 'nested export paths refused'; exit 2; }
if [[ $MODE == --export-only ]]; then
  [[ ! -e $DST ]] || { print -u2 'export destination must not exist'; exit 2; }
else
  git -C "$SRC" fetch -q origin
fi
SHA=$(git -C "$SRC" rev-parse --short origin/main)
mkdir -p "$DST"
cd "$DST"
if [[ $MODE != --export-only ]]; then
  [[ -d .git ]] || git init -q -b main
  find . -mindepth 1 -maxdepth 1 ! -name .git -exec rm -rf {} +
fi
git -C "$SRC" archive origin/main | tar -x -C "$DST"
# Internal process notes and root task artifacts stay private.
rm -rf docs/superpowers docs/tasks docs/migrations shots DONE
for artifact in result*.md(N); do rm -f -- "$artifact"; done
if [[ $MODE != --export-only ]]; then
  cp "${JOB_AGENT_PUBLIC_LICENSE:-$HOME/项目/bmdy6/scripts/job-agent-LICENSE}" LICENSE
fi
python3 - <<'PY'
import re
from pathlib import Path
for p in Path('.').rglob('*.md'):
    if '.git' in p.parts:
        continue
    s = p.read_text()
    t = re.sub(r'\[([^\]]+)\]\(https://github\.com/bmdy6/job-agent/(?:issues|pull)/[^)]*\)', r'\1', s)
    t = re.sub(r'(?:\s*·\s*)?\[11-26 路线\]\([^)]*\)', '', t)
    t = t.replace('github.com/bmdy6/job-agent.git', 'github.com/bmdy6/job-agent-mcp.git')
    t = t.replace('cd job-agent\n', 'cd job-agent-mcp\n')
    t = re.sub(r'\s*·\s*11-26 路线', '', t)
    t = re.sub(r'（#\d+）', '', t)
    t = re.sub(r'：#\d+ [^。\n]*。', '。', t)
    if t != s:
        p.write_text(t)
PY
if [[ $MODE != --export-only ]]; then
  git add -A
  git -c user.name=bmdy6 -c user.email=318852220+bmdy6@users.noreply.github.com commit -q -m "Sync from private development repo ($SHA)" || print 'nothing to commit'
fi
print "exported $SHA"
