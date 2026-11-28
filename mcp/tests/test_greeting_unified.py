import unittest
from job_agent.config import Settings
from job_agent.platforms.greeting import lint_greeting

class ConfigTests(unittest.TestCase):
    def test_inherit_and_override(self):
        c=Settings(engine={'greeting':'全局', 'greeting_newline_separator':' ', 'platforms':{
            'boss':{'greeting_max_length':1000},
            'zhilian':{'greeting':'覆盖','greeting_max_length':500},
            '51job':{'greeting':'','greeting_max_length':1000}}})
        self.assertEqual(c.engine.platforms['boss'].greeting,'全局')
        self.assertEqual(c.engine.platforms['boss'].greeting_newline_separator,' ')
        self.assertEqual(c.engine.platforms['zhilian'].greeting,'覆盖')
        self.assertEqual(c.engine.platforms['51job'].greeting,'')
    def test_old_config_and_disabled_generator(self):
        c=Settings(engine={'platforms':{'boss':{'greeting':'旧文案','greeting_max_length':1000}}})
        self.assertEqual(c.engine.platforms['boss'].greeting,'旧文案')
        self.assertFalse(c.greetings.generate.enabled)

import json
import tempfile
from pathlib import Path
from unittest.mock import patch
from job_agent.service.greeting_generation import generate, validate_result
from job_agent.service.deliver import preview
from job_agent.store import SQLiteDeliveryStore

FACT='主导开发31个知识库的问答服务。处理过旧文档干扰回答的问题。'
TEXT = '你好，我是候选人，做软件开发的。已离职，随时能到岗。\n\n看你们在做企业知识库检索。这块我做过，之前给31个知识库做过问答。旧文档把答案带偏的问题也处理过。\n\n简历发你看看？'

class GenerationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        env=patch.dict('os.environ',JOB_AGENT_HOME=self.temp.name);env.start();self.addCleanup(env.stop)
    def config(self):
        return Settings(greetings={'generate':{'enabled':True,'facts':[FACT]}},engine={'platforms':{
            'boss':{'enabled':True,'greeting':'固定文案','greeting_max_length':1000,'interval_seconds':0,'daily_limit':10}}})
    def job(self):
        return {'platform':'boss','platform_job_id':'one','company':'公司','title':'AI开发','jd':'企业知识库检索'}
    def test_generate_payload_and_one_fact(self):
        response={'choices':[{'message':{'content':json.dumps({'text':TEXT,'fact':FACT,'experience':TEXT.split('\n\n')[1].split('。',1)[1],'company_hook':'企业知识库检索'})}}]}
        with patch('job_agent.service.scoring.resolve_key',return_value='secret'),patch('job_agent.service.scoring.request_completion',return_value=(response,{})) as call:
            text,trace=generate(self.job(),self.config())
        self.assertEqual(text,TEXT)
        self.assertEqual(trace['source'],'generated')
        payload=call.call_args.args[0]
        self.assertEqual(set(json.loads(payload['messages'][1]['content'])),{'company','title','jd','company_intro','facts','blacklist','jargon_terms','direction','few_shot','hook_prefixes','hook_suffixes','previous_entries','closing_line','similarity_threshold','max_sentence_pattern'})
        self.assertEqual(payload['service_tier'],'priority')
        call.assert_called_once()
    def test_lint_numbers_and_fallback(self):
        for bad in (TEXT+'贵司',TEXT+'99%',TEXT*3):
            with self.assertRaises(ValueError):validate_result({'text':bad,'fact':FACT,'experience':TEXT.split('\n\n')[1].split('。',1)[1]},[FACT])
        with self.assertRaises(ValueError):validate_result({'text':TEXT,'fact':'虚构成果'},[FACT])
        with patch('job_agent.service.scoring.resolve_key',side_effect=RuntimeError('secret')):
            text,trace=generate(self.job(),self.config())
        self.assertTrue(lint_greeting(text)['ok'])
        self.assertEqual(trace['source'],'fallback');self.assertEqual(trace['reason'],'RuntimeError')
    def test_snapshot_execution_and_history(self):
        from job_agent.engine.queue import DeliveryEngine
        from job_agent.engine.adapters import FakeAdapter
        from job_agent.engine.preflight import FakePreflight
        from job_agent.engine.spaces import FakeTaskSpaces
        cfg=self.config()
        with tempfile.TemporaryDirectory() as temp,SQLiteDeliveryStore(Path(temp)/'db') as store:
            ids=[store.upsert_job(platform='boss',platform_job_id=p,url='https://example.invalid/'+p) for p in ('one','two')]
            with patch('job_agent.service.greeting_generation.generate',side_effect=[('岗位甲',{'source':'generated'}),('岗位乙',{'source':'generated'})]):
                batch=json.loads(preview(store,cfg,job_ids=ids))
            store.confirm_batch(batch['token'],config=cfg.engine.model_dump())
            greetings={'boss':'固定文案','boss:one':'被改文案'}
            with self.assertRaises(ValueError):store.prepare_run(cfg.engine.model_dump(),batch_token=batch['token'],greetings=greetings)
            with self.assertRaises(Exception):
                store.connection.execute("UPDATE batches SET greetings_json='{}' WHERE token=?",(batch['token'],))
            adapter=FakeAdapter()
            DeliveryEngine(store,cfg.engine,{'boss':adapter},FakePreflight(),FakeTaskSpaces(),sleep=lambda _:None).run(batch_token=batch['token'])
            self.assertEqual(adapter.greetings,['岗位甲','岗位乙'])
            self.assertEqual({r['greeting'] for r in store.list_deliveries()},{'岗位甲','岗位乙'})

