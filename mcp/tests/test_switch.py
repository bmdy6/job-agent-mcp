import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from config import Settings
from store import SQLiteDeliveryStore

class SwitchTests(unittest.TestCase):
    def test_backend_and_verified_limits(self):
        self.assertEqual(Settings().engine.backend, 'bosshunter')
        for p, cap in [('boss',1000),('zhilian',500),('liepin',800)]:
            with self.assertRaises(ValueError):
                Settings.model_validate({'engine':{'backend':'own','platforms':{p:{'greeting_max_length':cap+1,'greeting':'x'*(cap+1)}}}})

    def test_own_preview_disabled_live_and_exclusions(self):
        from service.mcp_backend import preview, send
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, JOB_AGENT_HOME=''):
            os.environ['JOB_AGENT_HOME']=home
            cfg=Settings.model_validate({'engine':{'backend':'own','platforms':{'boss':{'enabled':True,'greeting':'x'*301,'greeting_max_length':1000}}}})
            with SQLiteDeliveryStore(dry_run=False) as db:
                ids=[str(db.upsert_job(platform='boss',platform_job_id=str(i),url='https://example.com/'+str(i),title='AI')) for i in range(2)]
            with patch('service.mcp_backend.load_config',return_value=cfg):
                out=preview(ids,None)
                self.assertEqual(len(out['items']),2)
                self.assertEqual(len(out['items'][0]['greeting']),301)
                with self.assertRaises(ValueError):send(out['confirm_token'],None)
                cfg.engine.dry_run=False
                cfg.engine.live_send_enabled=True
                # Configuration changed after preview: fail closed.
                with self.assertRaises(ValueError):send(out['confirm_token'],None)

if __name__=='__main__':unittest.main()

class ConfirmationTests(unittest.TestCase):
    def test_excluded_pending_job_never_runs_and_attachment_is_per_platform(self):
        from service.deliver import preview,confirm
        from engine.models import Job,Status
        with tempfile.TemporaryDirectory() as home:
            cfg=Settings.model_validate({'engine':{'platforms':{'boss':{'enabled':True,'greeting':'hello','greeting_max_length':1000,'interval_seconds':0},'51job':{'enabled':True,'greeting':'hello','greeting_max_length':301,'interval_seconds':0,'attachment_name':'resume'}}}})
            with SQLiteDeliveryStore(Path(home)/'dry.db') as db:
                ids=[db.upsert_job(platform=p,platform_job_id=p,url='https://example.com/'+p) for p in ['boss','51job']]
                db.add(Job('boss','boss','https://example.com/boss'))
                token=json.loads(preview(db,cfg,job_ids=ids))['token']
                text=confirm(db,cfg,token,exclude_ids=[str(ids[0])])
                self.assertIn('成功 1',text)
                self.assertEqual(db.records()[0].status,Status.PENDING)
                self.assertEqual(db.list_deliveries()[-1]['attachment_name'],'resume')

    def test_ttl_replay_job_change_and_worker_stop(self):
        from service.mcp_backend import preview,send
        from service import tasks
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ,JOB_AGENT_HOME=''):
            os.environ['JOB_AGENT_HOME']=home
            cfg=Settings.model_validate({'engine':{'backend':'own','dry_run':False,'live_send_enabled':True,'platforms':{'boss':{'enabled':True,'greeting':'hello','greeting_max_length':1000}}}})
            with SQLiteDeliveryStore(dry_run=False) as db:
                ident=str(db.upsert_job(platform='boss',platform_job_id='1',url='https://example.com/1'))
            with patch('service.mcp_backend.load_config',return_value=cfg), patch('service.tasks.start',return_value={'status':'running'}) as start:
                token=preview([ident])['confirm_token']
                with patch('service.mcp_backend.time.time',return_value=10**12),self.assertRaises(ValueError):send(token)
                start.assert_not_called()
                send(token)
                with self.assertRaises(ValueError):send(token)
                self.assertEqual(start.call_count,1)
                token=preview([ident])['confirm_token']
                with SQLiteDeliveryStore(dry_run=False) as db:db.upsert_job(platform='boss',platform_job_id='1',url='https://example.com/changed')
                with self.assertRaises(ValueError):send(token)
            with tasks.locked('delivery') as path:
                tasks.write(path,{'id':'test','pid':os.getpid(),'status':'running','stop_path':str(Path(home)/'test.stop')})
            self.assertTrue(tasks.stop('delivery')['stopped'])
            self.assertTrue((Path(home)/'test.stop').exists())

class RoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_six_tools_use_own_without_http(self):
        import server
        cfg=Settings.model_validate({'engine':{'backend':'own'}})
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ,JOB_AGENT_HOME=home),patch('issue6.load_config',return_value=cfg),patch('server.api',side_effect=AssertionError('HTTP forbidden')):
            for tool,target,args in [
                ('preview_greetings','service.mcp_backend.preview',{}),
                ('send_greetings','service.mcp_backend.send',{'confirm_token':'test'}),
                ('delivery_status','service.mcp_backend.status',{}),
                ('stop_delivery','service.tasks.stop',{}),
                ('start_reply_monitor','service.monitor.start',{'confirm':True}),
                ('stop_reply_monitor','service.monitor.stop',{})]:
                with patch(target,return_value={'ok':True}) as call:
                    await server.mcp.call_tool(tool,args)
                    call.assert_called_once()
            with patch('service.monitor.start') as start:
                with self.assertRaises(Exception):await server.mcp.call_tool('start_reply_monitor',{'confirm':False})
                start.assert_not_called()
