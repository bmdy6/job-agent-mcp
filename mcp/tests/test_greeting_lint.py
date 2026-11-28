import unittest
from job_agent.platforms.greeting import lint_greeting


class GreetingLintTest(unittest.TestCase):
    def test_human_greeting(self):
        text='你好，我是候选人，做软件开发的。已离职，随时能到岗。\n\n看你们在做企业知识库检索。这块我做过，之前给31个知识库做过问答。旧文档把答案带偏的问题也处理过。\n\n简历发你看看？'
        self.assertTrue(lint_greeting(text)['ok'])

    def test_empty_short_long_and_format(self):
        self.assertEqual(lint_greeting('   ')['issues'][0]['code'],'empty')
        self.assertIn('too_short',{i['code'] for i in lint_greeting('你好。')['issues']})
        codes={i['code'] for i in lint_greeting('贵司赋能。'*40)['issues']}
        self.assertTrue({'too_long','paragraph_format','ai_cliche'} <= codes)
