"""Crash/rollback evidence uses synthetic inputs and injected controllers only."""
from contextlib import closing
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from job_agent.store import Store, SQLiteDeliveryStore
from job_agent.store.database import MIGRATIONS
from job_agent.config import Settings
from job_agent.service.deliver import notification_channels, drain_notifications


class StateRecoveryTest(unittest.TestCase):
    def test_sigkill_after_delivery_commit_before_send_drains_once(self):
        with tempfile.TemporaryDirectory() as home:
            root = Path(home)
            script = '''import os,signal
from job_agent.store import SQLiteDeliveryStore
from job_agent.config import Settings
from job_agent.engine.models import Job,Status,Reason
from job_agent.service.deliver import configure_summary_notifications
cfg=Settings(notify={'enabled':True,'channels':{'tg':{'type':'telegram','enabled':True,'chat_id':'fake'}}})
with SQLiteDeliveryStore(dry_run=False) as s:
 j=Job('boss','synthetic','https://example.invalid/fake')
 s.upsert_job(platform=j.platform,platform_job_id=j.job_id,url=j.url)
 token=s.create_batch([j],{'boss':'synthetic'},'',cfg.engine.model_dump())
 s.confirm_batch(token,config=cfg.engine.model_dump())
 s.prepare_run(cfg.engine.model_dump(),batch_token=token,greetings={'boss':'synthetic'})
 configure_summary_notifications(s,cfg)
 s.update(j,Status.SENT,Reason.NONE)
 os.kill(os.getpid(),signal.SIGKILL)
'''
            child = subprocess.run([sys.executable,'-c',script],env={**os.environ,'JOB_AGENT_HOME':home},capture_output=True,timeout=15)
            self.assertEqual(child.returncode, -signal.SIGKILL, child.stderr)
            cfg=Settings(notify={'enabled':True,'channels':{'tg':{'type':'telegram','enabled':True,'chat_id':'fake'}}})
            with Store(root/'jobs.sqlite3') as store:
                self.assertEqual(len(store.list_deliveries()),1)
                self.assertEqual(store.notifications()[0]['status'],'pending')
                with patch('job_agent.notify.send_one',return_value={'success':True}) as sender:
                    drain_notifications(store,cfg)
                    drain_notifications(store,cfg)
                self.assertEqual(sender.call_count,1)
                self.assertEqual(store.notifications()[0]['status'],'sent')
                self.assertEqual(store.notifications()[0]['attempts'],1)
                self.assertEqual(len(store.list_deliveries()),1)

    def test_render_failure_keeps_confirmed_receipt_and_notification(self):
        from job_agent.engine.models import Job,Status,Reason
        with tempfile.TemporaryDirectory() as home, SQLiteDeliveryStore(Path(home)/'jobs.sqlite3',dry_run=False) as store:
            job=Job('boss','synthetic','https://example.invalid/fake')
            store.upsert_job(platform=job.platform,platform_job_id=job.job_id,url=job.url)
            cfg=Settings()
            token=store.create_batch([job],{'boss':'synthetic'},'',cfg.engine.model_dump())
            store.confirm_batch(token,config=cfg.engine.model_dump())
            store.prepare_run(cfg.engine.model_dump(),batch_token=token,greetings={'boss':'synthetic'})
            def broken(_):raise ValueError('synthetic')
            store.configure_notifications(['synthetic-channel'],broken)
            store.update(job,Status.SENT,Reason.NONE)
            self.assertEqual(store.list_deliveries()[0]['status'],'sent')
            self.assertEqual(store.notifications()[0]['status'],'pending')
            self.assertTrue(store.notifications()[0]['body'])
            self.assertEqual(store.events()[0]['result']['error_type'],'ValueError')

    def test_normal_open_cannot_upgrade_existing_v6(self):
        with tempfile.TemporaryDirectory() as home:
            path=Path(home)/'jobs.sqlite3'
            with closing(sqlite3.connect(path)) as db,db:
                db.execute('CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY,applied_at TEXT)')
                for version,statements in MIGRATIONS:
                    if version>6:break
                    for sql in statements:db.execute(sql)
                    db.execute('INSERT INTO schema_migrations VALUES(?,?)',(version,'synthetic'))
                db.execute('PRAGMA user_version=6')
            legacy=Path(home)/'monitor-state.json';legacy.write_text('{}')
            before=path.read_bytes()
            with self.assertRaisesRegex(ValueError,'explicit deployment migration'):
                Store(path)
            self.assertEqual(path.read_bytes(),before)
            self.assertTrue(legacy.is_file())
            self.assertEqual(list(Path(home).glob('*.migrated-*')),[])
            with Store(path,migrate=True) as store:self.assertEqual(store.schema_version,7)

    def test_corrupt_import_rolls_back_record_set_keeps_source(self):
        with tempfile.TemporaryDirectory() as home:
            root=Path(home)
            path=root/'activity.jsonl'
            path.write_text(json.dumps({'time':'synthetic','tool':'status'})+'\ninvalid\n')
            with self.assertRaises(ValueError):
                Store(root/'jobs.sqlite3')
            self.assertTrue(path.is_file())
            with closing(sqlite3.connect(root/'jobs.sqlite3')) as db:
                self.assertEqual(db.execute('SELECT count(*) FROM events').fetchone()[0],0)
                self.assertEqual(db.execute('SELECT count(*) FROM legacy_imports').fetchone()[0],0)

    def test_archive_failure_retries_import_without_duplicates(self):
        with tempfile.TemporaryDirectory() as home:
            root=Path(home)
            (root/'activity.jsonl').write_text(json.dumps({'time':'synthetic','tool':'status'})+'\n')
            with patch('job_agent.store.legacy.os.replace',side_effect=OSError('synthetic disk error')):
                with self.assertRaises(OSError):Store(root/'jobs.sqlite3')
            with Store(root/'jobs.sqlite3',migrate=True) as store:
                self.assertEqual(len(store.events()),1)
            self.assertEqual(len(list(root.glob('activity.jsonl.migrated-*'))),1)

    def test_v6_upgrade_and_deploy_database_restore_restores_legacy_files(self):
        import importlib.util
        spec=importlib.util.spec_from_file_location('deploy_v7',Path(__file__).resolve().parents[2]/'scripts/deploy.py')
        deploy=importlib.util.module_from_spec(spec);spec.loader.exec_module(deploy)
        with tempfile.TemporaryDirectory() as home:
            root=Path(home);path=root/'jobs.sqlite3';backup=root/'backup';backup.mkdir()
            with closing(sqlite3.connect(path)) as db,db:
                db.execute('CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY,applied_at TEXT)')
                for v,statements in MIGRATIONS:
                    if v>6:break
                    for sql in statements:db.execute(sql)
                    db.execute('INSERT INTO schema_migrations VALUES(?,?)',(v,'synthetic'))
                    db.execute(f'PRAGMA user_version={v}')
                db.execute("INSERT INTO jobs(platform,platform_job_id,collected_at) VALUES('boss','synthetic','before')")
            originals={
                'monitor-state.json':{'boss':{'count':2,'stage':'fetch','kind':'fetch','reason_code':'synthetic'}},
                'monitor-runtime.json':{'pid':123,'state':'running','started_at':100,'platforms':{'boss':{'count':2}}},
                'monitor-policy.json':{'activated_at':100,'backfill_hours':24,'cutoff':1},
                'heartbeat-state.json':{'monitor_stale':{'since':1,'alerted':True}}}
            for name,data in originals.items():(root/name).write_text(json.dumps(data))
            deploy.backup_database(path,backup/'jobs.sqlite3')
            deploy.snapshot_legacy_state(root,backup)
            for _ in range(2):
                with Store(path,migrate=True) as store:
                    self.assertEqual(store.schema_version,7)
                    self.assertEqual(len(store.list_jobs()),1)
                    self.assertEqual(store.monitor_state(),originals['monitor-state.json'])
            deploy.restore_database(backup/'jobs.sqlite3',path)
            deploy.restore_legacy_state(root,backup)
            self.assertEqual(deploy.schema_version(path),6)
            for name,data in originals.items():self.assertEqual(json.loads((root/name).read_text()),data)
            # Archived v7 source files remain available; rollback never deletes them.
            self.assertEqual(len(list(root.glob('*.migrated-*'))),4)
