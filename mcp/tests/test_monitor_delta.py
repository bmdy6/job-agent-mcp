"""Delta monitoring through real sources, Store and ingestion (no browser/network)."""
import os
import tempfile
import time
import unittest
from unittest.mock import patch

from job_agent.browser.base import FakeBrowser, FakeSpace, PageTimeout
from job_agent.config import Settings
from job_agent.monitor import Monitor
from job_agent.platforms.boss.replies import ReplySource
from job_agent.store import Store
import test_boss_monitor


class DeltaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, JOB_AGENT_HOME=self.temp.name)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.store = Store()
        self.addCleanup(lambda: self.store.close())
        self.job = self.store.upsert_job(platform='boss', platform_job_id='job')
        self.store.record_delivery(self.job, status='sent')
        self.config = Settings.model_validate({'notify': {'enabled': False}})
        self.space = FakeSpace('job-agent 监听')
        self.browser = FakeBrowser({self.space.name: self.space})

    def row(self, mid='123', unread=0, sid='session', jid='job'):
        return dict(encryptJobId=jid, uniqueId=sid, lastMsgId=mid,
                    unreadCount=unread, lastTS=time.time()*1000)

    def poll(self, rows, *, timeout=False, fail_ingest=False):
        page = test_boss_monitor.MonitorSourceTests().page(rows)
        page.responses['boss.messages']['rows'][0]['time'] = time.time()
        if rows:
            target = next((r for r in rows if r['lastMsgId'] != '123' or r['unreadCount']), rows[0])
            data = page.responses['boss.messages']
            data.update(current_job_id=target['encryptJobId'], current_conversation_id=target['uniqueId'])
            data['rows'][0].update(id=target['lastMsgId'], job_id=target['encryptJobId'], conversation_id=target['uniqueId'])
        if timeout:
            original = page.wait_for
            def wait(selector, **kw):
                if 'data-mid' in selector:
                    raise PageTimeout('missing')
                original(selector, **kw)
            page.wait_for = wait
        self.space._pages.append(page)
        monitor = Monitor(self.browser, [ReplySource()], self.store, self.config)
        if fail_ingest:
            with patch.object(self.store, 'add_reply', side_effect=RuntimeError('write failed')):
                result = monitor.poll_once()
        else:
            result = monitor.poll_once()
        self.assertTrue(page.closed)
        self.assertFalse(any(a[0] in {'fill', 'press'} for a in page.actions))
        return result, [a for a in page.actions if a[0] == 'click_projected']

    def test_first_poll_seeds_read_sessions_but_opens_unread(self):
        result, clicks = self.poll([self.row(), self.row(sid='u', jid='u', unread=2)])
        self.assertEqual(len(clicks), 1)
        self.assertEqual(result['failed'], 0)
        self.assertEqual(self.poll([self.row(), self.row(sid='u', jid='u')])[1], [])

    def test_unchanged_after_restart_never_clicks(self):
        rows = [self.row()]
        self.poll(rows)
        self.store.close()
        self.store = Store()
        for _ in range(3):
            result, clicks = self.poll(rows)
            self.assertEqual(clicks, [])
            self.assertEqual(result['failed'], 0)

    def test_only_changed_session_is_opened_and_ingested(self):
        rows = [self.row(), self.row(sid='other', jid='other')]
        self.poll(rows)
        rows[0] = self.row('124')
        result, clicks = self.poll(rows)
        self.assertEqual(len(clicks), 1)
        self.assertEqual(result['added'], 1)
        self.assertEqual(self.store.list_replies()[0]['message_id'], 'session:124')
        self.assertEqual(self.poll(rows)[1], [])

    def test_unread_and_new_session_after_baseline_are_opened(self):
        self.poll([self.row()])
        self.assertEqual(len(self.poll([self.row(unread=1)])[1]), 1)
        self.assertEqual(len(self.poll([self.row(sid='new', jid='new')])[1]), 1)

    def test_message_timeout_keeps_old_cursor_for_retry(self):
        self.poll([self.row()])
        rows = [self.row('124')]
        self.poll(rows, timeout=True)
        self.assertEqual(self.poll(rows)[0]['added'], 1)
        self.assertEqual(self.poll(rows)[1], [])

    def test_ingest_failure_keeps_old_cursor_for_retry(self):
        self.poll([self.row()])
        rows = [self.row('124')]
        self.assertEqual(self.poll(rows, fail_ingest=True)[0]['failed'], 1)
        self.assertEqual(self.store.list_replies(), [])
        self.assertEqual(self.poll(rows)[0]['added'], 1)

    def test_empty_session_is_seeded_without_clicks(self):
        self.assertEqual(self.poll([self.row('0')])[1], [])
        self.assertEqual(self.poll([self.row('0')])[1], [])
        self.assertEqual(self.poll([self.row('124')])[0]['added'], 1)

    def test_cursor_write_failure_rolls_back_reply_ingestion(self):
        self.poll([self.row()])
        with patch.object(self.store, 'save_monitor_conversations', side_effect=RuntimeError('cursor write failed')):
            self.assertEqual(self.poll([self.row('124')])[0]['failed'], 1)
        self.assertEqual(self.store.list_replies(), [])
        self.assertEqual(self.poll([self.row('124')])[0]['added'], 1)

    def test_backfill_window_is_fixed_across_restart(self):
        self.config.engine.monitor.backfill_hours = 1
        self.poll([self.row()])
        cutoff = self.store.monitor_baseline('boss')
        self.assertAlmostEqual(cutoff, time.time()-3600, delta=2)
        self.config.engine.monitor.backfill_hours = 48
        self.poll([self.row()])
        self.assertEqual(self.store.monitor_baseline('boss'), cutoff)

    def test_missing_summary_fails_closed_without_advancing_baseline(self):
        row = self.row()
        del row['lastMsgId']
        self.assertEqual(self.poll([dict(row, lastMsgId='')])[0]['failed'], 1)
        self.assertIsNone(self.store.monitor_baseline('boss'))


class OtherPlatformTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name+'/jobs.sqlite3')
        self.addCleanup(self.store.close)

    def fetch(self, source, page):
        from job_agent.monitor.delta import ConversationCursor
        from job_agent.monitor.repository import ingest
        cursor = ConversationCursor(self.store, source.platform, 48)
        events = source.fetch(FakeSpace('test', pages=[page]), cursor=cursor, since=cursor.since)
        ingest(self.store, source.platform, events, cursor=cursor)
        self.assertTrue(page.closed)
        return events, [a for a in page.actions if a[0] in {'click', 'click_projected'}]

    def test_zhilian_baseline_unchanged_changed_unread(self):
        from job_agent.browser.base import FakePage
        from job_agent.platforms.zhilian.replies import ReplySource
        from job_agent.platforms.zhilian import pages
        row = dict(sessionId='session', jobNumber='job', sendTime=time.time()*1000,
                   text='PRIVATE preview', unreadCount=0, lastSentenceType=1, senderType=1)
        def page():
            return FakePage('p2', {'zhilian.state': {}, 'zhilian.list': {'rows': [dict(row)]},
                'zhilian.select_session': True, 'zhilian.messages': {'messages': [
                    {'session': {'sessionId': 'session', 'jobNumber': 'job'},
                     'msg': {'idServer': 'm', 'time': row['sendTime'], 'type': 'text',
                             'fromMe': False, 'flow': 'in', 'body': 'reply'}}]}})
        self.assertEqual(self.fetch(ReplySource(), page())[1], [])
        self.assertEqual(self.fetch(ReplySource(), page())[1], [])
        self.assertNotIn('PRIVATE', str(self.store.monitor_conversations('zhilian')))
        row['text'] = 'changed preview'
        self.assertEqual(len(self.fetch(ReplySource(), page())[1]), 1)
        self.assertEqual(self.fetch(ReplySource(), page())[1], [])
        row['unreadCount'] = 1
        self.assertEqual(len(self.fetch(ReplySource(), page())[1]), 1)
        with self.assertRaises(pages.PlatformStateError):
            pages.conversation_summary(dict(row, unreadCount=None))

    def test_liepin_baseline_unchanged_changed_and_unknown_summary(self):
        from job_agent.browser.base import FakePage
        from job_agent.platforms.liepin.replies import ReplySource
        from job_agent.platforms.liepin import pages
        row = dict(id='contact', index=0, lastMsgId='m', lastTime=time.time()*1000, unreadCount=0)
        def page():
            return FakePage('p2', {'liepin.guard': {}, 'liepin.contacts': [dict(row)],
                'liepin.select_contact': True,
                'liepin.messages': {'ready': True, 'contact': {'id': 'contact'}, 'items': []}})
        # ENTRY click is necessary to expose the contact list; it is not a conversation read.
        self.assertEqual(len(self.fetch(ReplySource(), page())[1]), 1)
        self.assertEqual(len(self.fetch(ReplySource(), page())[1]), 1)
        row['lastMsgId'] = 'new'
        self.assertEqual(len(self.fetch(ReplySource(), page())[1]), 2)
        row['unreadCount'] = 1
        self.assertEqual(len(self.fetch(ReplySource(), page())[1]), 2)
        with self.assertRaises(pages.Unverified):
            pages.conversation_summary({'id': 'contact'})

    def test_zhilian_stale_messages_do_not_acknowledge_new_summary(self):
        from job_agent.browser.base import FakePage
        from job_agent.platforms.zhilian.replies import ReplySource
        from job_agent.platforms.zhilian import pages
        row = dict(sessionId='session', jobNumber='job', sendTime=time.time()*1000,
                   text='preview', unreadCount=0)
        def page():
            return FakePage('p2', {'zhilian.state': {}, 'zhilian.list': {'rows': [dict(row)]},
                'zhilian.select_session': True, 'zhilian.messages': {'messages': [
                    {'session': {'sessionId': 'session', 'jobNumber': 'job'},
                     'msg': {'idServer': 'old', 'time': row['sendTime']-1000}}]}})
        self.fetch(ReplySource(), page())
        old = self.store.monitor_conversations('zhilian')
        row['sendTime'] += 1000
        with patch('job_agent.platforms.zhilian.replies.time.sleep'), self.assertRaises(pages.PlatformStateError):
            self.fetch(ReplySource(), page())
        self.assertEqual(self.store.monitor_conversations('zhilian'), old)
        wrong = page()
        msg = wrong.responses['zhilian.messages']['messages'][0]
        msg['msg']['time'] = row['sendTime']
        msg['session']['jobNumber'] = 'other'
        with patch('job_agent.platforms.zhilian.replies.time.sleep'), self.assertRaises(pages.PlatformStateError):
            self.fetch(ReplySource(), wrong)
        self.assertEqual(self.store.monitor_conversations('zhilian'), old)

    def test_zhilian_projection_uses_stable_identity_and_keeps_message_metadata(self):
        from job_agent.platforms.zhilian import pages
        run_js = test_boss_monitor.ProjectionTests().run_js
        result = run_js('''global.document={querySelectorAll:()=>[
          {__vue__:{$props:{session:{sessionId:'s',jobNumber:'j',sendTime:1795670400000,
            text:'preview',unreadCount:2,lastSentenceType:1,senderType:1}}}}]};''', pages.LIST_JS)
        summary = pages.conversation_summary(result['rows'][0])
        self.assertEqual(summary.unread_count, 2)
        self.assertTrue(summary.last_message_id.startswith('summary:'))
        script = pages.select_session({'sessionId': 's', 'jobNumber': 'j'})
        result = run_js('''const e={__vue__:{$props:{session:{sessionId:'s',jobNumber:'j'}}},
          scrollIntoView:()=>{},getBoundingClientRect:()=>({x:10,y:20,right:110,bottom:70,width:100,height:50}),
          contains:()=>false};global.innerWidth=800;global.innerHeight=600;
          global.document={querySelectorAll:()=>[{},e],elementFromPoint:()=>e};''', script)
        self.assertEqual((result['x'],result['y']), (10,20))

    def test_job51_status_baseline_unchanged_and_changed(self):
        from job_agent.browser.base import FakePage
        from job_agent.platforms.job51.replies import ReplySource
        import test_job51_status
        labels = []
        class Page(FakePage):
            def evaluate(self, js):
                from urllib.parse import urlsplit, parse_qs
                tag = parse_qs(urlsplit(self._url).query).get('tagType', [''])[0]
                rows = test_job51_status.snapshot()['rows'] if not tag or tag in labels else []
                data = test_job51_status.snapshot(tag, rows)
                if not rows:
                    data.update(empty=True, page=0, pages=0)
                return data
        self.assertEqual(self.fetch(ReplySource(), Page('p2')), ([], []))
        self.assertEqual(self.fetch(ReplySource(), Page('p2')), ([], []))
        labels.append('cy')
        events, clicks = self.fetch(ReplySource(), Page('p2'))
        self.assertEqual(len(events), 1)
        self.assertEqual(clicks, [])
        self.assertEqual(self.fetch(ReplySource(), Page('p2')), ([], []))

    def test_same_conversation_id_does_not_cross_platform_baselines(self):
        from job_agent.monitor.delta import ConversationCursor
        from job_agent.platforms.base import ConversationSummary
        summary = ConversationSummary('same', '123', 0)
        first = ConversationCursor(self.store, 'boss', 48)
        self.assertFalse(first.select(summary))
        with self.store.transaction():
            first.commit(self.store)
        other = ConversationCursor(self.store, 'zhilian', 48)
        self.assertFalse(other.select(summary))
        self.assertTrue(other.baseline)


