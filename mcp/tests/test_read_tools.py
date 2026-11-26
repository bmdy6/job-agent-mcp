"""Own read tools use only a temporary SQLite pool; HTTP/browser are forbidden."""
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import server
from store import Store
from service import read_tools as own


class OwnReadTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env=patch.dict(os.environ,{'JOB_AGENT_HOME':self.temp.name},clear=True)
        env.start();self.addCleanup(env.stop)
        self.home=Path(self.temp.name)
        (self.home/'config.yaml').write_text('engine:\n  backend: own\ndirections: [Agent]\n')
        with Store() as store:
            self.a=store.upsert_job(platform='boss',platform_job_id='native',title='Agent工程师',company='甲公司',city='杭州',salary='15-25K',jd='任职要求：\n熟悉 SQL\n熟悉 Python',collected_at='2026-11-20T12:00:00+00:00')
            self.b=store.upsert_job(platform='liepin',platform_job_id='imported',title='Agent研发',company='乙公司',city='上海',salary='20-30K',legacy_status='sent',legacy_score=85)
            self.c=store.upsert_job(platform='51job',platform_job_id='filtered',legacy_status='filtered')
            deleted=store.upsert_job(platform='boss',platform_job_id='deleted',deleted_at='2026-11-21')
            store.record_delivery(self.a,status='failed')
            store.record_delivery(self.a,status='sent')
            store.add_reply(self.a,message_id='in1',direction='inbound',body='您好',chat_url='https://example.invalid/chat')
            store.add_reply(self.b,message_id='out1',direction='outbound',body='您好')
        http=patch.object(server,'api',side_effect=AssertionError('own must not call HTTP'))
        http.start();self.addCleanup(http.stop)
        browser=patch('browser.ego.EgoBrowser._call',side_effect=AssertionError('no browser'))
        browser.start();self.addCleanup(browser.stop)

    async def call(self,name,**kwargs):
        return (await server.mcp.call_tool(name,kwargs))[1]

    async def test_reads_and_requirements(self):
        result=await self.call('search_jobs',query='agent',cities=['杭州'],salary_min=24,salary_max=26)
        self.assertEqual(result['total'],1)
        self.assertEqual(result['items'][0]['status'],'replied')
        job=await self.call('get_job',job_id=str(self.a))
        self.assertIsNone(job['score_trace'])
        self.assertIsNone(job['score'])
        self.assertIn('SQL',job['jd'])
        self.assertEqual((await self.call('search_jobs',min_score=80))['total'],1)
        self.assertEqual((await self.call('search_jobs',offset=100))['items'],[])
        req=await self.call('jd_requirements',cities=['杭州'],platforms=[],since='2026-11-01',limit=1)
        self.assertEqual(req['jobs'],1)
        self.assertTrue(req['items'])
        with self.assertRaises(ValueError):
            own.get_job('99999')

    async def test_events_funnel_history_and_briefing(self):
        for event in ['manual_applied','manual_applied','interview_scheduled','offer']:
            result=await self.call('log_event',job_id=str(self.b),event=event,resume_version='v1')
            self.assertEqual(result['platform'],'liepin')
        self.assertFalse((self.home/'applications.jsonl').exists())
        for grouping in [None,'platform','city','score_band']:
            result=await self.call('funnel_stats',group_by=grouping,days=3)
            self.assertEqual(result['backend'],'own')
            self.assertEqual(result['total']['applied'],2)
            self.assertEqual(result['total']['replied'],2)
            self.assertEqual(result['total']['offers'],1)
            if grouping:
                self.assertEqual(sum(g['applied'] for g in result['groups']),2)
        self.assertEqual((await self.call('funnel'))['total']['greeted'],2)
        history=await self.call('company_history',company='乙公司')
        self.assertEqual(len(history['events']),4)
        self.assertEqual(len(history['jobs']),1)
        self.assertEqual((await self.call('briefing'))['funnel']['total']['offers'],1)
        detail=await self.call('briefing',job_id=str(self.a))
        self.assertEqual(detail['job']['chat_url'],'https://example.invalid/chat')
        self.assertIn('preferences',await self.call('profile'))
        self.assertEqual((await self.call('status'))['backend'],'own')

    async def test_scoring_never_contacts_bosshunter(self):
        from mcp.server.fastmcp.exceptions import ToolError
        with self.assertRaises(ToolError):
            await self.call('start_scoring',job_ids=[str(self.a)])
        preview=await self.call('preview_scoring',job_ids=[str(self.a)])
        self.assertEqual(preview['estimated_requests'],1)
        self.assertEqual((await self.call('scoring_runs'))['runs'],[])
        for name,args in [('pending_evaluations',{}),('submit_evaluations',{'evaluations':[]})]:
            result=await self.call(name,**args)
            self.assertFalse(result['supported'])

    async def test_imported_history_does_not_replace_current_snapshot(self):
        with Store() as store:
            ident=store.upsert_job(platform='boss',platform_job_id='old-history',legacy_status='approved')
            store.add_reply(ident,message_id='old-reply',direction='inbound',body='旧事件',id_origin='source_history')
        result=await self.call('funnel_stats')
        self.assertEqual(result['total']['applied'],2)
        self.assertEqual(result['total']['replied'],1)
        self.assertEqual((await self.call('get_job',job_id=str(ident)))['status'],'approved')

    async def test_native_queue_and_event_metadata(self):
        with Store() as store:
            store.connection.execute("INSERT INTO delivery_queue(job_id,status) VALUES(?,'unverified')",(self.b,))
            store.record_delivery(self.b,status='unverified')
        self.assertEqual((await self.call('get_job',job_id=str(self.b)))['status'],'unverified')
        await self.call('log_event',job_id=str(self.b),event='interview_scheduled',variant='test',chat_url='https://example.invalid/hr',note='local')
        result=await self.call('company_history',company='乙公司')
        self.assertEqual(result['events'][0]['variant'],'test')
        self.assertEqual(result['jobs'][0]['chat_url'],'https://example.invalid/hr')

    async def test_setup_readonly_and_missing_database(self):
        before=(self.home/'jobs.sqlite3').read_bytes()
        result=await self.call('setup_check')
        self.assertTrue(result['checks']['database']['ok'])
        self.assertNotIn('bosshunter',result['checks'])
        self.assertFalse(result['checks']['ego_spaces']['verified'])
        self.assertEqual(before,(self.home/'jobs.sqlite3').read_bytes())
        (self.home/'jobs.sqlite3').unlink()
        checks=own.setup_checks()
        self.assertFalse(checks['database']['ok'])
        self.assertFalse((self.home/'jobs.sqlite3').exists())

    async def test_read_tools_do_not_write_pool_or_legacy_events(self):
        (self.home/'applications.jsonl').write_text('invalid ignored legacy file')
        before=(self.home/'jobs.sqlite3').read_bytes()
        for name in ['funnel_stats','briefing','status','profile','search_jobs']:
            await self.call(name)
        self.assertEqual(before,(self.home/'jobs.sqlite3').read_bytes())
