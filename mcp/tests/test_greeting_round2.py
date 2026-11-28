"""Round 2 regression: conversational rewriting, provenance and whole-batch diversity."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from job_agent.config import Settings
from job_agent.platforms.greeting import lint_greeting, prepare_greeting
from job_agent.service.greeting_generation import validate_result, generate
from job_agent.service.greeting_cache import GreetingCache
from job_agent.service.greeting_facts import select_anecdote

FACT = '主导开发31个知识库的问答服务。也处理过旧文档影响回答的问题。回答会附原文。'
EXPERIENCE = '这块我做过，之前给31个知识库做过问答。旧文档把答案带偏的问题也处理过，还会让回答附上原文。'
TEXT = '你好，我是候选人，做软件开发的。已离职，随时能到岗。\n\n看你们在做知识问答。'+EXPERIENCE+'\n\n简历发你看看？'
RESULT = dict(text=TEXT, fact=FACT, experience=EXPERIENCE, company_hook='知识问答')

class Round2Tests(unittest.TestCase):
    def codes(self, text, **kwargs):
        return {i['code'] for i in lint_greeting(text, **kwargs)['issues']}

    def test_three_paragraphs_and_rewritten_experience(self):
        self.assertEqual(validate_result(RESULT, [FACT], company_context='开发知识问答'), TEXT)
        self.assertTrue(lint_greeting(TEXT)['ok'])
        self.assertEqual(prepare_greeting('zhilian',TEXT,paragraph_separator=''),TEXT.replace('\n\n',''))

    def test_comma_ended_opening_clause_is_grounded_and_preserves_experience(self):
        for comma in ('，', ','):
            with self.subTest(comma=comma):
                text = TEXT.replace('看你们在做知识问答。', '看你们在做知识问答'+comma)
                result = {**RESULT, 'text': text}
                self.assertEqual(validate_result(result, [FACT], company_context='开发知识问答'), text)
                self.assertTrue(lint_greeting(text, company_context='开发知识问答', company_hook='知识问答')['ok'])
                with self.assertRaisesRegex(ValueError, '^fact_not_grounded$'):
                    validate_result({**result, 'experience': EXPERIENCE.split('。', 1)[1]},
                                    [FACT], company_context='开发知识问答')
                with self.assertRaisesRegex(ValueError, 'ungrounded_company'):
                    validate_result({**result, 'text': text.replace('知识问答'+comma, '知识问答和星云CRM'+comma)},
                                    [FACT], company_context='开发知识问答')

    def test_availability_empty_questions_resume_voice_and_long_ending_rejected(self):
        self.assertIn('identity',self.codes(TEXT.replace('已离职，随时能到岗。',''),profile=Settings(profile={'availability':'已离职，随时能到岗'}).profile))
        for phrase in ('想了解目前的难点','想听听需求','想聊聊方案','您','体现了','具备','能力','主导开发','问答服务','系统设计'):
            self.assertIn('ai_cliche',self.codes(TEXT.replace('这块我做过',phrase)))
        self.assertIn('experience_missing',self.codes(TEXT.replace(EXPERIENCE,'这块挺有意思。')))
        self.assertIn('closing',self.codes(TEXT.replace('简历发你看看？','有机会的话希望能和你详细讨论这个岗位的要求。')))

    def test_two_numbers_even_a_before_after_range_are_two(self):
        self.assertNotIn('numeric_list',self.codes(TEXT.replace(EXPERIENCE,'我让AI帮团队写代码、测代码。一周交付从6个涨到50个。')))
        self.assertIn('numeric_list',self.codes(TEXT.replace(EXPERIENCE,'我做过31个库的问答。一周交付从6个涨到50个。')))

    def test_single_technical_term_and_grounded_employer_claim(self):
        self.assertIn('jargon_overload',self.codes(TEXT.replace('知识问答','RAG和MCP'),company_context='RAG MCP'))
        bad={**RESULT,'text':TEXT.replace('知识问答。','知识问答和星云CRM。')}
        with self.assertRaisesRegex(ValueError,'ungrounded_company'):
            validate_result(bad,[FACT],company_context='知识问答')
        with self.assertRaisesRegex(ValueError,'number_outside_fact'):
            validate_result({**RESULT,'text':TEXT.replace('31','99'),'experience':EXPERIENCE.replace('31','99')},[FACT],company_context='知识问答')

    def test_similar_second_paragraph_across_directions(self):
        previous=[{'text':TEXT.split('\n\n')[1],'hook':'知识问答','closing':'简历发你看看？'}]
        self.assertIn('homogeneous',self.codes(TEXT,previous_entries=previous))

    def test_fourth_same_sentence_frame_rejected_but_first_three_allowed(self):
        previous=[{'text':'看你们在做'+h+'。'+('经历各不相同。'*i),'hook':h} for i,h in enumerate(('图片识别','记忆','代码生成'),1)]
        self.assertNotIn('sentence_pattern',self.codes(TEXT,previous_entries=previous[:2],company_hook='知识问答',similarity_threshold=1.0))
        self.assertIn('sentence_pattern',self.codes(TEXT,previous_entries=previous,company_hook='知识问答',similarity_threshold=1.0))

    def test_similarity_is_independent_of_pair_order(self):
        from job_agent.platforms.greeting import entry_similarity
        self.assertEqual(entry_similarity('tide','diet'),entry_similarity('diet','tide'))

    def test_short_experience_bridge_also_has_batch_limit(self):
        previous=[{'text':start+h+'。这块我做过。各不相同的经历。','hook':h}
                  for start,h in [('你们要做','识别'),('岗位提到','记忆'),('这份工作要做','写代码')]]
        self.assertIn('sentence_pattern',self.codes(TEXT,company_hook='知识问答',previous_entries=previous))

    def test_interest_without_concrete_experience_rejected(self):
        self.assertIn('experience_missing',self.codes(TEXT.replace(EXPERIENCE,'这块我很感兴趣。')))

    def test_recent_entries_covers_entire_run_and_all_directions(self):
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ,JOB_AGENT_HOME=home), GreetingCache() as cache:
            for i in range(45):
                key=str(i); cache.claim(key)
                cache.save(key,TEXT,dict(source='generated',direction='rag' if i%2 else 'agent',job_key=key,run_id='batch',company_hook='知识问答'))
            cache.claim('other'); cache.save('other',TEXT,dict(source='generated',run_id='other'))
            self.assertEqual(len(cache.recent_entries('rag','new',run_id='batch')),45)

    def test_config_closings_and_thresholds_validated(self):
        cfg=Settings().greetings.generate
        self.assertGreaterEqual(len(cfg.closing_lines),4)
        self.assertEqual(cfg.max_sentence_pattern,3)
        for changes in ({'closing_lines':[]},{'similarity_threshold':1.1},{'max_sentence_pattern':0}):
            with self.assertRaises(ValueError):Settings(greetings={'generate':changes})

    def test_confirmed_when_field_and_relevance_keywords(self):
        import yaml
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ,JOB_AGENT_HOME=home):
            Path(home,'greeting-facts.yaml').write_text(yaml.safe_dump(dict(version=1,facts=[dict(id='vision',text='自采图片训练识别。',when='JD提到视觉、多模态、识别',tags=['图像','多模态','主动性'],confirmed=True,source='本人')] ),allow_unicode=True))
            self.assertEqual(select_anecdote({'jd':'计算机视觉识别'})['id'],'vision')
            self.assertIsNone(select_anecdote({'jd':'后端开发','company_intro':'主动性'}))

    def test_supplemental_story_and_unknown_story_are_checked(self):
        fact='做过31个知识库的问答。'
        experience='这块我做过。之前给31个知识库做过问答。'
        story=dict(id='evaluation',text='给知识库加过提准确率的功能。测下来没用还慢，我就关掉。')
        text='你好，我是候选人，做软件开发的。已离职，随时能到岗。\n\n看你们在做知识问答。'+experience+'\n\n'+story['text']+'\n\n简历发你看看？'
        result=dict(text=text,fact=fact,experience=experience,company_hook='知识问答',anecdote_id='evaluation',anecdote_text=story['text'])
        self.assertEqual(validate_result(result,[fact,story['text']],company_context='知识问答',anecdote=story),text)
        for changes in ({'anecdote_id':'unknown'},{'anecdote_text':''}):
            with self.assertRaises(ValueError):validate_result({**result,**changes},[fact],company_context='知识问答',anecdote=story)

    def test_conflicting_cached_drafts_regenerate_in_new_batch(self):
        cfg=Settings(greetings={'generate':{'enabled':True,'facts':[FACT]}},engine={'platforms':{'boss':{'greeting_max_length':1000}}})
        job=dict(platform='boss',platform_job_id='a',jd='知识问答')
        second={**job,'platform_job_id':'b'}
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ,JOB_AGENT_HOME=home), patch('job_agent.service.scoring.resolve_key',return_value='test'), patch('job_agent.service.scoring.request_completion') as call:
            call.return_value=({'choices':[{'message':{'content':json.dumps(RESULT)}}]}, {})
            generate(job,cfg,run_id='old-a');generate(second,cfg,run_id='old-b')
            generate(job,cfg,run_id='new')
            revised='之前给知识库做过问答，旧文档会把答案带偏，这事我专门处理过。也会让回答附原文，方便核对。'
            text=TEXT.replace('看你们在做知识问答。'+EXPERIENCE,'岗位提到知识问答。'+revised).replace('简历发你看看？',cfg.greetings.generate.closing_lines[1])
            call.return_value=({'choices':[{'message':{'content':json.dumps({**RESULT,'text':text,'experience':revised})}}]}, {})
            actual,trace=generate(second,cfg,run_id='new')
            self.assertEqual(actual,text)
            self.assertTrue(trace['batch_regenerated'])
            self.assertEqual(trace['source'],'generated')
            self.assertEqual(call.call_count,3)

    def test_cache_hit_participates_in_batch_and_closing_rotation(self):
        response={'choices':[{'message':{'content':json.dumps(RESULT)}}]}
        cfg=Settings(greetings={'generate':{'enabled':True,'facts':[FACT]}},engine={'platforms':{'boss':{'greeting_max_length':1000}}})
        job=dict(platform='boss',platform_job_id='a',jd='知识问答')
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ,JOB_AGENT_HOME=home), patch('job_agent.service.scoring.resolve_key',return_value='test'), patch('job_agent.service.scoring.request_completion',return_value=(response,{})) as call:
            self.assertEqual(generate(job,cfg,run_id='old')[1]['source'],'generated')
            self.assertTrue(generate(job,cfg,run_id='new')[1]['cache_hit'])
            with GreetingCache() as cache:
                self.assertEqual(len(cache.recent_entries('rag','b',run_id='new')),1)
            call.assert_called_once()
