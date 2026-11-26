import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from store import Store
from store.bosshunter import import_bosshunter

class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_api_migration_constraints(self):
        path = self.root/'own.db'
        with Store(path) as db:
            self.assertEqual(db.schema_version, 6)
            a = db.upsert_job(platform='boss', platform_job_id='1', title='AI')
            self.assertEqual(a, db.upsert_job(platform='boss', platform_job_id='1', title='Engineer'))
            self.assertNotEqual(a, db.upsert_job(platform='liepin', platform_job_id='1'))
            d = db.record_delivery(a, status='pending', greeting='x'*501)
            with self.assertRaises(ValueError):
                db.update_delivery(d, status='unverified', reason_code='receipt_missing')
            self.assertEqual(db.get_delivery(d)['greeting'], 'x'*501)
            self.assertEqual(db.get_delivery(d)['status'], 'pending')
            with self.assertRaises(ValueError): db.record_delivery(a, status='replied')
            r = db.add_reply(a, message_id='msg1', direction='inbound', body='hello')
            self.assertEqual(r, db.add_reply(a, message_id='msg1', direction='inbound', body='hello'))
            with self.assertRaises(sqlite3.IntegrityError): db.record_delivery(999, status='sent')
        with Store(path) as db:
            self.assertEqual(db.get_job(a)['title'], 'Engineer')
            self.assertEqual(len(db.list_jobs()), 2)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def source(self):
        path = self.root/'source.db'
        with sqlite3.connect(path) as c:
            c.executescript('''
                CREATE TABLE jobs(id TEXT PRIMARY KEY, source_platform TEXT, source_job_id TEXT,
                    title TEXT, company TEXT, jd TEXT, status TEXT, greeting TEXT, created_at TEXT,
                    updated_at TEXT, deleted_at TEXT, score INTEGER, score_reason TEXT,
                    resume_path TEXT, last_error_code TEXT);
                CREATE TABLE history(id INTEGER PRIMARY KEY, job_id TEXT, action TEXT, detail TEXT, created_at TEXT);
            ''')
            for row in [('a','boss','same','sent',None),('b','liepin','same','error',None),('c','boss','deleted','pending','2026-01-01')]:
                c.execute('INSERT INTO jobs(id,source_platform,source_job_id,status,deleted_at,title,company,greeting,created_at) VALUES(?,?,?,?,?,?,?,?,?)',
                          (*row,'Engineer','Example','long greeting','2026-01-01'))
            for row in [(1,'a','sent','truncated greeting'),(2,'a','hr_reply_detected','HR回复: hello'),
                        (3,'a','replied',json.dumps({'hr_question':'hello full','reply_fingerprint':'fp','manual_reply':'thanks'})),
                        (4,'b','error','unknown'),(5,'a','approved','ok')]:
                c.execute('INSERT INTO history VALUES(?,?,?,?,?)',(*row,'2026-01-02'))
        return path

    def test_import_repeat_stats(self):
        source = self.source()
        before = source.read_bytes()
        with Store(self.root/'own.db') as db:
            first = import_bosshunter(db,source)
            second = import_bosshunter(db,source)
            self.assertEqual(first['destination'],second['destination'])
            self.assertEqual(second['inserted'],dict.fromkeys(('jobs','history','deliveries','replies'),0))
            self.assertEqual(db.legacy_stats(),{'sent':1,'error':1})
            self.assertEqual(db.funnel_stats()['采集总数'],2)
            self.assertEqual(db.funnel_stats()['发送'],1)
            self.assertEqual(len(db.list_jobs(include_deleted=True)),3)
            self.assertEqual(db.counts()['history'],5)
            replies = db.list_replies()
            self.assertEqual({r['direction'] for r in replies},{'inbound','outbound'})
            self.assertTrue(any(r['is_excerpt'] for r in replies))
            self.assertEqual(len(replies),3)
            self.assertEqual(db.counts()['deliveries_by_status'],{'sent':2,'unverified':2,'pending':1})
        self.assertEqual(before,source.read_bytes())

    def test_failure_rolls_back(self):
        source = self.source()
        with sqlite3.connect(source) as c: c.execute("INSERT INTO history VALUES(99,'missing','sent','','2026-01-01')")
        with Store(self.root/'own.db') as db:
            with self.assertRaises(ValueError): import_bosshunter(db,source)
            self.assertEqual(db.counts()['jobs'],0)
            self.assertEqual(db.counts()['history'],0)

    def test_future_schema(self):
        path = self.root/'future.db'
        with sqlite3.connect(path) as c: c.execute('PRAGMA user_version=999')
        with self.assertRaises(ValueError): Store(path)

    def test_cli(self):
        from cli import main
        source = self.source()
        with patch.dict(os.environ,{'JOB_AGENT_HOME':str(self.root/'home')}):
            out = io.StringIO()
            with contextlib.redirect_stdout(out): self.assertEqual(main(['import-bosshunter','--source',str(source)]),0)
            self.assertEqual(json.loads(out.getvalue())['destination']['jobs'],3)
            self.assertTrue((self.root/'home/jobs.sqlite3').exists())
            with self.assertLogs('cli',level='ERROR') as logs:
                self.assertEqual(main(['import-bosshunter','--source',str(self.root/'private-missing')]),1)
            self.assertNotIn('private-missing',''.join(logs.output))

    def test_same_target(self):
        with Store(self.root/'own.db') as db:
            with self.assertRaises(ValueError): import_bosshunter(db,db.path)

    def test_cli_same_file_never_migrates_source(self):
        from cli import main
        source = self.source()
        before = source.read_bytes()
        with self.assertLogs('cli',level='ERROR'):
            self.assertEqual(main(['import-bosshunter','--source',str(source),'--database',str(source)]),1)
        self.assertEqual(before,source.read_bytes())

    def test_missing_source_not_created(self):
        missing = self.root/'missing.db'
        with Store(self.root/'own.db') as db:
            with self.assertRaises(sqlite3.OperationalError): import_bosshunter(db,missing)
        self.assertFalse(missing.exists())

    def test_no_draft_as_sent_reply(self):
        source = self.source()
        with sqlite3.connect(source) as c:
            c.execute('INSERT INTO history VALUES(6,?,?,?,?)',('a','reply_pending',json.dumps({'hr_question':'question','ai_reply':'UNSENT DRAFT'}),'2026-01-03'))
        with Store(self.root/'own.db') as db:
            import_bosshunter(db,source)
            self.assertFalse(any(r['body']=='UNSENT DRAFT' for r in db.list_replies()))

    def test_snapshot_never_overwrites_engine_status(self):
        source = self.source()
        with Store(self.root/'own.db') as db:
            import_bosshunter(db,source)
            snapshot = db.list_deliveries()[0]
            db.record_delivery(snapshot['job_id'],status='unverified',reason_code='engine_receipt_missing')
            import_bosshunter(db,source)
            self.assertEqual(db.list_deliveries()[-1]['reason_code'],'engine_receipt_missing')