class MigrationTests(unittest.TestCase):
    def test_v4_to_v5_preserves_rows_and_reopens_idempotently(self):
        import sqlite3
        from job_agent.store.database import MIGRATIONS
        with tempfile.TemporaryDirectory() as home:
            path = home+'/v4.sqlite3'
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT)')
                for version, statements in MIGRATIONS:
                    if version > 4:
                        break
                    for statement in statements:
                        db.execute(statement)
                    db.execute('INSERT INTO schema_migrations VALUES(?,?)', (version, 'before'))
                db.execute('PRAGMA user_version=4')
                db.execute("INSERT INTO jobs(platform,platform_job_id,collected_at) VALUES('boss','old','before')")
            for _ in range(2):
                with Store(path, migrate=True) as store:
                    self.assertEqual(store.schema_version, 7)
                    self.assertEqual(store.get_job(1)['platform_job_id'], 'old')
                    self.assertEqual([r[0] for r in store.connection.execute('SELECT version FROM schema_migrations')], [1,2,3,4,5,6,7])
                    self.assertEqual(store.monitor_conversations('boss'), {})

    def test_failed_v5_migration_rolls_back_schema_and_ledger(self):
        import sqlite3
        from job_agent.store import database
        with tempfile.TemporaryDirectory() as home:
            path = home+'/v4.sqlite3'
            with Store(path, migrate=True):
                pass
            with sqlite3.connect(path) as db:
                db.execute('DROP TABLE monitor_baselines')
                db.execute('DROP TABLE monitor_conversations')
                db.execute('DELETE FROM schema_migrations WHERE version=5')
                db.execute('PRAGMA user_version=4')
            bad = [(v, [*sql, 'INVALID SQL'] if v==5 else sql) for v,sql in database.MIGRATIONS]
            with patch.object(database, 'MIGRATIONS', bad), self.assertRaises(sqlite3.OperationalError):
                Store(path, migrate=True)
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 4)
                self.assertEqual(db.execute('SELECT count(*) FROM schema_migrations WHERE version=5').fetchone()[0], 0)
                self.assertEqual(db.execute("SELECT count(*) FROM sqlite_master WHERE name IN ('monitor_baselines','monitor_conversations')").fetchone()[0], 0)
