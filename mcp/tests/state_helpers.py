"""SQLite fixtures for contracts formerly asserted through private state files."""
import json
from pathlib import Path
import sqlite3
from contextlib import closing
from job_agent.store import Store


def read_state_file(path):
    path = Path(path)
    with Store(path.parent/'jobs.sqlite3') as store:
        if path.name == 'activity.jsonl':
            return ''.join(json.dumps(row, ensure_ascii=False)+'\n' for row in store.events())
        if path.name == 'notify-outbox.jsonl':
            return ''.join(json.dumps(dict(id=r['id'],channel=r['channel'],title=r['title'],text=r['body'],url=r['url']))+'\n' for r in store.notifications(pending=True) if r['kind']=='user')
        if path.name == 'notified.jsonl':
            # Receipt projection contains no original message content.
            return json.dumps([{k:r[k] for k in ('dedupe_key','channel','status')} for r in store.notifications() if r['kind'] in {'reply','alert'}])
        if path.name == 'monitor-state.json':
            return json.dumps(store.monitor_state())
        return json.dumps(store.latest_run({'monitor-runtime.json':'monitor','heartbeat-state.json':'heartbeat','daily-last-run.json':'daily'}[path.name]))


def write_state_file(path, text):
    path = Path(path)
    value = json.loads(text)
    with Store(path.parent/'jobs.sqlite3') as store:
        if path.name == 'monitor-state.json':
            store.save_monitor_state(value)
        elif path.name == 'monitor-policy.json':
            store.monitor_policy(value)
        else:
            store.save_run({'monitor-runtime.json':'monitor','heartbeat-state.json':'heartbeat','daily-last-run.json':'daily'}[path.name], value)


def business_snapshot(path):
    with closing(sqlite3.connect(path)) as db:
        return tuple(line for line in db.iterdump() if not line.startswith('INSERT INTO "events"'))