class DefaultSyncTests(unittest.TestCase):
    def test_preview_equal_confirm_readback_and_no_repeat(self):
        from job_agent.platforms.zhilian import default_greeting as site
        from unittest.mock import Mock
        current={'origin':'https://i.zhaopin.com','path':'/im/greeting/setting','ready':True,
                 'rows':[{'id':1,'text':'旧文案','selected':True},{'id':2,'text':'新文案','selected':False}]}
        page=Mock()
        page.evaluate.return_value=current
        self.assertFalse(site.synchronize(page,'新文案')['changed'])
        page.click.assert_not_called()
        self.assertTrue(site.synchronize(page,'旧文案',True)['verified'])
        page.click.assert_not_called()
        after={**current,'rows':[{'id':1,'text':'旧文案','selected':False},{'id':2,'text':'新文案','selected':True}]}
        page.evaluate.side_effect=[current,after]
        self.assertTrue(site.synchronize(page,'新文案',True)['changed'])
        self.assertEqual(page.click.call_args_list[0].args[0],'#greeting-item-2 .greeting-list__default')
        page.reset_mock()
        page.evaluate.side_effect=[current,current]
        with self.assertRaises(ValueError):site.synchronize(page,'新文案',True)
        self.assertEqual(sum(c.args[0].endswith('.greeting-list__default') for c in page.click.call_args_list),1)
    def test_add_save_and_reload(self):
        from job_agent.platforms.zhilian import default_greeting as site
        from unittest.mock import Mock
        current={'origin':'https://i.zhaopin.com','path':'/im/greeting/setting','ready':True,
                 'rows':[{'id':1,'text':'旧文案','selected':True}]}
        added={**current,'rows':current['rows']+[{'id':2,'text':'新文案','selected':False}]}
        final={**current,'rows':[{'id':2,'text':'新文案','selected':True}]}
        page=Mock();page.evaluate.side_effect=[current,'新文案',added,final]
        self.assertTrue(site.synchronize(page,'新文案',True)['verified'])
        self.assertEqual(page.goto.call_count,2)
        self.assertIn(((site.SAVE,),{}),page.click.call_args_list)
    def test_51job_unsupported_never_writes_or_sends(self):
        from job_agent.service.greeting_sync import sync_default_greeting
        self.assertFalse(sync_default_greeting('51job',True)['supported'])
    def test_51job_message_mode_rejected_before_application(self):
        from job_agent.platforms.job51.apply import Job51Adapter
        from job_agent.platforms.job51.pages import NotStarted
        from types import SimpleNamespace
        from unittest.mock import Mock
        a=Job51Adapter(Mock(),SimpleNamespace(greeting_mode='send_message',greeting_max_length=1000))
        a.page=Mock()
        with self.assertRaises(NotStarted):a.send_greeting('test')
        a.page.click.assert_not_called()
    def test_51job_receipt_requires_matching_success(self):
        from job_agent.platforms.job51.pages import attachment_receipt
        def data(status=1,ident='a',logs=None):
            return {'receipt':{'sendStatus':status,'params':{'attachResumeId':ident,'cvLogIdList':logs or ['job']}}}
        self.assertTrue(attachment_receipt(data(),'a',['job']))
        for d in (data(0),data(ident='wrong'),data(logs=['other']),{}):
            self.assertFalse(attachment_receipt(d,'a',['job']))

class CompatibilityTests(unittest.TestCase):
    setUp = GenerationTests.setUp
    def test_assignment_preserves_platform_models(self):
        c=Settings(engine={'greeting':'全局默认'})
        c.engine.dry_run=False
        self.assertEqual(c.engine.platforms['boss'].greeting,'全局默认')
        self.assertEqual(c.engine.platforms['51job'].greeting,'全局默认')
    def test_lint_rejection_is_fallback_with_reason(self):
        c=GenerationTests().config()
        response={'choices':[{'message':{'content':json.dumps({'text':TEXT+'贵司','fact':FACT,'experience':TEXT.split('\n\n')[1].split('。',1)[1],'company_hook':'企业知识库检索'})}}]}
        with patch('job_agent.service.scoring.resolve_key',return_value='secret'),patch('job_agent.service.scoring.request_completion',return_value=(response,{})):
            text,trace=generate(GenerationTests().job(),c)
        self.assertTrue(lint_greeting(text)['ok'])
        self.assertIn('ai_cliche',trace['reason'])
    def test_non_composer_has_explicit_fallback(self):
        c=GenerationTests().config();c.engine.platforms['boss'].greeting_mode='platform_default'
        with patch('job_agent.service.scoring.request_completion') as call:
            self.assertEqual(generate(GenerationTests().job(),c)[1]['reason'],'mode_has_no_per_job_composer')
        call.assert_not_called()
    def test_numbers_reject_fullwidth_and_chinese_quantities(self):
        for suffix in ('９９％','十万用户','一年经验'):
            with self.assertRaisesRegex(ValueError,'number_outside_fact'):
                validate_result({'text':TEXT+suffix,'fact':FACT,'experience':TEXT.split('\n\n')[1].split('。',1)[1]},[FACT])
