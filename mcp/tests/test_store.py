from pathlib import Path
import sqlite3
import tempfile
import unittest
from job_agent.store import Store

class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_api_migration_constraints(self):
        path = self.root/'own.db'
        with Store(path) as db:
            self.assertEqual(db.schema_version, 7)
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
            with self.assertRaisesRegex(ValueError, 'Own job not found: 999'): db.record_delivery(999, status='sent')
        with Store(path) as db:
            self.assertEqual(db.get_job(a)['title'], 'Engineer')
            self.assertEqual(len(db.list_jobs()), 2)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)




    def test_future_schema(self):
        path = self.root/'future.db'
        with sqlite3.connect(path) as c: c.execute('PRAGMA user_version=999')
        with self.assertRaises(ValueError): Store(path)
