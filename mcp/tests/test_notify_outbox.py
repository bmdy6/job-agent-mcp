from state_helpers import read_state_file
from job_agent.store import Store
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from job_agent.config import TelegramSettings
import job_agent.notify as notify


class NotifyOutboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        env=patch.dict(os.environ,{'JOB_AGENT_HOME':self.tmp.name,'TG_TEST':'PRIVATE-TOKEN'},clear=True)
        env.start();self.addCleanup(env.stop)
        self.cfg=TelegramSettings(enabled=True,chat_id='123',bot_token_env='TG_TEST')
        self.path=Path(self.tmp.name)/'notify-outbox.jsonl'

    def test_transport_retry_exponential_and_restart_fifo(self):
        with patch.object(notify,'post',side_effect=notify.SendError('transport')) as post,patch.object(notify.time,'sleep') as sleep:
            result=notify.send_one(self.cfg,'old','message')
        self.assertEqual(post.call_count,3)
        self.assertEqual([c.args[0] for c in sleep.call_args_list],[1,2])
        self.assertTrue(result['queued'])
        self.assertEqual((self.path.parent/'jobs.sqlite3').stat().st_mode & 0o777,0o600)
        self.assertNotIn('PRIVATE',read_state_file(self.path))
        with patch.object(notify,'post',return_value={'ok':True}) as post:
            self.assertTrue(notify.send_one(self.cfg,'new','message')['success'])
        self.assertEqual([c.args[1]['text'].splitlines()[0] for c in post.call_args_list],['old','new'])
        self.assertEqual(read_state_file(self.path),'')

    def test_permanent_error_is_not_retried_or_queued(self):
        with patch.object(notify,'post',return_value={'ok':False,'error_code':403}) as post,patch.object(notify.time,'sleep') as sleep:
            result=notify.send_one(self.cfg,'title','body')
        self.assertEqual(post.call_count,1);sleep.assert_not_called()
        self.assertEqual(result,{'success':False,'error_code':'403'})
        self.assertFalse(self.path.exists() and read_state_file(self.path))

    def test_failed_replay_keeps_old_and_new_in_order(self):
        with patch.object(notify,'post',side_effect=notify.SendError('transport')),patch.object(notify.time,'sleep'):
            notify.send_one(self.cfg,'old','body')
            notify.send_one(self.cfg,'new','body')
        self.assertEqual([json.loads(x)['title'] for x in read_state_file(self.path).splitlines()],['old','new'])

    def test_changed_destination_never_receives_old_messages(self):
        with patch.object(notify,'post',side_effect=notify.SendError('transport')),patch.object(notify.time,'sleep'):
            notify.send_one(self.cfg,'old','body')
        cfg=self.cfg.model_copy(update={'chat_id':'456'})
        with patch.object(notify,'post',return_value={'ok':True}) as post:
            notify.send_one(cfg,'new','body')
        self.assertEqual(post.call_count,1)
        self.assertEqual(json.loads(read_state_file(self.path))['title'],'old')

    def test_explicit_one_shot_test_neither_queues_nor_drains(self):
        with patch.object(notify,'post',side_effect=notify.SendError('transport')),patch.object(notify.time,'sleep'):
            notify.send_one(self.cfg,'old','body')
        original=read_state_file(self.path).encode()
        from job_agent.config import Settings
        cfg=Settings(notify={'channels':{'tg':self.cfg.model_dump()}})
        with patch.object(notify,'load_config',return_value=cfg),patch.object(notify,'post',side_effect=notify.SendError('transport')) as post:
            notify.notify_test('tg')
        self.assertEqual(post.call_count,1);self.assertEqual(original,read_state_file(self.path).encode())

    def test_deadline_stops_retries_before_sixty_seconds(self):
        clock=[0.0]
        def fail(*_):
            clock[0]+=49
            raise notify.SendError('transport')
        with patch.object(notify.time,'monotonic',side_effect=lambda:clock[0]),patch.object(notify,'post',side_effect=fail) as post,patch.object(notify.time,'sleep') as sleep:
            result=notify.send_one(self.cfg,'title','body')
        self.assertEqual(post.call_count,1);sleep.assert_not_called()
        self.assertLess(clock[0],60);self.assertTrue(result['queued'])

    def test_corrupt_or_unwritable_outbox_is_reported_without_losing_old_data(self):
        self.path.write_text('broken PRIVATE queue\n')
        with patch.object(notify,'post') as post,self.assertLogs('job_agent.notify',level='WARNING') as logs:
            result=notify.send_one(self.cfg,'title','body')
        post.assert_not_called()
        self.assertEqual(result['error_code'],'outbox_storage')
        self.assertEqual(self.path.read_text(),'broken PRIVATE queue\n')
        self.assertNotIn('PRIVATE',str(logs.output))
        self.path.unlink()
        with patch('job_agent.store.Store.enqueue_notification',side_effect=OSError('PRIVATE disk')),patch.object(notify,'post') as post:
            self.assertEqual(notify.send_one(self.cfg,'title','body')['error_code'],'outbox_storage')
        post.assert_not_called()

    def test_disabled_channels_do_not_drain_and_token_change_is_isolated(self):
        with patch.object(notify,'post',side_effect=notify.SendError('transport')),patch.object(notify.time,'sleep'):
            notify.send_one(self.cfg,'old','body')
        original=read_state_file(self.path).encode()
        from job_agent.config import NotifySettings
        with patch.object(notify,'post') as post:
            self.assertEqual(notify.send_channels(NotifySettings(enabled=False,channels={'tg':self.cfg}),'new','body'),{})
        post.assert_not_called();self.assertEqual(read_state_file(self.path).encode(),original)
        with patch.dict(os.environ,{'TG_TEST':'CHANGED-TOKEN'}),patch.object(notify,'post',return_value={'ok':True}) as post:
            notify.send_one(self.cfg,'new','body')
        self.assertEqual(post.call_count,1);self.assertEqual(read_state_file(self.path).encode(),original)

    def test_cross_process_producers_do_not_lose_each_others_messages(self):
        import subprocess,sys
        script='''from unittest.mock import patch
from job_agent.config import TelegramSettings
import job_agent.notify as n
import sys
with patch.object(n,'post',side_effect=n.SendError('transport')),patch.object(n.time,'sleep'):
 n.send_one(TelegramSettings(enabled=True,chat_id='123',bot_token_env='TG_TEST'),sys.argv[1],'body')
'''
        children=[subprocess.Popen([sys.executable,'-c',script,str(i)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,env={**os.environ,'PYTHONPATH':str(Path(__file__).resolve().parents[1])}) for i in range(4)]
        for child in children:
            out,err=child.communicate(timeout=10)
            self.assertEqual(child.returncode,0,err)
        rows=[json.loads(line) for line in read_state_file(self.path).splitlines()]
        self.assertEqual({r['title'] for r in rows},{str(i) for i in range(4)})
        self.assertEqual(len(rows),4)

    def test_success_receipt_is_saved_before_later_replay_fails(self):
        with patch.object(notify,'post',side_effect=notify.SendError('transport')),patch.object(notify.time,'sleep'):
            notify.send_one(self.cfg,'one','body');notify.send_one(self.cfg,'two','body')
        with patch.object(notify,'post',side_effect=[{'ok':True}]+[notify.SendError('transport')]*3),patch.object(notify.time,'sleep'):
            notify.send_one(self.cfg,'three','body')
        self.assertEqual([json.loads(x)['title'] for x in read_state_file(self.path).splitlines()],['two','three'])

    def test_same_pending_message_is_not_enqueued_twice(self):
        with patch.object(notify,'post',side_effect=notify.SendError('transport')),patch.object(notify.time,'sleep'):
            notify.send_one(self.cfg,'same','body');notify.send_one(self.cfg,'same','body')
        self.assertEqual(len(read_state_file(self.path).splitlines()),1)
        with patch.object(notify,'post',return_value={'ok':True}) as post:
            self.assertTrue(notify.send_one(self.cfg,'same','body')['success'])
        self.assertEqual(post.call_count,1)

    def test_trickling_http_body_stops_at_shared_deadline_after_connection_detaches(self):
        from unittest.mock import MagicMock
        clock=[0.0];connection=MagicMock();sock=connection.sock
        response=connection.getresponse.return_value
        response.status=200
        def detach():
            connection.sock=None
            return response
        connection.getresponse.side_effect=detach
        def trickle(_):
            clock[0]+=5
            return b' '
        response.read1.side_effect=trickle
        with patch.object(notify,'HTTPSConnection',return_value=connection),patch.object(notify.time,'monotonic',side_effect=lambda:clock[0]):
            deadline=notify._SEND_DEADLINE.set(17)
            try:
                with self.assertRaises(notify.SendError) as ctx:notify.post('https://example.invalid',{})
            finally:notify._SEND_DEADLINE.reset(deadline)
        self.assertEqual(ctx.exception.code,'transport')
        self.assertLess(clock[0],60)
        self.assertEqual(sock.settimeout.call_args.args[0],2)
        connection.close.assert_called_once()
