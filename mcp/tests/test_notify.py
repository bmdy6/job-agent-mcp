"""Channel contract and safety tests; HTTP is always mocked."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from job_agent.config import Settings, load_config
import job_agent.notify as notify


def settings(**channels):
    return Settings.model_validate({'notify': {'enabled': True, 'channels': channels}}).notify


class NotifyTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ, {'JOB_AGENT_HOME': self.temp.name,
                         'TG_TEST': 'PRIVATE-TOKEN', 'WC_TEST': 'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=PRIVATE-KEY',
                         'SC_TEST': 'SCTPRIVATE'}, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def test_defaults_disabled_and_reject_literal_secrets(self):
        self.assertFalse(Settings().notify.enabled)
        self.assertFalse(Settings().notify.draft_reply)
        self.assertEqual(Settings().notify.interval_minutes, 5)
        self.assertEqual(Settings().notify.channels, {})
        with self.assertRaises(ValueError):
            settings(tg={'type': 'telegram', 'bot_token': 'secret'})
        with self.assertRaises(ValueError):
            settings(wc={'type': 'webhook', 'format': 'wecom', 'url': 'secret'})

    def test_telegram_payload_only_sendmessage_and_no_parse_mode(self):
        cfg = settings(tg={'type': 'telegram', 'enabled': True, 'bot_token_env': 'TG_TEST', 'chat_id': '123'})
        with patch.object(notify, 'post', return_value={'ok': True, 'result': {'message_id': 1}}) as post:
            result = notify.send_channels(cfg, '公司 <test>', '职位 & 摘要', 'https://example.invalid/job')
        self.assertTrue(result['tg']['success'])
        url, body = post.call_args.args[:2]
        self.assertEqual(url, 'https://api.telegram.org/botPRIVATE-TOKEN/sendMessage')
        self.assertEqual(body['chat_id'], '123')
        self.assertIn('公司 <test>', body['text'])
        self.assertIn('https://example.invalid/job', body['text'])
        self.assertNotIn('parse_mode', body)

    def test_webhook_wecom_serverchan_and_generic_payloads(self):
        cfg = settings(wc={'type': 'webhook', 'enabled': True, 'format': 'wecom', 'url_env': 'WC_TEST'},
                       sc={'type': 'webhook', 'enabled': True, 'format': 'serverchan', 'sendkey_env': 'SC_TEST'},
                       hook={'type': 'webhook', 'enabled': True, 'url_env': 'WC_TEST'})
        with patch.object(notify, 'post', side_effect=[{'errcode': 0}, {'code': 0}, {}]) as post:
            result = notify.send_channels(cfg, '标题', '正文', 'https://example.invalid')
        self.assertTrue(all(r['success'] for r in result.values()))
        calls = post.call_args_list
        self.assertEqual(calls[0].args[1]['msgtype'], 'markdown')
        self.assertIn('正文', calls[0].args[1]['markdown']['content'])
        self.assertEqual(calls[1].args[0], 'https://sctapi.ftqq.com/SCTPRIVATE.send')
        self.assertEqual(calls[1].args[1]['title'], '标题')
        self.assertIn('https://example.invalid', calls[1].args[1]['desp'])
        self.assertEqual(calls[2].args[1], {'title': '标题', 'text': '正文', 'url': 'https://example.invalid'})

    def test_permanent_failure_and_other_channel_continues_no_secrets_in_logs(self):
        cfg = settings(tg={'type': 'telegram', 'enabled': True, 'bot_token_env': 'TG_TEST', 'chat_id': '123'},
                       wc={'type': 'webhook', 'enabled': True, 'format': 'wecom', 'url_env': 'WC_TEST'})
        with patch.object(notify, 'post', side_effect=[{'ok': False, 'error_code': 403, 'description': 'PRIVATE-TOKEN'}, {'errcode': 0}]) as post, \
                patch.object(notify.time, 'sleep'), self.assertLogs('job_agent.notify', level='WARNING') as logs:
            result = notify.send_channels(cfg, 'title', 'text')
        self.assertEqual(post.call_count, 2)
        self.assertEqual(result['tg'], {'success': False, 'error_code': '403'})
        self.assertTrue(result['wc']['success'])
        self.assertNotIn('PRIVATE', str(logs.output))

    def test_api_level_errors_and_missing_secret(self):
        for fmt, response, code in [('wecom', {'errcode': 93000, 'errmsg': 'PRIVATE'}, '93000'),
                                     ('serverchan', {'code': 400, 'message': 'PRIVATE'}, '400')]:
            cfg = settings(hook={'type': 'webhook', 'enabled': True, 'format': fmt,
                                  'url_env': 'WC_TEST', 'sendkey_env': 'SC_TEST'})
            with patch.object(notify, 'post', return_value=response):
                result = notify.send_channels(cfg, 'title', 'text', retries=0)
            self.assertEqual(result['hook']['error_code'], code)
        cfg = settings(tg={'type': 'telegram', 'enabled': True, 'bot_token_env': 'MISSING', 'chat_id': '123'})
        with patch.object(notify, 'post') as post:
            result = notify.send_channels(cfg, 'title', 'text', retries=0)
        post.assert_not_called()
        self.assertEqual(result['tg']['error_code'], 'missing_secret')

    def test_disabled_never_calls_http_and_explicit_test_bypasses_disabled_once(self):
        Path(self.temp.name, 'config.yaml').write_text('notify:\n  enabled: false\n  channels:\n    telegram:\n      type: telegram\n      bot_token_env: TG_TEST\n      chat_id: "123"\n')
        with patch.object(notify, 'post', return_value={'ok': False, 'error_code': 403}) as post:
            self.assertEqual(notify.send_channels(load_config().notify, 'title', 'text'), {})
            result = notify.notify_test('telegram')
        self.assertEqual(post.call_count, 1)
        self.assertIn('job-agent 通知测试', post.call_args.args[1]['text'])
        self.assertEqual(result, {'success': False, 'error_code': '403'})

    def test_message_lengths_preserve_links(self):
        cfg = settings(tg={'type': 'telegram', 'enabled': True, 'bot_token_env': 'TG_TEST', 'chat_id': '123'},
                       wc={'type': 'webhook', 'enabled': True, 'format': 'wecom', 'url_env': 'WC_TEST'})
        with patch.object(notify, 'post', side_effect=[{'ok': True}, {'errcode': 0}]) as post:
            notify.send_channels(cfg, '长标题', '中'*10000, 'https://example.invalid/chat')
        tg, wc = [c.args[1] for c in post.call_args_list]
        self.assertLessEqual(len(tg['text'].encode('utf-16-le'))//2, 4096)
        self.assertLessEqual(len(wc['markdown']['content'].encode()), 4096)
        self.assertIn('https://example.invalid/chat', tg['text'])
        self.assertIn('https://example.invalid/chat', wc['markdown']['content'])

    def test_transport_rejects_non_https_and_sanitizes_exceptions(self):
        with self.assertRaises(notify.SendError) as ctx:
            notify.post('http://remote.invalid/PRIVATE', {})
        self.assertNotIn('PRIVATE', str(ctx.exception))
        with patch.object(notify, 'HTTPSConnection', side_effect=OSError('PRIVATE-TOKEN')):
            with self.assertRaises(notify.SendError) as ctx:
                notify.post('https://example.invalid/PRIVATE', {})
        self.assertNotIn('PRIVATE', str(ctx.exception))

    def test_transport_rejects_redirects_malformed_and_api_codes_without_body_leak(self):
        from unittest.mock import MagicMock
        from http.client import HTTPException
        for status, raw, code in [(302, b'{"secret":"PRIVATE"}', '302'),
                                   (200, b'not json PRIVATE', 'invalid_response'),
                                   (200, b'[]', 'invalid_response'),
                                   (200, b'x'*(1024*1024+1), 'invalid_response')]:
            connection = MagicMock()
            response = connection.getresponse.return_value
            response.status, response.read1.side_effect = status, [raw, b'']
            with self.subTest(code=code), patch.object(notify, 'HTTPSConnection', return_value=connection), \
                    self.assertRaises(notify.SendError) as ctx:
                notify.post('https://example.invalid/PRIVATE', {})
            self.assertEqual(ctx.exception.code, code)
            self.assertNotIn('PRIVATE', str(ctx.exception))
            self.assertEqual(connection.request.call_count, 1)
            connection.close.assert_called_once()
        connection.getresponse.side_effect = HTTPException('PRIVATE')
        with patch.object(notify, 'HTTPSConnection', return_value=connection), self.assertRaises(notify.SendError) as ctx:
            notify.post('https://example.invalid', {})
        self.assertEqual(ctx.exception.code, 'transport')
