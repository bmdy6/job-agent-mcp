import hashlib
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from job_agent.store import Store, SQLiteDeliveryStore
from job_agent.store.database import MIGRATIONS
from job_agent.engine.models import Job, Status, Reason


class StoreV2Test(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'jobs.db'

    def open(self, **kwargs):
        kwargs.setdefault('migrate', True)
        db = SQLiteDeliveryStore(self.path, **kwargs)
        self.addCleanup(db.close)
        return db

    def job(self, key='1'):
        return Job('boss', key, 'https://example.invalid/'+key)

    def batch(self, db, jobs=None):
        return db.create_batch(jobs or [self.job()], {'boss':'您'*501}, 'resume', {'limit':2})

    def test_v1_migration_preserves_audit_and_prefers_snapshot(self):
        with sqlite3.connect(self.path) as c:
            for statement in MIGRATIONS[0][1]: c.execute(statement)
            c.execute('PRAGMA user_version=1')
            c.execute("INSERT INTO jobs(id,platform,platform_job_id,url,collected_at) VALUES(1,'boss','1','https://example.invalid','2026-11-24')")
            for status,kind in [('sent','legacy_snapshot'),('pending','legacy_event')]:
                c.execute('''INSERT INTO deliveries(job_id,status,kind,created_at,updated_at)
                    VALUES(1,?,?,'2026-11-24','2026-11-24')''', (status,kind))
        before = hashlib.sha256(self.path.read_bytes()).digest()
        with self.assertRaises(ValueError): self.open()
        self.assertEqual(before, hashlib.sha256(self.path.read_bytes()).digest())
        db = self.open(dry_run=False)
        self.assertEqual(db.schema_version, 7)
        self.assertEqual(len(db.list_deliveries()), 2)
        self.assertEqual(db.records()[0].status, Status.SENT)
        self.assertIsNotNone(db.records()[0].attempted_at)
        db.close()
        with Store(self.path, migrate=True) as reopened:
            self.assertEqual(len(reopened.list_deliveries()),2)

    def test_audit_is_append_only_and_atomic_with_queue(self):
        db = self.open()
        db.add(self.job())
        db.update(self.job(), Status.UNVERIFIED, Reason.INTERRUPTED, 100)
        db.update(self.job(), Status.SENT, Reason.NONE)
        self.assertEqual([d['status'] for d in db.list_deliveries()], ['unverified','sent'])
        self.assertEqual(db.records()[0].attempted_at, 100)
        for sql in ['UPDATE deliveries SET greeting=\'edit\'', 'DELETE FROM deliveries']:
            with self.assertRaises(sqlite3.IntegrityError): db.connection.execute(sql)
        db.connection.execute("CREATE TRIGGER reject_audit BEFORE INSERT ON deliveries BEGIN SELECT RAISE(ABORT,'test'); END")
        with self.assertRaises(sqlite3.IntegrityError): db.update(self.job(), Status.FAILED, Reason.ATTACHMENT)
        self.assertEqual(db.records()[0].status, Status.SENT)

    def test_batch_immutable_snapshot_and_full_audit(self):
        db = self.open(dry_run=False)
        greetings = {'boss':'您'*501}
        jobs = [self.job()]
        token = db.create_batch(jobs,greetings,'resume',{'limit':2})
        greetings['boss']='changed'; jobs.clear()
        self.assertEqual(db.batch_jobs(token), [self.job()])
        with self.assertRaises(ValueError): db.confirm_batch(token, config={'limit':3})
        self.assertEqual(db.get_batch(token)['status'],'pending')
        db.confirm_batch(token, config={'limit':2})
        with self.assertRaises(ValueError): db.confirm_batch(token)
        for sql in ["UPDATE batches SET greetings_json='{}'", "UPDATE batches SET status='pending'", 'DELETE FROM batches']:
            with self.assertRaises(sqlite3.IntegrityError): db.connection.execute(sql)
        with self.assertRaises(ValueError): db.complete_batch(token)
        db.update(self.job(),Status.UNVERIFIED,Reason.INTERRUPTED,100)
        db.update(self.job(),Status.SENT,Reason.NONE)
        for event in db.list_deliveries():
            self.assertEqual(event['batch_id'], token)
            self.assertEqual(event['greeting'],'您'*501)
            self.assertEqual(event['attachment_name'],'resume')
        with self.assertRaises(sqlite3.IntegrityError):
            db.connection.execute('INSERT OR REPLACE INTO batches SELECT * FROM batches WHERE token=?',(token,))
        with self.assertRaises(sqlite3.IntegrityError):
            db.connection.execute('INSERT OR REPLACE INTO deliveries SELECT * FROM deliveries LIMIT 1')
        db.complete_batch(token)
        self.assertEqual(db.get_batch(token)['status'],'executed')

    def test_confirmation_conflict_rolls_back_all_jobs(self):
        db = self.open()
        db.add(self.job())
        db.update(self.job(),Status.UNVERIFIED,Reason.INTERRUPTED)
        token = self.batch(db,[self.job('2'),self.job()])
        with self.assertRaises(ValueError): db.confirm_batch(token)
        self.assertEqual(len(db.records()),1)
        self.assertEqual(db.get_batch(token)['status'],'pending')
        self.assertIsNone(db.connection.execute("SELECT id FROM jobs WHERE platform_job_id='2'").fetchone())

    def test_cross_process_lock(self):
        db = self.open()
        script = '''import sys
from job_agent.store import SQLiteDeliveryStore
with SQLiteDeliveryStore(sys.argv[1]) as db:
    try:
        with db.exclusive(): pass
    except RuntimeError: sys.exit(23)
'''
        with db.exclusive():
            r = subprocess.run([sys.executable,'-c',script,str(self.path)],cwd=Path(__file__).resolve().parents[1],timeout=10)
            self.assertEqual(r.returncode,23)
        with db.exclusive(): pass

    def test_mode_isolation_and_unbound_live_jobs(self):
        db = self.open(dry_run=False)
        before = self.path.read_bytes()
        with self.assertRaises(ValueError): self.open()
        self.assertEqual(before,self.path.read_bytes())
        with self.assertRaises(ValueError): db.add(self.job())
        with self.assertRaises(ValueError): SQLiteDeliveryStore()

    def test_invalid_batches_and_missing_token(self):
        db=self.open()
        for jobs,greeting in [([],{'boss':'x'}),([self.job(),self.job()],{'boss':'x'}),([self.job()],{})]:
            with self.assertRaises(ValueError): db.create_batch(jobs,greeting,'resume',{})
        with self.assertRaises(KeyError): db.confirm_batch('missing')
        with self.assertRaises(KeyError): db.batch_jobs('missing')

    def test_approved_url_survives_collection_refresh(self):
        db=self.open(dry_run=False)
        token=self.batch(db)
        db.confirm_batch(token)
        db.upsert_job(platform='boss',platform_job_id='1',url='https://example.invalid/changed')
        self.assertEqual(db.records()[0].job, self.job())
        with self.assertRaises(ValueError): db.prepare_run({'changed':True})
        with self.assertRaises(ValueError): db.prepare_run({'limit':2})

    def test_new_v2_import_initializes_queue_without_overwriting(self):
        db=self.open()
        job_id=db.upsert_job(platform='boss',platform_job_id='1',url=self.job().url)
        db.record_delivery(job_id,status='sent',kind='legacy_snapshot',source_key='first')
        db.record_delivery(job_id,status='unverified',kind='legacy_event')
        self.assertEqual(db.records()[0].status,Status.SENT)
        db.record_delivery(job_id,status='pending',kind='legacy_snapshot',source_key='second')
        self.assertEqual(db.records()[0].status,Status.SENT)

    def test_run_uses_exact_preview_content(self):
        db=self.open()
        cfg={'platforms':{'boss':{'greeting':'full','attachment_name':'resume'}}}
        token=db.create_batch([self.job()],{'boss':'different'},'resume',cfg)
        db.confirm_batch(token)
        with self.assertRaises(ValueError): db.prepare_run(cfg)
        other=SQLiteDeliveryStore(Path(self.temp.name)/'other.db')
        self.addCleanup(other.close)
        token=other.create_batch([self.job()],{'boss':'full'},'resume',cfg)
        other.confirm_batch(token)
        other.prepare_run(cfg)


class RebindUnattemptedTest(unittest.TestCase):
    def test_pending_unattempted_job_can_move_to_new_batch(self):
        import tempfile, os
        from job_agent.store import SQLiteDeliveryStore
        with tempfile.TemporaryDirectory() as d:
            path=os.path.join(d,'jobs.sqlite3')
            with SQLiteDeliveryStore(path, dry_run=False) as store:
                store.upsert_job(platform='boss', platform_job_id='j1', url='https://example.invalid/j1')
                jobs=[Job("boss","j1","https://example.invalid/j1")]
                t1=store.create_batch(jobs=jobs, greetings={'boss':'hi'}, attachment_name='', config={'a':1})
                store.confirm_batch(t1)
                t2=store.create_batch(jobs=jobs, greetings={'boss':'hi'}, attachment_name='', config={'a':1})
                store.confirm_batch(t2)
                row=store.connection.execute('SELECT batch_id FROM delivery_queue').fetchone()
                self.assertEqual(row['batch_id'], t2)
