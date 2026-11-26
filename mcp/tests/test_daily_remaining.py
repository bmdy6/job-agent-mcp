import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from browser.platform_lock import platform_lock, PlatformBusy
from collect.config import CollectSettings
from config import Settings
from service.daily import summarize, delivery_message


class RemainingTests(unittest.TestCase):
    def test_budget_tracks_pages_operations_pacing_and_manual_override(self):
        cfg=CollectSettings(platforms={'boss':{'enabled':True,'daily_page_limit':3}},
                            timeout_margin_seconds=100)
        p=cfg.rate_policy('boss')
        expected=6*sum(p.interval_seconds)/2 + 6/(sum(p.rest_every)/2)*sum(p.rest_seconds)/2+100
        self.assertEqual(cfg.timeout_seconds('boss'),expected)
        cfg.fetch_jd=True
        self.assertGreater(cfg.timeout_seconds('boss'),expected)
        cfg.platform_timeout_seconds=19
        self.assertEqual(cfg.timeout_seconds('boss'),19)

    def test_cross_process_lock_and_release_and_other_platform(self):
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ,JOB_AGENT_HOME=home):
            script='from browser.platform_lock import platform_lock,PlatformBusy\ntry:\n with platform_lock("boss"): pass\nexcept PlatformBusy: raise SystemExit(75)'
            with platform_lock('boss'):
                with self.assertRaises(PlatformBusy):
                    with platform_lock('boss'): pass
                with platform_lock('zhilian'): pass
                proc=subprocess.run([sys.executable,'-c',script],capture_output=True)
                self.assertEqual(proc.returncode,75,proc.stderr)
            self.assertEqual(subprocess.run([sys.executable,'-c',script]).returncode,0)

    def test_summary_timeout_and_zero_reason(self):
        cfg=Settings()
        report=dict(started_at='test',collection={'platforms':{'boss':{'error':'collection_timeout','pages':2,'last_page':4}}},
            delivery={'boss':dict(sent=0,failed=0,unverified=0,skipped=0,pending=0,reasons={})},
            candidate_counts={'boss':0},planning_exclusions={'boss':{'known_cooldown':9}})
        summarize(report,cfg)
        row=report['summary']['boss']
        self.assertEqual(row['cooldown_excluded'],9)
        self.assertTrue(row['zero_reason'])
        self.assertIn('采到第 4 页，因超时停止',delivery_message(report)[1])

    def test_mismatch_only_skips_on_positive_readonly_evidence(self):
        from platforms.boss.apply import BossAdapter
        from platforms.boss import pages
        from browser.base import FakeBrowser, FakePage
        from engine.models import Job
        adapter=BossAdapter(FakeBrowser(),{})
        adapter.page=FakePage('p1',{'boss.chat_identity':{'job_id':'wrong'}})
        adapter.space=object()
        adapter.job=Job('boss','target','https://www.zhipin.com/job_detail/target.html')
        for evidence,exception in [(True,pages.NotStarted),(False,pages.PlatformStateError)]:
            with patch('platforms.boss.receipt_reader.read_no_outgoing',return_value=evidence) as read:
                with self.assertRaises(exception): adapter._mismatched_chat('mismatch')
                self.assertEqual(read.call_args_list[0].args[1],'target')
                if evidence: self.assertEqual(read.call_args_list[1].args[1],'wrong')
        adapter.message_submitted=True
        with patch('platforms.boss.receipt_reader.read_no_outgoing') as read:
            with self.assertRaises(pages.PlatformStateError): adapter._mismatched_chat('mismatch')
            read.assert_not_called()
        adapter.message_submitted=False
        adapter.modal_submitted=True
        with patch('platforms.boss.receipt_reader.read_no_outgoing') as read:
            with self.assertRaises(pages.PlatformStateError): adapter._mismatched_chat('mismatch')
            read.assert_not_called()

    def test_readonly_absence_requires_exact_loaded_session_and_no_outgoing(self):
        from browser.base import FakePage, FakeSpace
        from platforms.boss import pages
        from platforms.boss.receipt_reader import read_no_outgoing
        for outgoing,ready,expected in [(False,True,True),(True,True,False),(False,False,False)]:
            data={'current_job_id':'target','current_conversation_id':'session','rows':[]}
            if outgoing: data['rows']=[{'id':'1','job_id':'target','conversation_id':'session','direction':'outgoing'}]
            state=dict(root=True,editor=True,input_empty=True,loading=not ready,visible_messages=len(data['rows']))
            page=FakePage('owned',{'boss.state':{},'boss.list':{'rows':[{'encryptJobId':'target','uniqueId':'session'}]},
                'boss.select_session':True,'boss.chat_identity':{'job_id':'target','conversation_id':'session'},
                'boss.messages':data,'boss.chat_ready':state})
            page.current_url=pages.CHAT_URL
            with patch('platforms.boss.receipt_reader.time.sleep'):
                self.assertEqual(read_no_outgoing(FakeSpace('audit',pages=[page]),'target'),expected)
            self.assertTrue(page.closed)
            self.assertFalse(any(a[0] in ('click','fill','press') for a in page.actions))

    def test_collection_busy_never_calls_source(self):
        from browser.base import FakeBrowser
        from collect.runner import Collector
        from store import Store
        from store.collection import CollectionRepository
        class Source:
            platform='boss'
            def search(self,*args): raise AssertionError('busy platform accessed')
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ,JOB_AGENT_HOME=home):
            with Store(Path(home)/'jobs.sqlite3') as store, platform_lock('boss'):
                result=Collector(FakeBrowser(),[Source()],CollectionRepository(store),
                    CollectSettings(platforms={'boss':{'enabled':True}})).run()
                self.assertEqual(result['platforms']['boss']['error'],'platform_busy')

    def test_resume_wrapper_defers_without_executing_child(self):
        script=Path(__file__).resolve().parents[2]/'scripts/platform-lock.py'
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ,JOB_AGENT_HOME=home):
            marker=Path(home)/'child-ran'
            args=[sys.executable,str(script),'boss','--',sys.executable,'-c',
                'from pathlib import Path; Path('+repr(str(marker))+').touch()']
            with platform_lock('boss'):
                proc=subprocess.run(args,capture_output=True)
                self.assertEqual(proc.returncode,75)
                self.assertFalse(marker.exists())
            self.assertEqual(subprocess.run(args).returncode,0)
            self.assertTrue(marker.exists())
