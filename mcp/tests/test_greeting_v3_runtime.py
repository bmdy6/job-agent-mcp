"""Generation is private, bounded, cached and independent of an interactive Agent."""
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from job_agent.config import Settings
from job_agent.platforms.greeting import lint_greeting
from job_agent.service.greeting_generation import generate
from job_agent.service.prompt_files import render_prompt
GENERIC_FALLBACK = render_prompt('fallback')

FACT = '主导开发31个知识库的问答服务。处理过旧文档干扰回答的问题。'
HOOK = "企业知识库检索"
TEXT = '你好，我是候选人，做软件开发的。已离职，随时能到岗。\n\n看你们在做企业知识库检索。这块我做过，之前给31个知识库做过问答。旧文档把答案带偏的问题也处理过。\n\n简历发你看看？'

class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, JOB_AGENT_HOME=self.temp.name)
        self.env.start(); self.addCleanup(self.env.stop)
        self.key = patch('job_agent.service.scoring.resolve_key', return_value='synthetic')
        self.key.start(); self.addCleanup(self.key.stop)
        self.call = patch('job_agent.service.scoring.request_completion')
        self.request = self.call.start(); self.addCleanup(self.call.stop)
        self.request.return_value = (self.response(), {})
        self.cfg = Settings(greetings={'generate':{'enabled':True,'facts':[FACT]}},
            engine={'platforms':{'boss':{'greeting_max_length':1000,'greeting':'旧文案'}}})
        self.job = dict(platform='boss',platform_job_id='one',jd=HOOK,title='RAG开发')

    def response(self, text=TEXT, hook=HOOK):
        return {'choices':[{'finish_reason':'stop','message':{'content':json.dumps(dict(text=text,fact=FACT,experience=text.split('\n\n')[1].split('。',1)[1],company_hook=hook))}}],
                'usage':{'prompt_tokens':20,'completion_tokens':50,'total_tokens':70}}

    def test_cache_and_usage_and_unlimited_payload(self):
        text, trace = generate(self.job, self.cfg)
        self.assertEqual(text,TEXT)
        self.assertTrue(lint_greeting(text)['ok'])
        payload=self.request.call_args.args[0]
        self.assertEqual(payload['model'],'gpt-6.1-sol')
        self.assertEqual(payload['service_tier'],'priority')
        self.assertFalse({'max_tokens','max_completion_tokens','thinking','reasoning_effort'} & payload.keys())
        self.assertEqual(generate(self.job,self.cfg)[0],TEXT)
        self.request.assert_called_once()
        self.assertTrue(generate(self.job,self.cfg)[1]['cache_hit'])
        with sqlite3.connect(Path(self.temp.name)/'greeting-generation.sqlite3') as db:
            row=db.execute('SELECT run_id,total_tokens,duration_ms,status FROM calls').fetchone()
            self.assertTrue(row[0]); self.assertEqual(row[1],70);self.assertGreaterEqual(row[2],0);self.assertEqual(row[3],'generated')
        generate({**self.job,'jd':HOOK+'开发'},self.cfg)
        self.assertEqual(self.request.call_count,2)

    def test_two_invalid_results_use_linted_generic_fallback_and_cache(self):
        self.request.return_value=(self.response(TEXT+'贵司'),{})
        text,trace=generate(self.job,self.cfg)
        self.assertEqual(self.request.call_count,2)
        self.assertEqual(trace['source'],'fallback');self.assertEqual(trace['attempts'],2)
        self.assertTrue(lint_greeting(text)['ok'])
        self.assertEqual(len(text.split('\n\n')),3)
        self.assertEqual(text, GENERIC_FALLBACK)
        generate(self.job,self.cfg);self.assertEqual(self.request.call_count,2)

    def test_new_generic_fallback_passes_default_lint(self):
        self.assertTrue(GENERIC_FALLBACK.startswith('你好，我是候选人，做软件开发的。'))
        lint = lint_greeting(GENERIC_FALLBACK)
        self.assertTrue(lint['ok'], lint['issues'])
        self.assertGreaterEqual(lint['length'], 70)
        self.assertLessEqual(lint['length'], 120)
        self.cfg.greetings.generate.facts = []
        text, trace = generate(self.job, self.cfg)
        self.assertEqual(text, GENERIC_FALLBACK)
        self.assertEqual(trace['source'], 'fallback')
        self.assertEqual(trace['reason'], 'facts_empty')
        self.assertTrue(trace['lint']['ok'])
        self.request.assert_not_called()

    def test_choppy_results_retry_then_cache_linted_fallback_with_reason(self):
        choppy = TEXT.replace('这块我做过，', '这块我做过。')
        self.assertEqual({i['code'] for i in lint_greeting(choppy)['issues']}, {'choppy'})
        self.request.return_value = (self.response(choppy), {})
        text, trace = generate(self.job, self.cfg)
        self.assertEqual(text, GENERIC_FALLBACK)
        self.assertEqual(trace['source'], 'fallback')
        self.assertEqual(trace['reason'], 'lint:choppy')
        self.assertEqual(trace['attempts'], 2)
        self.assertTrue(trace['lint']['ok'])
        cached, cached_trace = generate(self.job, self.cfg)
        self.assertEqual(cached, text)
        self.assertTrue(cached_trace['cache_hit'])
        self.assertEqual(cached_trace['reason'], 'lint:choppy')
        self.assertEqual(self.request.call_count, 2)

    def test_provider_failure_is_not_retried_or_leaked(self):
        self.request.side_effect=RuntimeError('secret private response body')
        text,trace=generate(self.job,self.cfg)
        self.assertTrue(lint_greeting(text)['ok']);self.assertEqual(trace['reason'],'RuntimeError')
        self.assertNotIn('secret',json.dumps(trace));self.request.assert_called_once()

    def test_daily_budget_counts_lint_retries_and_survives_restart(self):
        self.cfg.greetings.generate.daily_budget_calls=1
        self.request.return_value=(self.response(TEXT+'贵司'),{})
        self.assertEqual(generate(self.job,self.cfg)[1]['reason'],'daily_budget_exhausted')
        self.assertEqual(generate({**self.job,'platform_job_id':'two'},self.cfg)[1]['reason'],'daily_budget_exhausted')
        self.request.assert_called_once()

    def test_disabled_and_non_composer_do_not_create_cache(self):
        self.cfg.greetings.generate.enabled=False
        self.assertEqual(generate(self.job,self.cfg)[0],'旧文案')
        self.cfg.greetings.generate.enabled=True
        self.cfg.engine.platforms['boss'].greeting_mode='platform_default'
        generate(self.job,self.cfg)
        self.request.assert_not_called()
        self.assertFalse((Path(self.temp.name)/'greeting-generation.sqlite3').exists())

    def test_invented_product_with_real_hook_rejected(self):
        text=TEXT.replace('看你们在做企业知识库检索。','JD里提到企业知识库检索，我能开发你们的星云CRM产品。')
        self.request.return_value=(self.response(text),{})
        self.assertEqual(generate(self.job,self.cfg)[1]['source'],'fallback')

    def test_merged_platform_setting_uses_periods_and_no_blank_lines(self):
        self.cfg.engine.platforms['boss'].greeting_paragraph_separator=''
        text,trace=generate(self.job,self.cfg)
        self.assertNotIn('\n',text);self.assertNotIn('；',text)
        self.assertEqual(trace['source'],'generated')

    def test_unconfirmed_file_never_enters_request(self):
        (Path(self.temp.name)/'greeting-facts.yaml').write_text('version: 1\nfacts:\n- id: draft\n  text: PRIVATE-STORY\n  tags: [RAG]\n  confirmed: false\n  source: draft\n')
        generate(self.job,self.cfg)
        self.assertNotIn('PRIVATE-STORY',json.dumps(self.request.call_args.args[0]))

    def test_interrupted_claim_is_not_replayed(self):
        from job_agent.service.greeting_cache import GreetingCache
        with GreetingCache() as cache:
            cache.claim(cache.key(self.job))
        text,trace=generate(self.job,self.cfg)
        self.assertTrue(lint_greeting(text)['ok'])
        self.assertEqual(trace['reason'],'generation_in_progress_or_interrupted')
        self.request.assert_not_called()

    def test_invalid_json_and_unknown_usage_are_recorded(self):
        self.request.return_value=({'choices':[{'message':{'content':'not json'}}],'usage':{'total_tokens':'unknown'}},{})
        text,trace=generate(self.job,self.cfg)
        self.assertTrue(lint_greeting(text)['ok']);self.assertEqual(trace['source'],'fallback')
        with sqlite3.connect(Path(self.temp.name)/'greeting-generation.sqlite3') as db:
            self.assertEqual(db.execute('select count(*) from calls where status="lint_failed" and total_tokens is null').fetchone()[0],2)

    def test_confirmed_anecdote_can_be_selected_and_cached(self):
        story='知识库搜不准时，我先拿旧问题对比。测下来新功能帮助不大还慢，我就关掉。'
        text=TEXT.replace(TEXT.split('\n\n')[1].split('。',1)[1],story)
        (Path(self.temp.name)/'greeting-facts.yaml').write_text('version: 1\nfacts:\n- id: story\n  text: '+story+'\n  tags: [RAG]\n  confirmed: true\n  source: synthetic test\n')
        self.request.return_value=({'choices':[{'message':{'content':json.dumps(dict(text=text,fact=story,experience=story,anecdote_id="story",company_hook=HOOK))}}]}, {})
        actual,trace=generate(self.job,self.cfg)
        self.assertEqual(actual,text);self.assertEqual(trace['anecdote_id'],'story')
        self.assertTrue(generate(self.job,self.cfg)[1]['cache_hit'])

    def test_non_composer_offline_preview_does_not_enable_live_generation(self):
        self.cfg.engine.platforms['boss'].greeting_mode='platform_default'
        self.assertEqual(generate(self.job,self.cfg)[1]['source'],'fixed')
        text,trace=generate(self.job,self.cfg,preview_only=True)
        self.assertEqual(text,TEXT);self.assertEqual(trace['source'],'generated')
        self.assertEqual(generate(self.job,self.cfg)[1]['source'],'fixed')
        self.request.assert_called_once()

    def test_copy_rejected_by_custom_rules_blocks_preview_before_batch(self):
        from job_agent.service.deliver import preview
        from job_agent.store import SQLiteDeliveryStore
        self.cfg.greetings.generate.blacklist=['你好']
        self.cfg.engine.platforms['boss'].enabled=True
        with SQLiteDeliveryStore(Path(self.temp.name)/'jobs.db') as store:
            ident=store.upsert_job(platform='boss',platform_job_id='one',url='https://example.invalid/job',jd=HOOK)
            with self.assertRaisesRegex(ValueError,'Greeting blocked'):
                preview(store,self.cfg,job_ids=[ident])
            self.assertEqual(store.connection.execute('select count(*) from batches').fetchone()[0],0)

    def test_budget_reservations_are_atomic_across_connections(self):
        from job_agent.service.greeting_cache import GreetingCache
        with GreetingCache() as first, GreetingCache() as second:
            self.assertTrue(first.reserve_call('a','run','model',1,'UTC'))
            self.assertIsNone(second.reserve_call('b','run','model',1,'UTC'))

    def test_preview_generation_summary_counts_only_paid_generation_modes(self):
        from job_agent.service.deliver import generation_summary
        self.assertEqual(generation_summary({'_generation':{'a':{'source':'generated'},'b':{'source':'fallback'},'c':{'source':'fixed'}}}), {'generated':1,'fallback':1})

    def test_same_direction_cannot_only_swap_jd_words(self):
        first,trace=generate(self.job,self.cfg,run_id="batch")
        self.assertEqual(trace['source'],'generated')
        self.request.return_value=(self.response(),{})
        second,trace=generate({**self.job,'platform_job_id':'another'},self.cfg,run_id='batch')
        self.assertEqual(trace['source'],'fallback')
        self.assertIn('homogeneous',trace['reason'])
        data=json.loads(self.request.call_args.args[0]['messages'][1]['content'])
        self.assertEqual(data['previous_entries'][0]['hook'],HOOK)
        self.assertEqual(self.request.call_count,3)

    def test_account_default_sync_preview_uses_paragraph_setting_read_only(self):
        from unittest.mock import MagicMock
        from job_agent.service.greeting_sync import sync_default_greeting
        self.cfg.engine.platforms['zhilian'].greeting_mode='platform_default'
        self.cfg.engine.platforms['zhilian'].greeting_max_length=500
        self.cfg.engine.platforms['zhilian'].greeting=TEXT
        self.cfg.engine.platforms['zhilian'].greeting_paragraph_separator=''
        browser=MagicMock()
        with patch('job_agent.service.greeting_sync.load_config',return_value=self.cfg), \
             patch('job_agent.browser.ego.EgoBrowser',return_value=browser), \
             patch('job_agent.platforms.zhilian.default_greeting.synchronize',return_value={'changed':False}) as sync:
            self.assertFalse(sync_default_greeting('zhilian')['changed'])
        args=sync.call_args.args
        self.assertNotIn('\n',args[1]);self.assertNotIn('；',args[1]);self.assertFalse(args[2])
        browser.space.return_value.new_page.return_value.close.assert_called_once()

    def test_daily_summary_reports_fallback_without_draft_text(self):
        from job_agent.service.daily import delivery_message
        title,text=delivery_message({'greeting_generation':{'generated':2,'fallback':1},'today_success':{},'next_resume_at':None})
        self.assertIn('fallback 1',text);self.assertNotIn(TEXT,text)
