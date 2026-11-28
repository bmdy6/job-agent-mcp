"""BOSS monitor regressions from the 2026-11-26 chat DOM (synthetic data)."""
import json
import subprocess
import unittest
from job_agent.browser.base import FakePage, FakeSpace, PageTimeout
from job_agent.platforms.boss import pages
from job_agent.platforms.boss.replies import ReplySource


class MonitorSourceTests(unittest.TestCase):
    def page(self, rows, data=None):
        return FakePage('p2', {'boss.state': {}, 'boss.list': {'rows': rows},
            'boss.select_session': True, 'boss.messages': data if data is not None else {
                'current_job_id': 'job', 'current_conversation_id': 'session', 'rows': [
                    {'id': '123', 'job_id': 'job', 'conversation_id': 'session',
                     'time': 1795505482681, 'direction': 'incoming', 'body': 'PRIVATE_BODY'}]}})

    def test_zero_message_session_does_not_abort_other_replies(self):
        page=self.page([{'encryptJobId': 'empty', 'uniqueId': 'empty', 'lastMsgId': '0'},
            {'encryptJobId': 'job', 'uniqueId': 'session', 'lastMsgId': '123'}])
        events=ReplySource().fetch(FakeSpace('test', pages=[page]))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].body, 'PRIVATE_BODY')
        self.assertEqual(len([a for a in page.actions if a[0]=='click_projected']), 1)
        self.assertFalse(any(a[0] in {'fill','press'} for a in page.actions))
        self.assertTrue(page.closed)

    def test_failure_logs_stage_and_selector_without_message_or_identifiers(self):
        page=self.page([{'encryptJobId': 'PRIVATE_JOB', 'uniqueId': 'PRIVATE_SESSION', 'lastMsgId': '123'}])
        page.wait_for=lambda *a,**k: (_ for _ in ()).throw(PageTimeout('PRIVATE_BODY'))
        with self.assertLogs('job_agent.platforms.boss.replies', level='WARNING') as logs:
            with self.assertRaises(PageTimeout):ReplySource().fetch(FakeSpace('test', pages=[page]))
        output=' '.join(logs.output)
        self.assertIn('stage=list_ready',output)
        self.assertIn('selector=[role=listitem]',output)
        self.assertNotIn('PRIVATE', output)
        self.assertTrue(page.closed)

    def test_wrong_selected_session_even_without_events_is_failure(self):
        page=self.page([{'encryptJobId': 'job','uniqueId': 'session','lastMsgId': '123'}],
            {'current_job_id':'other','current_conversation_id':'other','rows':[]})
        with self.assertRaises(pages.PlatformStateError):
            ReplySource().fetch(FakeSpace('test',pages=[page]))


class ProjectionTests(unittest.TestCase):
    def run_js(self, setup, projection):
        return json.loads(subprocess.check_output(['node','-e',setup+'\nconsole.log(JSON.stringify(('+projection+')()));'],text=True))

    def test_list_uses_current_title_and_marks_zero_message_id(self):
        result=self.run_js('''global.document={querySelectorAll:()=>[
            {__vue__:{$props:{source:{encryptJobId:'job',uniqueId:'session',title:'Role',lastMsgId:0}}}}]};''',pages.LIST_JS)
        self.assertEqual(result['rows'][0]['jobName'], 'Role')
        self.assertEqual(result['rows'][0]['lastMsgId'], '0')

    def test_projection_scrolls_by_identity_and_rechecks_before_geometry(self):
        setup='''let scrolled=false;
const rect=()=>({x:10,y:scrolled?100:1500,right:200,bottom:scrolled?150:1550,width:190,height:50});
const target={getBoundingClientRect:rect,scrollIntoView:()=>{scrolled=true},contains:()=>false};
const row={__vue__:{$props:{source:{encryptJobId:'job',uniqueId:'session'}}},querySelector:()=>target};
global.innerWidth=800;global.innerHeight=600;
global.document={querySelectorAll:()=>[row],elementFromPoint:()=>target};'''
        result=self.run_js(setup,pages.select_session({'encryptJobId':'job','uniqueId':'session'}))
        self.assertEqual(result['y'],100)

class MonitorPipelineTests(unittest.TestCase):
    def test_parsed_reply_uses_monitor_rate_and_notification_dedup(self):
        import os
        import tempfile
        from unittest.mock import Mock, patch
        from job_agent.browser.base import FakeBrowser
        from job_agent.config import Settings
        from job_agent.monitor import Monitor
        from job_agent.store import Store
        import time
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, JOB_AGENT_HOME=home), \
                patch('job_agent.notify.send_one', return_value={'success':True}) as send:
            with Store() as store:
                job=store.upsert_job(platform='boss', platform_job_id='job', company='Synthetic', title='Role')
                store.record_delivery(job,status='sent')
                config=Settings.model_validate({'notify':{'enabled':True,'channels':{
                    'tg':{'enabled':True,'type':'telegram','chat_id':'fake'}}},
                    'engine':{'platforms':{'boss':{'daily_limit':0}}}})
                limiter=Mock()
                row={'encryptJobId':'job','uniqueId':'session','lastMsgId':'123','unreadCount':0}
                page=MonitorSourceTests().page([row])
                page.responses['boss.messages']['rows'][0].update(time=time.time(), body='职位已关闭，但可以聊另一个岗位')
                space=FakeSpace(config.engine.monitor.task_space_name,pages=[page])
                monitor=Monitor(FakeBrowser({space.name:space}),[ReplySource()],store,config,limiter=limiter)
                self.assertEqual(monitor.poll_once()['added'],0)  # Seed without opening.
                row['unreadCount']=1
                space._pages.append(MonitorSourceTests().page([row],page.responses['boss.messages']))
                result=monitor.poll_once()
                self.assertEqual(result['added'],1)
                self.assertEqual(result['sent'],1)
                self.assertEqual(result['failed'],0)
                self.assertIn('职位已关闭',send.call_args.args[2])
                self.assertEqual(limiter.acquire.call_args.args[:2],('boss','monitor'))
                self.assertIsNone(limiter.acquire.call_args.args[2].daily_limit)
                space._pages.append(MonitorSourceTests().page([row],page.responses['boss.messages']))
                self.assertEqual(monitor.poll_once()['sent'],0)
                send.assert_called_once()

class UnknownStructureTests(unittest.TestCase):
    def test_missing_message_id_is_not_treated_as_known_empty_conversation(self):
        page=MonitorSourceTests().page([{'encryptJobId':'job','uniqueId':'session'}])
        with self.assertRaises(pages.PlatformStateError):
            ReplySource().fetch(FakeSpace('test',pages=[page]))
        self.assertFalse(any(a[0]=='click_projected' for a in page.actions))
        self.assertTrue(page.closed)
