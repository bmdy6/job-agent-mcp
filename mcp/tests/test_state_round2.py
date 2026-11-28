"""Round-two migration contracts: upstream runs, alerts, and daily crash recovery."""
from contextlib import closing
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from job_agent.config import Settings
from job_agent.store import Store
from job_agent.store.database import MIGRATIONS


def seed_v6(path):
    with closing(sqlite3.connect(path)) as db, db:
        db.execute('CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY,applied_at TEXT)')
        for version, statements in MIGRATIONS:
            if version > 6:
                break
            for sql in statements:
                db.execute(sql)
            db.execute('INSERT INTO schema_migrations VALUES(?,?)', (version, 'fixture'))
        db.execute('PRAGMA user_version=6')


class RoundTwoStateTest(unittest.TestCase):
    def test_daily_failure_checkpoint_and_settings_audit_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            seed_v6(home/'jobs.sqlite3')
            report = dict(run_id='upstream-run',status='interrupted',started_at='2026-11-28T09:00:00+08:00',
                current_step='delivery',collection_complete=True,interrupted=True,
                last_failure={'run_id':'earlier-run','reason_code':'connection_refused'})
            source = home/'daily-last-run.json'
            source.write_text(json.dumps(report))
            history = home/'config-history'/'changes.jsonl'
            history.parent.mkdir()
            history.write_text('{"history_id":"fixture","changes":[]}\n')
            original_history = history.read_bytes()
            for _ in range(2):
                with Store(home/'jobs.sqlite3',migrate=True) as store:
                    self.assertEqual(store.latest_run('daily'),report)
                    self.assertEqual(store.connection.execute("SELECT run_id FROM runs WHERE kind='daily'").fetchone()[0],'upstream-run')
                    self.assertEqual(store.connection.execute("SELECT count(*) FROM legacy_imports WHERE name LIKE 'config-history/%'").fetchone()[0],0)
            self.assertEqual(history.read_bytes(),original_history)
            self.assertFalse(source.exists())
            self.assertEqual(len(list(home.glob('daily-last-run.json.migrated-*'))),1)

    def test_import_alert_cooldown_and_reset_preserves_reply_receipts(self):
        from job_agent.observability import notify_once, recover_alerts
        cfg = Settings(notify={'enabled':True,'channels':{'tg':{'type':'telegram','enabled':True,'chat_id':'fixture'}}})
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ,JOB_AGENT_HOME=directory):
            home = Path(directory)
            seed_v6(home/'jobs.sqlite3')
            def receipt(event, platform, **fields):
                return dict(event=event,job=event,channel='tg',platform=platform,**fields)
            rows = [receipt('error:boss','boss',alert_at=1000),
                receipt('error:zhilian','zhilian',alert_at=1000),
                dict(event='watch',job='watch-job',channel='tg'),
                receipt('alert-recovered:boss','boss',alert_reset=True),
                receipt('error:boss','boss',alert_at=1050)]
            (home/'notified.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in rows))
            with Store(migrate=True) as store:
                self.assertEqual(store.alert_receipts()['error:boss','tg']['alert_at'],1050)
                self.assertTrue(store.notification_sent('reply:watch:tg'))
            with patch('job_agent.notify.send_one',return_value={'success':True}) as sender, patch('job_agent.observability.time.time',return_value=1100):
                notify_once(cfg,'error:boss','title','body',platform='boss')
                sender.assert_not_called()
                recover_alerts(cfg,'boss')
                notify_once(cfg,'error:boss','title','body',platform='boss')
                notify_once(cfg,'error:zhilian','title','body',platform='zhilian')
                self.assertEqual(sender.call_count,1)
            with patch('job_agent.notify.send_one',return_value={'success':True}) as sender, patch('job_agent.observability.time.time',return_value=9000):
                notify_once(cfg,'error:boss','title','body',platform='boss')
                self.assertEqual(sender.call_count,1)
            with Store() as store:
                self.assertTrue(store.notification_sent('reply:watch:tg'))

    def test_daily_sigkill_commits_result_and_one_replayable_summary(self):
        from job_agent.service.deliver import drain_notifications
        cfg = Settings(notify={'enabled':True,'channels':{'tg':{'type':'telegram','enabled':True,'chat_id':'fixture'}}})
        with tempfile.TemporaryDirectory() as directory:
            script = '''import os,signal
from job_agent.config import Settings
from job_agent.store import SQLiteDeliveryStore
from job_agent.engine.models import Job,Status,Reason
from job_agent.service.daily import delivery_checkpoint,run_lock
from job_agent.config import job_agent_home
cfg=Settings(notify={'enabled':True,'channels':{'tg':{'type':'telegram','enabled':True,'chat_id':'fixture'}}})
with run_lock(job_agent_home()), SQLiteDeliveryStore(dry_run=False) as store:
 job=Job('boss','fixture','https://example.invalid/fixture')
 store.upsert_job(platform=job.platform,platform_job_id=job.job_id,url=job.url)
 token=store.create_batch([job],{'boss':'fixture'},'',cfg.engine.model_dump())
 store.confirm_batch(token,config=cfg.engine.model_dump())
 store.prepare_run(cfg.engine.model_dump(),batch_token=token,greetings={'boss':'fixture'})
 report=dict(run_id='a'*32,status='running',started_at='2026-11-28T09:00:00+08:00',batches={'boss':[token]})
 store.result_notification_hook=lambda:delivery_checkpoint(store,report,cfg)
 store.update(job,Status.SENT,Reason.NONE)
 os.kill(os.getpid(),signal.SIGKILL)
'''
            child = subprocess.run([sys.executable,'-c',script],env={**os.environ,'JOB_AGENT_HOME':directory},capture_output=True,timeout=20)
            self.assertEqual(child.returncode,-signal.SIGKILL,child.stderr)
            with Store(Path(directory)/'jobs.sqlite3') as store:
                self.assertEqual(len(store.list_deliveries()),1)
                self.assertEqual(store.latest_run('daily')['delivery']['boss']['sent'],1)
                entries = store.notifications()
                self.assertEqual(len(entries),1)
                self.assertEqual(entries[0]['status'],'pending')
                self.assertTrue(entries[0]['body'].endswith('运行编号：aaaaaaaa'))
                with patch('job_agent.notify.send_one',return_value={'success':True}) as sender:
                    drain_notifications(store,cfg)
                    drain_notifications(store,cfg)
                self.assertEqual(sender.call_count,1)
                self.assertEqual(len(store.list_deliveries()),1)
                self.assertEqual(store.notifications()[0]['status'],'sent')

    def test_daily_active_writer_prevents_background_summary_send(self):
        from job_agent.service.daily import run_lock, queue_summary
        from job_agent.service.deliver import drain_notifications
        cfg = Settings(notify={'enabled':True,'channels':{'tg':{'type':'telegram','enabled':True,'chat_id':'fixture'}}})
        with tempfile.TemporaryDirectory() as directory, Store(Path(directory)/'jobs.sqlite3') as store:
            queue_summary(store,{'run_id':'b'*32,'status':'running'},cfg,'title','body',topic='delivery')
            with run_lock(Path(directory)), patch('job_agent.notify.send_one') as sender:
                drain_notifications(store,cfg)
                sender.assert_not_called()
            self.assertEqual(store.notifications()[0]['status'],'pending')

    def test_final_daily_summary_replaces_checkpoint_and_sends_once(self):
        from job_agent.service.daily import queue_summary, send_summary
        from job_agent.service.deliver import drain_notifications
        cfg = Settings(notify={'enabled':True,'channels':{'tg':{'type':'telegram','enabled':True,'chat_id':'fixture'}}})
        with tempfile.TemporaryDirectory() as directory, Store(Path(directory)/'jobs.sqlite3') as store:
            report = {'run_id':'c'*32,'status':'running'}
            queue_summary(store,report,cfg,'interrupted checkpoint','partial body',topic='delivery')
            report['status']='completed'
            with patch('job_agent.notify.send_one',return_value={'success':True}) as sender:
                self.assertEqual(send_summary(store,report,cfg,'final title','final body',topic='delivery'),['tg'])
                self.assertEqual(send_summary(store,report,cfg,'final title','final body',topic='delivery'),['tg'])
                drain_notifications(store,cfg)
            self.assertEqual(sender.call_count,1)
            self.assertEqual(len(store.notifications()),1)
            self.assertTrue(store.notifications()[0]['body'].startswith('final body'))
            self.assertEqual(store.latest_run('daily')['status'],'completed')

    def test_daily_checkpoint_rolls_back_with_delivery_result(self):
        from job_agent.store import SQLiteDeliveryStore
        from job_agent.engine.models import Job,Status,Reason
        from job_agent.service.daily import delivery_checkpoint
        cfg = Settings(notify={'enabled':True,'channels':{'tg':{'type':'telegram','enabled':True,'chat_id':'fixture'}}})
        with tempfile.TemporaryDirectory() as directory, SQLiteDeliveryStore(Path(directory)/'jobs.sqlite3',dry_run=False) as store:
            job = Job('boss','fixture','https://example.invalid/fixture')
            store.upsert_job(platform=job.platform,platform_job_id=job.job_id,url=job.url)
            token=store.create_batch([job],{'boss':'fixture'},'',cfg.engine.model_dump())
            store.confirm_batch(token,config=cfg.engine.model_dump())
            report=dict(run_id='d'*32,status='running',batches={'boss':[token]})
            def crash():
                delivery_checkpoint(store,report,cfg)
                raise RuntimeError('before commit')
            store.result_notification_hook=crash
            with self.assertRaisesRegex(RuntimeError,'before commit'):
                store.update(job,Status.SENT,Reason.NONE)
            self.assertEqual(store.notifications(),[])
            self.assertEqual(store.list_deliveries(),[])
            self.assertIsNone(store.latest_run('daily'))
            self.assertEqual(store.records()[0].status,Status.PENDING)
