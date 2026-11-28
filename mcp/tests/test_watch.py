from state_helpers import read_state_file, write_state_file
"""Owned saved-reply notification entry; all effects use temporary stores and fake providers."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from job_agent.config import Settings
from job_agent.store import Store
from job_agent.watch import poll_once

ROOT = Path(__file__).resolve().parents[1]


class WatchTest(unittest.TestCase):
    def test_default_source_uses_store_and_deduplicates_across_cli_restarts(self):
        with tempfile.TemporaryDirectory() as home:
            Path(home,'config.yaml').write_text('notify:\n  enabled: true\n  channels:\n    tg: {type: telegram, enabled: true, chat_id: "123"}\n')
            with Store(Path(home,'jobs.sqlite3')) as db:
                jid = db.upsert_job(platform='boss',platform_job_id='fake',title='合成岗位',company='合成公司')
                db.record_delivery(jid,status='sent')
                db.add_reply(jid,message_id='fake-message',direction='inbound',body='PRIVATE HR reply',occurred_at='2099-01-01T00:00:00Z')
            runner = "from job_agent import notify,cli; notify.send_one=lambda *a,**k:{'success':True}; raise SystemExit(cli.main(['watch','--once']))"
            for expected in [1,0]:
                result=subprocess.run([sys.executable,'-c',runner],env={**os.environ,'JOB_AGENT_HOME':home,'PYTHONPATH':str(ROOT)},capture_output=True,text=True,timeout=15)
                self.assertEqual(result.returncode,0,result.stderr)
                self.assertEqual(json.loads(result.stdout)['sent'],expected)
            self.assertNotIn('PRIVATE',read_state_file(Path(home)/'notified.jsonl'))

    def test_disabled_watch_does_not_open_database(self):
        with patch('job_agent.store.Store', side_effect=AssertionError('database must stay closed')):
            self.assertEqual(poll_once(config=Settings())['sent'],0)

    def test_explicit_replies_source_and_injected_store(self):
        cfg=Settings(notify={'enabled':True,'channels':{'tg':{'type':'telegram','enabled':True}}})
        with patch('job_agent.monitor.notifications.poll_replies',return_value={'sent':1}) as notify:
            store=object()
            self.assertEqual(poll_once(cfg,source='replies',store=store)['sent'],1)
            notify.assert_called_once_with(store,cfg)
