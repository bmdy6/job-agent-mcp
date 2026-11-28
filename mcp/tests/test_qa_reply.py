import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from job_agent.config import Settings
from job_agent.store import Store


class QAReplyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        env = patch.dict('os.environ',JOB_AGENT_HOME=self.tmp.name); env.start(); self.addCleanup(env.stop)
        Path(self.tmp.name,'qa.yaml').write_text('期望薪资: 希望月薪20K，可以结合岗位职责沟通。\n离职原因: 想找更符合发展方向的岗位。\n')
        self.store = Store(Path(self.tmp.name)/'jobs.sqlite3'); self.addCleanup(self.store.close)
        self.cfg = Settings(notify={'draft_reply':True})

    def test_match_and_rewrite_with_model_ref_and_accounting(self):
        from job_agent.service.draft_reply import suggest
        self.cfg = Settings(notify={'draft_reply':True},llm={'models':{'draft':{'model':'mock-model'}},'draft_reply':{'model_ref':'draft'}})
        response = {'choices':[{'finish_reason':'stop','message':{'content':json.dumps({'text':'希望月薪20K，也可以结合岗位职责沟通。'})}}],
            'usage':{'prompt_tokens':100,'completion_tokens':20}}
        with patch('job_agent.service.scoring.resolve_key',return_value='mock'),patch('job_agent.service.scoring.request_completion',return_value=(response,{})) as api:
            result = suggest(self.store,{'id':1,'body':'请问你的期望薪资？'},self.cfg)
            again = suggest(self.store,{'id':1,'body':'请问你的期望薪资？'},self.cfg)
        self.assertEqual(result['text'],'希望月薪20K，也可以结合岗位职责沟通。')
        self.assertEqual(result,again); api.assert_called_once()
        self.assertEqual(api.call_args.args[0]['model'],'mock-model')
        from job_agent.service.llm_usage import ledger
        with ledger() as db: self.assertEqual(db.execute('SELECT purpose FROM calls').fetchone()[0],'draft_reply')

    def test_disabled_unmatched_missing_and_failure(self):
        from job_agent.service.draft_reply import suggest
        with patch('job_agent.service.scoring.request_completion') as api:
            self.assertIsNone(suggest(self.store,{'id':1,'body':'你好'},self.cfg))
            self.cfg.notify.draft_reply = False
            self.assertIsNone(suggest(self.store,{'id':2,'body':'薪资多少'},self.cfg))
            api.assert_not_called()
        self.cfg.notify.draft_reply = True
        with patch('job_agent.service.scoring.resolve_key',side_effect=RuntimeError):
            out = suggest(self.store,{'id':3,'body':'期望薪资多少'},self.cfg)
        self.assertEqual(out['text'],'希望月薪20K，可以结合岗位职责沟通。')
        Path(self.tmp.name,'qa.yaml').unlink()
        self.assertIsNone(suggest(self.store,{'id':4,'body':'期望薪资多少'},self.cfg))

    def test_notification_contains_draft_and_chat_link_only_user_transport(self):
        from job_agent.monitor.notifications import poll_replies
        from datetime import datetime, timezone
        self.cfg = Settings(notify={'enabled':True,'draft_reply':True,'channels':{'tg':{'type':'telegram','enabled':True,'chat_id':'mock'}}})
        jid = self.store.upsert_job(platform='boss',platform_job_id='one',url='https://example.invalid/job',company='示例公司',title='工程师')
        self.store.record_delivery(jid,status='sent')
        self.store.add_reply(jid,message_id='reply',direction='inbound',body='期望薪资多少？',occurred_at=datetime.now(timezone.utc).isoformat(),chat_url='https://example.invalid/chat')
        with patch('job_agent.service.scoring.resolve_key',side_effect=RuntimeError),patch('job_agent.notify.send_one',return_value={'success':True}) as user_send,patch('job_agent.platforms.boss.apply.BossAdapter.send_greeting') as hr_send:
            result = poll_replies(self.store,self.cfg)
        self.assertEqual(result['sent'],1)
        self.assertIn('建议回复（未发送）',user_send.call_args.args[2])
        self.assertIn('20K',user_send.call_args.args[2])
        self.assertEqual(user_send.call_args.args[3],'https://example.invalid/chat')
        hr_send.assert_not_called()

    def test_budget_exhaustion_and_ungrounded_model_output_fall_back(self):
        from job_agent.service.draft_reply import suggest
        cfg=Settings(notify={'draft_reply':True},llm={'draft_reply':{'daily_calls':0}})
        with patch('job_agent.service.scoring.resolve_key',return_value='mock'),patch('job_agent.service.scoring.request_completion') as api:
            out=suggest(self.store,{'id':5,'body':'期望薪资多少？'},cfg)
        api.assert_not_called();self.assertEqual(out['source'],'standard')
        response={'choices':[{'message':{'content':json.dumps({'text':'期望月薪99K。'})}}]}
        with patch('job_agent.service.scoring.resolve_key',return_value='mock'),patch('job_agent.service.scoring.request_completion',return_value=(response,{})):
            out=suggest(self.store,{'id':6,'body':'期望薪资多少？'},self.cfg)
        self.assertNotIn('99K',out['text']);self.assertEqual(out['source'],'standard')

    def test_custom_path_multicategory_and_invalid_yaml(self):
        from job_agent.service.draft_reply import suggest
        path=Path(self.tmp.name,'custom.yaml')
        self.cfg.qa.path=str(path)
        path.write_text('是否接受外包: 不接受外包。\n能否加班: 偶尔可以，长期不接受。\n')
        with patch('job_agent.service.scoring.resolve_key',side_effect=RuntimeError):
            out=suggest(self.store,{'id':7,'body':'接受外包吗，能加班吗？'},self.cfg)
        self.assertEqual(out['categories'],['是否接受外包','能否加班'])
        for value in ('期望薪资: 甲\n期望薪资: 乙\n','[1,2,3]','期望薪资: &a 原文\n离职原因: *a\n'):
            path.write_text(value)
            self.assertIsNone(suggest(self.store,{'id':8,'body':'期望薪资？'},self.cfg))


class DraftFromExperienceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        env = patch.dict('os.environ',JOB_AGENT_HOME=self.tmp.name); env.start(); self.addCleanup(env.stop)
        Path(self.tmp.name,'qa.yaml').write_text('到岗时间: 随时可以到岗。\n')
        self.store = Store(Path(self.tmp.name)/'jobs.sqlite3'); self.addCleanup(self.store.close)
        self.cfg = Settings(notify={'draft_reply':True,'draft_reply_categories':['question']},
                            greetings={'generate':{'facts':['做过31个知识库的问答服务。']}})

    def reply(self, text):
        return {'choices':[{'finish_reason':'stop','message':{'content':json.dumps({'text':text})}}]}

    def test_unmatched_question_drafts_from_experience(self):
        from job_agent.service.draft_reply import suggest
        with patch('job_agent.service.scoring.resolve_key',return_value='mock'),patch('job_agent.service.scoring.request_completion',return_value=(self.reply('做过，之前做了31个知识库的问答。'),{})) as api:
            out = suggest(self.store,{'id':1,'body':'做过知识库吗？'},self.cfg,job={'company':'甲','title':'乙'},category='question')
        self.assertEqual(out['text'],'做过，之前做了31个知识库的问答。')
        sent = json.loads(api.call_args.args[0]['messages'][1]['content'])
        self.assertIn('做过31个知识库的问答服务。',sent['experience'])
        self.assertEqual(sent['other_answers'],{'到岗时间':'随时可以到岗。'})

    def test_unmatched_needs_configured_category_and_model(self):
        from job_agent.service.draft_reply import suggest
        with patch('job_agent.service.scoring.request_completion') as api:
            self.assertIsNone(suggest(self.store,{'id':2,'body':'在吗'},self.cfg,category='rejection'))
            api.assert_not_called()
        with patch('job_agent.service.scoring.resolve_key',side_effect=RuntimeError):
            self.assertIsNone(suggest(self.store,{'id':3,'body':'做过知识库吗？'},self.cfg,category='question'))
            self.assertIsNone(suggest(self.store,{'id':3,'body':'做过知识库吗？'},self.cfg,category='question'))

    def test_invented_numbers_rejected(self):
        from job_agent.service.draft_reply import suggest
        with patch('job_agent.service.scoring.resolve_key',return_value='mock'),patch('job_agent.service.scoring.request_completion',return_value=(self.reply('做过50个知识库。'),{})):
            self.assertIsNone(suggest(self.store,{'id':4,'body':'做过知识库吗？'},self.cfg,category='question'))

    def test_notification_shows_reply_time_in_engine_timezone(self):
        from job_agent.monitor.notifications import poll_replies
        from datetime import datetime, timezone, timedelta
        cfg = Settings(notify={'enabled':True,'channels':{'tg':{'type':'telegram','enabled':True,'chat_id':'mock'}}})
        jid = self.store.upsert_job(platform='boss',platform_job_id='one',url='https://example.invalid/job',company='示例公司',title='工程师')
        self.store.record_delivery(jid,status='sent')
        at = datetime.now(timezone.utc).replace(second=0,microsecond=0)
        self.store.add_reply(jid,message_id='r',direction='inbound',body='你好',occurred_at=at.isoformat())
        with patch('job_agent.notify.send_one',return_value={'success':True}) as send:
            poll_replies(self.store,cfg)
        local = (at+timedelta(hours=8)).strftime('%m-%d %H:%M')
        self.assertIn('回复时间：'+local,send.call_args.args[2])
