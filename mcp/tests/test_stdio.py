"""Real MCP stdio acceptance uses one temporary owned database, without network."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from store import Store

ROOT = Path(__file__).resolve().parents[1]


class StdioTest(unittest.IsolatedAsyncioTestCase):
    async def test_owned_tools_filters_context_gates_and_redacted_audit(self):
        with tempfile.TemporaryDirectory() as home:
            path = Path(home)
            (path/'config.yaml').write_text('mode: practice\ndirections: [Agent]\ncollect:\n  platforms:\n    boss: {enabled: false}\n')
            (path/'source.md').write_text('PRIVATE-RESUME')
            with Store(path/'jobs.sqlite3') as db:
                ident = str(db.upsert_job(platform='boss', platform_job_id='fake', title='Agent 开发', company='合成公司',
                    city='杭州', salary='10-20K', jd='任职要求：\n熟悉 SQL。', legacy_status='approved', legacy_score=90,
                    url='https://example.invalid/job', collected_at='2026-11-26T00:00:00Z'))
                db.upsert_job(platform='liepin', platform_job_id='other', city='上海', title='其他岗位')
            parameters = StdioServerParameters(command=sys.executable, args=[str(ROOT/'server.py')],
                env={**os.environ, 'JOB_AGENT_HOME': home, 'JOB_AGENT_CALLER': 'unit-stdio'})
            calls = 0
            async with stdio_client(parameters) as (reader, writer):
                async with ClientSession(reader, writer) as client:
                    await client.initialize()
                    tools = {tool.name: tool for tool in (await client.list_tools()).tools}
                    self.assertEqual(set(tools), {'dashboard', 'status', 'resume_delivery_status', 'review_resume_application_record',
                        'search_jobs', 'get_job', 'check_collection', 'start_collection', 'preview_scoring', 'start_scoring',
                        'scoring_runs', 'jd_requirements', 'profile', 'experience_list', 'experience_get', 'experience_add',
                        'company_get', 'company_save', 'log_event', 'company_history', 'funnel', 'setup_check',
                        'resume_import', 'briefing', 'funnel_stats', 'greeting_targets', 'preview_greetings', 'send_greetings',
                        'delivery_status', 'stop_delivery', 'start_reply_monitor', 'stop_reply_monitor',
                        'reply_monitor_status', 'notify_test', 'greeting_stats', 'interview_schedule'})
                    for name in ['search_jobs', 'funnel_stats', 'greeting_stats', 'interview_schedule']:
                        self.assertTrue(tools[name].annotations.readOnlyHint)
                    self.assertFalse(tools['send_greetings'].annotations.readOnlyHint)
                    async def call(name, args=None, error=False):
                        nonlocal calls
                        result = await client.call_tool(name, args or {})
                        calls += 1
                        self.assertEqual(bool(result.isError), error, result.content)
                        return result.structuredContent
                    result = await call('search_jobs', {'query':'Agent','cities':['杭州'], 'platforms':['boss'],
                        'salary_min':15,'salary_max':25,'min_score':80,'statuses':['approved']})
                    self.assertEqual(result['total'], 1)
                    self.assertEqual(result['items'][0]['id'], ident)
                    self.assertNotIn('jd', result['items'][0])
                    self.assertIn('SQL', (await call('get_job', {'job_id':ident}))['jd'])
                    self.assertEqual((await call('jd_requirements', {'since':'2026-11-26'}))['items'][0]['text'], '熟悉 SQL')
                    self.assertEqual((await call('profile'))['mode'], 'practice')
                    exp = await call('experience_add', {'title':'素材','md':'PRIVATE-EXPERIENCE'})
                    self.assertEqual((await call('experience_get', {'id':exp['id']}))['item']['md'], 'PRIVATE-EXPERIENCE')
                    self.assertEqual(len((await call('experience_list'))['items']), 1)
                    await call('company_save', {'company':'合成公司','md':'PRIVATE-COMPANY','source_url':'https://example.invalid'})
                    self.assertEqual((await call('company_get', {'company':'合成公司'}))['item']['md'], 'PRIVATE-COMPANY')
                    await call('log_event', {'job_id':ident,'event':'interview_scheduled','note':'PRIVATE-NOTE'})
                    self.assertEqual(len((await call('company_history', {'company':'合成公司'}))['events']), 1)
                    self.assertEqual((await call('funnel'))['total']['greeted'], 1)
                    self.assertEqual((await call('funnel_stats'))['total']['interview_scheduled'], 1)
                    self.assertIn('job', await call('briefing', {'job_id':ident}))
                    self.assertEqual((await call('resume_import', {'path':str(path/'source.md')}))['text'], 'PRIVATE-RESUME')
                    self.assertEqual((await call('preview_scoring', {'job_ids':[ident]}))['estimated_requests'], 0)
                    self.assertEqual((await call('scoring_runs'))['runs'], [])
                    for name,args in [('start_scoring',{'job_ids':[ident]}),
                                      ('start_collection',{'platforms':['boss'],'confirm':'true'}),
                                      ('start_reply_monitor',{'confirm':False}),
                                      ('send_greetings',{'confirm_token':'invalid'}),
                                      ('jd_requirements',{'limit':0})]:
                        await call(name, args, error=True)
                    self.assertFalse((await call('check_collection', {'platforms':['boss']}))['ok'])
                    for name in ['greeting_stats','interview_schedule','greeting_targets']:
                        self.assertIsInstance(await call(name), dict)
            raw = (path/'activity.jsonl').read_text()
            self.assertNotIn('PRIVATE', raw)
            self.assertNotIn('example.invalid', raw)
            entries = [json.loads(line) for line in raw.splitlines()]
            self.assertEqual(len(entries), calls)
            self.assertTrue(all(e['caller']=='unit-stdio' for e in entries))
            self.assertFalse((path/'applications.jsonl').exists())
