import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from job_agent.store import Store


class StateV7Test(unittest.TestCase):
    def test_transactional_delivery_intent(self):
        with tempfile.TemporaryDirectory() as home, Store(Path(home)/'jobs.sqlite3') as s:
            jid = s.upsert_job(platform='boss', platform_job_id='fake')
            with self.assertRaises(RuntimeError):
                with s.transaction():
                    s.record_delivery(jid, status='sent', notifications=[dict(key='delivery:fake', channel='tg', title='result', text='synthetic')])
                    raise RuntimeError('crash')
            self.assertEqual(s.list_deliveries(), [])
            self.assertEqual(s.notifications(), [])
            s.record_delivery(jid, status='sent', notifications=[dict(key='delivery:fake', channel='tg', title='result', text='synthetic')])
            self.assertEqual(s.notifications()[0]['delivery_id'], s.list_deliveries()[0]['id'])

    def test_legacy_import_and_repeat(self):
        with tempfile.TemporaryDirectory() as home:
            root = Path(home)
            (root/'monitor-state.json').write_text(json.dumps({'boss': {'count': 2}}))
            (root/'monitor-runtime.json').write_text(json.dumps({'state': 'running', 'started_at': 123}))
            (root/'monitor-policy.json').write_text(json.dumps({'cutoff': 10}))
            (root/'heartbeat-state.json').write_text(json.dumps({'monitor_stale': {'alerted': True}}))
            (root/'activity.jsonl').write_text(json.dumps({'time': '2026-01-01', 'tool': 'status'})+'\n')
            (root/'notified.jsonl').write_text(json.dumps({'event': 'part', 'job': 'reply', 'channel': 'tg'})+'\n')
            (root/'notify-outbox.jsonl').write_text(json.dumps({'id': 'queued', 'channel': 'hash', 'title': 'fake', 'text': 'synthetic', 'url': None})+'\n')
            with sqlite3.connect(root/'confirmations.sqlite3') as db:
                db.execute('CREATE TABLE approvals(token TEXT PRIMARY KEY,payload TEXT,expires REAL,used INTEGER)')
                db.execute("INSERT INTO approvals VALUES('hash','{}',999,0)")
            for _ in range(2):
                with Store(root/'jobs.sqlite3') as s:
                    self.assertEqual(s.schema_version, 7)
                    self.assertEqual(s.monitor_state(), {'boss': {'count': 2}})
                    self.assertEqual(s.latest_run('monitor')['state'], 'running')
                    self.assertEqual(len(s.events()), 1)
                    self.assertEqual(len(s.notifications()), 2)
                    self.assertEqual(s.connection.execute('SELECT count(*) FROM approvals').fetchone()[0], 1)
            self.assertEqual(len(list(root.glob('*.migrated-*'))), 8)

    def test_three_concurrent_writers_200_each(self):
        with tempfile.TemporaryDirectory() as home:
            script = '''from job_agent.store import Store
import sys
with Store() as s:
 for i in range(200):
  with s.transaction():
   s.append_event({'time':str(i),'worker':sys.argv[1]})
   s.save_run(sys.argv[1],{'status':'running','i':i},run_id=sys.argv[1]+str(i))
'''
            children = [subprocess.Popen([sys.executable, '-c', script, kind], env={**os.environ, 'JOB_AGENT_HOME': home}, stderr=subprocess.PIPE) for kind in ('daily','monitor','heartbeat')]
            for child in children:
                _, err = child.communicate(timeout=60)
                self.assertEqual(child.returncode, 0, err)
            with Store(Path(home)/'jobs.sqlite3') as s:
                self.assertEqual(len(s.events()), 600)
                self.assertEqual(s.connection.execute('SELECT count(*) FROM runs').fetchone()[0], 600)
