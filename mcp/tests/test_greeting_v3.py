"""V3 copy contract and strict anecdote admission."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from job_agent.config import Settings
from job_agent.platforms.greeting import lint_greeting, prepare_greeting, split_hook
from job_agent.service.greeting_generation import validate_result
from job_agent.service.greeting_facts import select_anecdote
from job_agent.service.greeting_templates import select_template

FACT = '主导开发31个知识库的问答服务。处理过旧文档干扰回答的问题。'
HOOK = '企业知识库检索'
PARTS = ('你好，我是候选人，做软件开发的。已离职，随时能到岗。', '看你们在做企业知识库检索。这块我做过，之前给31个知识库做过问答。旧文档把答案带偏的问题也处理过。', '简历发你看看？')
TEXT = '\n\n'.join(PARTS)


class LintV3Tests(unittest.TestCase):
    def codes(self, text, **kwargs):
        return {i['code'] for i in lint_greeting(text, **kwargs)['issues']}

    def test_blacklist_normalization_and_config(self):
        self.assertTrue(lint_greeting(TEXT)['ok'])
        for term in ('看了贵公司', '贵司', '非常符合', '十分契合', '希望有机会', '期待您的回复', '我相信我能',
                     '核心落地经验', '攻克', '赋能', '深耕', '助力', '贵 司'):
            self.assertIn('ai_cliche', self.codes(TEXT+term))
        self.assertIn('ai_cliche', self.codes(TEXT+'特别合适', blacklist=['特别合适']))
        self.assertNotIn('ai_cliche', self.codes(TEXT+'贵司', blacklist=[]))

    def test_three_or_four_paragraphs_and_length_excluding_line_breaks(self):
        self.assertEqual(lint_greeting(TEXT)['length'], len(''.join(PARTS)))
        for text in (''.join(PARTS), '\n'.join(PARTS), TEXT+'\n\n多一段。\n\n又一段。', TEXT.replace('\n\n','\n')):
            self.assertIn('paragraph_format', self.codes(text))
        self.assertNotIn('paragraph_format', self.codes(TEXT.replace('\n', '\r\n')))
        self.assertIn('too_long', self.codes(TEXT*2))
        self.assertIn('too_short', self.codes('短。\n\n短。\n\n短。\n\n短。'))
        for n in (70,79,85,110,120):
            text=('字。'*60)[:n]
            self.assertFalse({'too_long','too_short'} & self.codes(text,require_paragraphs=False))
        self.assertIn('too_short', self.codes(('字。'*35)[:69], require_paragraphs=False))
        self.assertIn('too_long', self.codes(('字。'*61)[:121], require_paragraphs=False))

    def test_long_sentences_split_on_full_stop_not_comma(self):
        self.assertEqual(Settings().greetings.generate.max_sentence_length, 45)
        self.assertIn('long_sentence', self.codes('字'*46+'。'))
        for comma in ('，', ','):
            with self.subTest(comma=comma):
                self.assertIn('long_sentence', self.codes('字'*22+comma+'字'*23+'。'))
                self.assertNotIn('long_sentence', self.codes('字'*22+comma+'字'*22+'。'))
        self.assertNotIn('long_sentence', self.codes('字'*45+'。\n\n'+'字'*45+'。'))
        self.assertIn('long_sentence', self.codes('字'*26+'。', max_sentence_length=25))

    def test_split_hook_at_first_comma_or_full_stop(self):
        opening = '看你们在做企业知识库检索'
        experience = '这块我做过，之前给31个知识库做过问答。旧文档的问题也处理过。'
        for delimiter in ('，', ',', '。'):
            with self.subTest(delimiter=delimiter):
                self.assertEqual(split_hook(opening+delimiter+experience), (opening, experience))
                self.assertEqual(split_hook(opening+delimiter), (opening, ''))
        self.assertEqual(split_hook(opening), (opening, ''))
        self.assertEqual(split_hook(''), ('', ''))

    def test_second_paragraph_allows_three_sentences_but_rejects_four(self):
        for punctuation in ('。', '！', '？', '!', '?'):
            with self.subTest(punctuation=punctuation):
                body = punctuation.join(('看你们在做企业知识库检索，这块我做过',
                                         '之前给31个知识库做过问答', '旧文档的问题也处理过'))+punctuation
                text = '\n\n'.join((PARTS[0], body, PARTS[2]))
                self.assertTrue(lint_greeting(text)['ok'])
                four = text.replace(body, body+'回答会附原文。')
                self.assertEqual(self.codes(four), {'choppy'})
                self.assertNotIn('choppy', self.codes(four, require_paragraphs=False))

    def test_jargon_count_and_config(self):
        self.assertIn('jargon_overload', self.codes(TEXT+'RAG、MCP、LangGraph。', company_context=''))
        self.assertIn('jargon_overload', self.codes(TEXT+'RAG和MCP。'))
        self.assertIn('jargon_overload', self.codes(TEXT+'甲乙丙。', jargon_terms=['甲','乙','丙']))
        self.assertNotIn('jargon_overload', self.codes(TEXT+'多Agent路由。'))

    def test_three_numbers_rejected_ranges_count_two(self):
        for text in ('覆盖31个库、1300篇文档、交付50个需求。', '覆盖３１个库，１３００篇文档，节省１万元。',
                     '覆盖三十个库、一千篇文档、节省两万元。'):
            self.assertIn('numeric_list', self.codes(text))
        self.assertIn('numeric_list', self.codes('达标率从30%到70%。交付从6个到50个。'))

    def test_entire_entry_paragraph_grounded_not_just_one_real_phrase(self):
        kwargs=dict(company_context='负责企业知识库检索开发',company_hook=HOOK,candidate_facts=[FACT])
        self.assertTrue(lint_greeting(TEXT,**kwargs)['ok'])
        for entry in ('你们在做星云CRM。','星云CRM很有意思。','JD里提到企业知识库检索和星云CRM。',
                      'JD里提到企业知识库检索，我用过你们的产品。'):
            self.assertIn('ungrounded_company',self.codes('\n\n'.join((PARTS[0],entry,*PARTS[2:])),**kwargs))
        self.assertIn('ungrounded_company',self.codes(TEXT,company_context='',company_hook=HOOK))
        with self.assertRaisesRegex(ValueError,'ungrounded_company'):
            validate_result(dict(text=TEXT,fact=FACT,experience=PARTS[1].split('。',1)[1],company_hook=HOOK),[FACT],company_context='某公司')
        with self.assertRaisesRegex(ValueError,'number_outside_fact'):
            validate_result(dict(text=TEXT+'十万用户',fact=FACT,experience=PARTS[1].split('。',1)[1],company_hook=HOOK),[FACT],company_context=HOOK)

    def test_config_separator_inheritance_and_legacy_preservation(self):
        c=Settings(engine={'greeting_paragraph_separator':'','platforms':{'boss':{'greeting_paragraph_separator':'\n\n'}}})
        self.assertEqual(c.engine.platforms['zhilian'].greeting_paragraph_separator,'')
        self.assertEqual(c.engine.platforms['boss'].greeting_paragraph_separator,'\n\n')
        flat=prepare_greeting('zhilian',TEXT,paragraph_separator='')
        self.assertNotIn('\n',flat);self.assertNotIn('；',flat)
        self.assertEqual(prepare_greeting('boss','旧\n文案',paragraph_separator=''),'旧\n文案')


class AnecdoteV3Tests(unittest.TestCase):
    def test_only_strictly_confirmed_relevant_story_selected(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ,JOB_AGENT_HOME=temp):
            path=Path(temp)/'greeting-facts.yaml'
            path.write_text('version: 1\nfacts:\n- id: draft\n  text: 不参与生成。\n  tags: [RAG, Agent]\n  confirmed: false\n  source: 简历\n- id: memory\n  text: 记忆故事。\n  tags: [Agent]\n  confirmed: true\n  source: 本人\n- id: rag\n  text: 检索故事。\n  tags: [RAG, Agent]\n  confirmed: true\n  source: 本人\n')
            self.assertEqual(select_anecdote({'jd':'RAG Agent'})['id'],'rag')
            self.assertIsNone(select_anecdote({'jd':'前端'}))
            for value in ('"false"','"true"','1'):
                path.write_text('version: 1\nfacts:\n- id: bad\n  text: 不可用。\n  tags: [RAG]\n  confirmed: '+value+'\n  source: 简历\n')
                with self.assertRaisesRegex(ValueError,'invalid_greeting_facts'):select_anecdote({'jd':'RAG'})

    def test_missing_and_malformed_files(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ,JOB_AGENT_HOME=temp):
            self.assertIsNone(select_anecdote({'jd':'RAG'}))
            for content in ('not a mapping','version: 99\nfacts: []','version: 1\nfacts: ['):
                (Path(temp)/'greeting-facts.yaml').write_text(content)
                with self.assertRaisesRegex(ValueError,'invalid_greeting_facts'):select_anecdote({'jd':'RAG'})

    def test_default_and_configured_templates_and_malformed_file(self):
        import yaml
        from job_agent.service.greeting_templates import DEFAULTS
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ,JOB_AGENT_HOME=temp):
            settings=Settings().greetings.generate
            self.assertEqual(select_template({'jd':'Agent长期记忆'},settings)[0],'agent')
            path=Path(temp)/'greeting-templates.yaml'
            path.write_text(yaml.safe_dump({'version':1,'directions':DEFAULTS},allow_unicode=True))
            self.assertEqual(select_template({'jd':'研发自动化流程'},settings)[0],'automation')
            path.write_text('version: 1\ndirections: []')
            with self.assertRaisesRegex(ValueError,'invalid_greeting_templates'):select_template({},settings)
