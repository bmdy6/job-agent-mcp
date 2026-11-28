import unittest
from job_agent.platforms.boss import pages


class BossChatDeepLinkTest(unittest.TestCase):
    def data(self, **extra):
        row = {'id': '9', 'job_id': 'J1', 'conversation_id': 'U1', 'company': 'C', 'job_title': 'T',
               'direction': 'incoming', 'body': '你好', 'time': 1764300000000, **extra}
        return {'current_job_id': 'J1', 'current_conversation_id': 'U1', 'rows': [row]}

    def test_event_links_to_its_conversation(self):
        event, = pages.events(self.data(boss_id='abc~'))
        self.assertEqual(event.chat_url, 'https://www.zhipin.com/web/geek/chat?id=abc~&source=0')
        self.assertTrue(pages.is_chat_url(event.chat_url))

    def test_missing_boss_id_falls_back_to_chat_home(self):
        event, = pages.events(self.data())
        self.assertEqual(event.chat_url, pages.CHAT_URL)


if __name__ == '__main__':
    unittest.main()
