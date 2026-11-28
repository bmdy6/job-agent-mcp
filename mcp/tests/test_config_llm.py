"""Issue 115 contracts. All rates are fictional test values; no provider calls."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from job_agent.config import Settings


class ConfigLLMTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, JOB_AGENT_HOME=self.tmp.name)
        self.env.start(); self.addCleanup(self.env.stop)

    def test_legacy_and_named_models(self):
        from job_agent.config.llm import resolve_model
        cfg = Settings(scoring={'model': 'legacy'})
        self.assertEqual(resolve_model(cfg, 'scoring').model, 'legacy')
        cfg = Settings(llm={'models': {'test': {'model': 'chosen', 'base_url': 'https://example.invalid/v1'}}},
                       scoring={'model_ref': 'test', 'model': 'legacy'})
        self.assertEqual(resolve_model(cfg, 'scoring').model, 'chosen')
        with self.assertRaises(ValueError): Settings(scoring={'model_ref':'missing'})

    def test_prices_and_cached_tokens(self):
        from job_agent.service.llm_usage import metrics
        from job_agent.config.llm import ModelSettings
        usage = dict(prompt_tokens=100, completion_tokens=20, prompt_tokens_details={'cached_tokens': 40})
        self.assertIsNone(metrics(usage, ModelSettings()).get('cost'))
        model = ModelSettings(pricing=dict(input_per_million=10, cached_input_per_million=2,
                                          output_per_million=30, currency='TEST'))
        self.assertAlmostEqual(metrics(usage, model)['cost'], .00128)

    def test_profile_and_override(self):
        from job_agent.service.prompt_files import render_prompt
        cfg = Settings(profile={'name':'张三','role':'做测试的','availability':''})
        p = Path(self.tmp.name)/'prompts';p.mkdir()
        (p/'greeting.md').write_text('{name}|{role}|{availability}|{"json":true}')
        self.assertEqual(render_prompt('greeting', cfg), '张三|做测试的||{"json":true}')

    def test_private_data_scan(self):
        import re
        root=Path(__file__).resolve().parents[1]/'job_agent'
        patterns=[r'肖亮',r'xiaoliang',r'(?<!\d)1[3-9]\d{9}(?!\d)',
                  r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}']
        offenders=[]
        for p in root.rglob('*'):
            if p.is_file() and p.suffix in {'.py','.md','.yaml','.yml','.txt','.json','.html','.js'}:
                if any(re.search(pattern,p.read_text(),re.I) for pattern in patterns):offenders.append(str(p.relative_to(root)))
        self.assertEqual(offenders,[])

    def configured(self, **policy):
        return Settings(llm={'models':{'test':{'model':'test-model','base_url':'https://example.invalid/v1',
            'pricing':dict(input_per_million=10,cached_input_per_million=2,output_per_million=30,currency='TEST')}},
            'scoring':{'model_ref':'test'},'greetings':{'model_ref':'test'},
            'reply_classification':{'model_ref':'test'},**policy})

    def test_daily_caps_global_scope_and_once_only_alert(self):
        from job_agent.service import llm_usage as u
        from job_agent.config.llm import resolve_model
        cfg=self.configured(budget={'daily_cost':.001})
        cfg.notify.enabled=True
        from job_agent.config import TelegramSettings
        cfg.notify.channels['test']=TelegramSettings(enabled=True)
        with patch('job_agent.notify.send_one') as alert:
            ident,_=u.reserve(cfg,'greetings','run')
            self.assertTrue(ident)
            # An active cost-controlled call prevents concurrent budget overspend.
            self.assertEqual(u.reserve(cfg,'scoring','run')[1],'budget_call_in_progress')
            u.finish(ident,{'prompt_tokens':100,'completion_tokens':20},resolve_model(cfg,'greetings'))
            for _ in range(2):
                self.assertEqual(u.reserve(cfg,'scoring','run')[1],'daily_cost_exhausted')
            alert.assert_called_once()
            self.assertTrue(alert.call_args.kwargs['queue_only'])
        report=u.summary(cfg)
        self.assertEqual(report['today']['greetings']['calls'],1)
        self.assertAlmostEqual(report['last_7_days']['greetings']['costs']['TEST'],.0016)

    def test_per_purpose_and_unknown_prices_and_count_caps(self):
        from job_agent.service import llm_usage as u
        from job_agent.config.llm import resolve_model
        cfg=self.configured(greetings={'model_ref':'test','daily_cost':0})
        self.assertEqual(u.reserve(cfg,'greetings','run')[1],'daily_cost_exhausted')
        ident,_=u.reserve(cfg,'scoring','run');self.assertTrue(ident)
        u.finish(ident,{},resolve_model(cfg,'scoring'),'provider_error')
        unknown=Settings(llm={'budget':{'daily_cost':0,'daily_calls':2}})
        ident,_=u.reserve(unknown,'greetings','run');self.assertTrue(ident)
        u.finish(ident,{'prompt_tokens':10,'completion_tokens':1},resolve_model(unknown,'greetings'))
        self.assertEqual(u.reserve(unknown,'greetings','run')[1],'daily_budget_exhausted')
        self.assertIn(u.UNKNOWN,u.summary(unknown)['today']['greetings']['cost_text'])

    def test_currency_safety_and_no_retroactive_repricing(self):
        from job_agent.service import llm_usage as u
        from job_agent.config.llm import resolve_model
        cfg=self.configured()
        ident,_=u.reserve(cfg,'scoring','run')
        u.finish(ident,{'prompt_tokens':100,'completion_tokens':20},resolve_model(cfg,'scoring'))
        cfg.llm.models['test'].pricing.input_per_million=999
        self.assertAlmostEqual(u.summary(cfg)['today']['scoring']['costs']['TEST'],.0016)
        cfg.llm.budget.daily_cost=100
        cfg.llm.budget.currency='OTHER'
        self.assertEqual(u.reserve(cfg,'scoring','run')[1],'budget_currency_mismatch')

    def test_window_boundaries_and_readonly_summary(self):
        from datetime import datetime, timedelta
        from job_agent.service import llm_usage as u
        cfg=self.configured()
        self.assertEqual(u.summary(cfg)['today']['scoring']['calls'],0)
        self.assertFalse((Path(self.tmp.name)/'llm-usage.sqlite3').exists())
        ident,_=u.reserve(cfg,'scoring','run')
        day=u.day_for(cfg)
        with u.ledger() as db:db.execute('UPDATE calls SET day=? WHERE id=?',((day-timedelta(days=6)).isoformat(),ident))
        self.assertEqual(u.summary(cfg)['today']['scoring']['calls'],0)
        self.assertEqual(u.summary(cfg)['last_7_days']['scoring']['calls'],1)
        with u.ledger() as db:db.execute('UPDATE calls SET day=?',((day-timedelta(days=7)).isoformat(),))
        self.assertEqual(u.summary(cfg)['last_7_days']['scoring']['calls'],0)

    def test_identity_custom_profile_optional_fields(self):
        from job_agent.platforms.greeting import lint_greeting
        cfg=Settings(profile={'name':'张三','role':'做软件开发的'})
        text='你好，我是张三，做软件开发的。\n\n之前做过的相关工作已经整理在简历里，也写了具体负责的内容和结果。我想先看看岗位要求，再聊聊这些经历能用在哪。\n\n简历发你看看？'
        self.assertTrue(lint_greeting(text,profile=cfg.profile)['ok'])
        self.assertIn('identity',{i['code'] for i in lint_greeting(text.replace('张三','李四'),profile=cfg.profile)['issues']})
        cfg.profile.availability='下月到岗'
        self.assertIn('identity',{i['code'] for i in lint_greeting(text,profile=cfg.profile)['issues']})

    def test_prompt_explicit_path_and_single_pass_substitution(self):
        from job_agent.service.prompt_files import render_prompt
        file=Path(self.tmp.name)/'custom.md';file.write_text('{name}|{role}')
        cfg=Settings(profile={'name':'{role}','role':'测试'},prompts={'greeting':str(file)})
        self.assertEqual(render_prompt('greeting',cfg),'{role}|测试')
        cfg.prompts.greeting=str(file)+'missing'
        with self.assertRaises(FileNotFoundError):render_prompt('greeting',cfg)

    def test_settings_set_profile_models_and_prompt_path(self):
        from job_agent.service.settings import update_settings
        from job_agent.config import load_config
        result=update_settings({'profile.name':'张三','profile.role':'做测试的',
            'prompts.greeting':'$JOB_AGENT_HOME/prompts/greeting.md','deploy.label_prefix':'com.example',
            'llm.models.test.model':'fixture','scoring.model_ref':'test'},confirm=True,reason='offline fixture')
        self.assertTrue(result['applied'])
        cfg=load_config();self.assertEqual(cfg.profile.name,'张三')
        self.assertEqual(cfg.scoring.model_ref,'test')

    def test_greeting_budget_fallback_no_provider_and_original_ledger_cost(self):
        import json,sqlite3
        from job_agent.service.greeting_generation import generate
        cfg=self.configured(greetings={'model_ref':'test','daily_cost':0})
        cfg.greetings.generate.enabled=True;cfg.greetings.generate.facts=['做过软件开发。']
        job=dict(platform='boss',platform_job_id='test',title='开发',jd='软件开发')
        cfg.engine.platforms['boss'].greeting_max_length=1000
        with patch('job_agent.service.scoring.resolve_key',return_value='fixture'),patch('job_agent.service.scoring.request_completion') as call:
            text,trace=generate(job,cfg,run_id='fixture-run')
            self.assertEqual(trace['reason'],'daily_cost_exhausted');self.assertTrue(text)
            call.assert_not_called()
        cfg.llm.greetings.daily_cost=None
        payload={'choices':[{'message':{'content':'invalid'}}],'usage':{'prompt_tokens':100,'completion_tokens':20}}
        cfg.greetings.generate.max_retries=0
        with patch('job_agent.service.scoring.resolve_key',return_value='fixture'),patch('job_agent.service.scoring.request_completion',return_value=(payload,{})) as call:
            generate({**job,'platform_job_id':'second'},cfg,run_id='fixture-run')
            self.assertEqual(call.call_args.args[0]['model'],'test-model')
            self.assertEqual(call.call_args.args[1].base_url,'https://example.invalid/v1')
        with sqlite3.connect(Path(self.tmp.name)/'greeting-generation.sqlite3') as db:
            usage=json.loads(db.execute('SELECT usage_json FROM calls').fetchone()[0])
            self.assertAlmostEqual(usage['cost'],.0016);self.assertEqual(usage['run_id'],'fixture-run')

    def test_reply_classification_accounts_invalid_response_and_budget(self):
        from job_agent.service.reply_classification import classify
        from job_agent.service import llm_usage as u
        cfg=self.configured()
        with patch('job_agent.service.reply_classification.resolve_key',return_value='fixture'),patch('job_agent.service.reply_classification.request_completion',return_value=({'usage':{'prompt_tokens':100,'completion_tokens':20}},{})) as call:
            out=classify('了解一下背景',cfg.scoring,use_model=True,config=cfg,run_id='fixture')
            self.assertEqual(out['error'],'invalid_model_result');call.assert_called_once()
        self.assertAlmostEqual(u.summary(cfg)['today']['reply_classification']['costs']['TEST'],.0016)
        cfg.llm.reply_classification.daily_cost=0
        with patch('job_agent.service.reply_classification.request_completion') as call:
            self.assertEqual(classify('了解一下背景',cfg.scoring,use_model=True,config=cfg)['error'],'daily_cost_exhausted')
            call.assert_not_called()

    def test_reports_expose_usage_without_writing(self):
        from job_agent.store import Store
        from job_agent.service import read_tools,dashboard,daily,llm_usage
        from job_agent.dashboard_html import render
        with Store():pass
        report=read_tools.funnel_stats()
        self.assertIn('llm_usage',report)
        with read_tools.snapshot() as store:data=dashboard.collect(store,Settings(),Path(self.tmp.name))
        self.assertIn(llm_usage.UNKNOWN,render(data))
        title,text=daily.delivery_message({'today_success':{},'next_resume_at':None})
        self.assertIn('当天模型',text);self.assertIn('最近7天模型',text);self.assertIn(llm_usage.UNKNOWN,text)

    def test_old_tokens_are_visible_without_repricing_and_new_calls_deduplicate(self):
        from datetime import datetime
        from job_agent.service import llm_usage as u
        from job_agent.config.llm import resolve_model
        from job_agent.service.greeting_cache import GreetingCache
        cfg=self.configured()
        with GreetingCache() as cache:
            ident=cache.reserve_call('legacy','old-run','old-model',100,'Asia/Shanghai')
            cache.finish_call(ident,'generated',0,{'prompt_tokens':10,'completion_tokens':5,'total_tokens':15})
        row=u.summary(cfg)['today']['greetings']
        self.assertEqual(row['calls'],1);self.assertEqual(row['total_tokens'],15)
        self.assertEqual(row['unknown_cost_calls'],1);self.assertEqual(row['costs'],{})
        with GreetingCache() as cache:
            original=cache.reserve_call('new','new-run','test-model',100,'Asia/Shanghai')
            ident,_=u.reserve(cfg,'greetings','new-run')
            value=u.finish(ident,None,resolve_model(cfg,'greetings'),'provider_error')
            cache.finish_call(original,'provider_error',0,value)
        row=u.summary(cfg)['today']['greetings']
        self.assertEqual(row['calls'],2)
        self.assertEqual(row['unknown_cost_calls'],2)

    def test_missing_usage_cannot_bypass_configured_money_budget(self):
        from job_agent.service import llm_usage as u
        from job_agent.config.llm import resolve_model
        cfg=self.configured(budget={'daily_cost':100})
        ident,_=u.reserve(cfg,'scoring','run')
        u.finish(ident,None,resolve_model(cfg,'scoring'),'provider_unknown')
        self.assertEqual(u.reserve(cfg,'greetings','run')[1],'budget_cost_unknown')

    def test_settings_prompt_override_preserves_path_boundary(self):
        from job_agent.service.settings import update_settings
        for path in ('$JOB_AGENT_HOME/config.yaml','/tmp/credential.md'):
            with self.assertRaises(ValueError):update_settings({'prompts.greeting':path},confirm=True,reason='fixture')
