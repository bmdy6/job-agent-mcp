import unittest
from job_agent.engine.models import Job, Record, Status, Reason
from job_agent.service.daily import delivery_message
from job_agent.service.deliver import summary, REASON_LABELS


def stats(**kw):
    return {**dict(sent=0,unverified=0,failed=0,skipped=0,pending=0,reasons={}),**kw}


class TelegramSummaryTests(unittest.TestCase):
    def test_every_reason_has_a_chinese_label(self):
        import re
        for reason in Reason:
            if reason.value:
                self.assertRegex(REASON_LABELS[reason.value],r'[\u4e00-\u9fff]')

    def test_snapshot_separates_skipped_pending_unknown_and_collapses_empty(self):
        report=dict(started_at='2026-11-27T12:00:00+08:00',status='deferred',
            next_resume_at='2026-11-28T09:00:00+08:00',today_success={'boss':25,'zhilian':7},
            delivery={'boss':stats(sent=15,skipped=15,pending=52,reasons={'job_unavailable':15,'hourly_limit':32,'outside_hours':20}),
                      'zhilian':stats(unverified=1,pending=48,reasons={'daily_limit':48}), 'liepin':stats(), 'maimai':stats()},
            delivery_details=['【BOSS】','✅ 公司｜岗位｜10K｜上海 — 成功','⏭ 不应列出 — 跳过','⏸ 不应列出 — 待续',
                              '⚠️ 某公司｜开发｜15K｜苏州 — 不明：回执未知','❌ 某公司｜开发｜15K｜苏州 — 失败：未登录'])
        title,text=delivery_message(report)
        self.assertIn('推迟到 11-28 09:00',title)
        self.assertIn('今日累计成功：BOSS 25、智联 7',title)
        self.assertIn('BOSS：成功 15、不明 0、跳过 15（岗位已关闭 15）、待续 52（本小时额度已用完 32、不在投递时段 20）',text)
        self.assertIn('本轮无动作：猎聘、脉脉',text)
        self.assertNotIn('不应列出',text)
        self.assertNotIn('hourly_limit',title+text)
        self.assertNotIn('未执行',text)
        self.assertNotIn('投递完成',title)
        self.assertIn('❌',text)

    def test_manual_summary_filters_skips_and_utf16_truncates_whole_details(self):
        records=[Record(Job('boss',str(i),'https://example.invalid/'+str(i)),Status.SENT) for i in range(180)]
        records.extend([Record(Job('boss','pending','https://example.invalid/pending'),Status.PENDING,Reason.HOURLY_LIMIT),
                        Record(Job('boss','skip','https://example.invalid/skip'),Status.SKIPPED,Reason.JOB_UNAVAILABLE)])
        details={('boss',str(i)):dict(company='公司😀'*20,title='岗位',salary='10K',city='上海') for i in range(180)}
        text=summary(records,dry_run=False,details=details)
        self.assertLessEqual(len(('job-agent 投递汇总\n\n'+text).encode('utf-16-le'))//2,4096)
        self.assertNotIn('⏭',text);self.assertNotIn('⏸',text)
        import re
        omitted=int(re.search(r'其余 (\d+) 条见 dashboard',text)[1])
        self.assertEqual(text.count('✅')+omitted,180)

    def test_failure_status_and_unknown_codes_do_not_leak(self):
        title,text=delivery_message(dict(status='failed',delivery={'boss':stats(failed=1,reasons={'secret_TOKEN':1})}))
        self.assertIn('出错',title)
        self.assertNotIn('secret_TOKEN',text)

    def test_today_success_counts_distinct_confirmed_jobs_from_earlier_rounds(self):
        import tempfile
        from pathlib import Path
        from datetime import datetime
        from job_agent.store import SQLiteDeliveryStore
        from job_agent.service.delivery_report import today_success_counts
        with tempfile.TemporaryDirectory() as tmp,SQLiteDeliveryStore(Path(tmp)/'db') as store:
            one=Job('boss','one','https://example.invalid/one')
            two=Job('boss','two','https://example.invalid/two')
            store.add(one);store.add(two)
            store.update(one,Status.SENT,Reason.NONE)
            store.update(one,Status.SENT,Reason.NONE)
            store.update(two,Status.UNVERIFIED,Reason.RECEIPT_UNKNOWN)
            self.assertEqual(today_success_counts(store),{'boss':1})
            self.assertEqual(today_success_counts(store,datetime.fromisoformat('2000-01-01T12:00:00+08:00')),{})

    def test_continuation_respects_overnight_windows_and_daily_hourly_limits(self):
        from datetime import datetime
        from job_agent.config import Settings
        from job_agent.service.delivery_report import next_resume
        cfg=Settings(engine={'platforms':{'boss':{'enabled':True,'greeting_max_length':10,'rate':{'allowed_hours':['09:00','21:00'],'hourly_limit':2}}}})
        now=datetime.fromisoformat('2026-11-27T21:00:00+08:00')
        rows={'boss':stats(pending=3,reasons={'outside_hours':3})}
        self.assertIn('2026-11-28T09:00',next_resume(cfg,rows,now))
        now=datetime.fromisoformat('2026-11-27T10:00:00+08:00')
        rows['boss']['reasons']={'hourly_limit':3}
        times=[now.timestamp()-1200,now.timestamp()-600]
        self.assertIn('10:40:00',next_resume(cfg,rows,now,hourly_times={'boss':times}))
        rows['boss']['reasons']={'daily_limit':3}
        self.assertIn('2026-11-28T09:00',next_resume(cfg,rows,now))
        cfg.engine.platforms['boss'].rate.allowed_hours=('21:00','06:00')
        self.assertIn('2026-11-28T00:00',next_resume(cfg,rows,now))

    def test_daily_summary_is_single_message_and_keeps_omitted_count(self):
        from job_agent.config import Settings
        from job_agent.service.deliver import send_summary
        from unittest.mock import patch
        cfg=Settings(notify={'enabled':True,'channels':{'tg':{'type':'telegram','enabled':True,'chat_id':'123','bot_token_env':'TG'}}})
        report=dict(status='completed',today_success={'boss':500},delivery={'boss':stats(sent=500)},
            delivery_details=[f'✅ 公司😀｜岗位 {i}｜薪资｜城市 — 成功' for i in range(500)])
        title,body=delivery_message(report)
        self.assertLessEqual(len((title+'\n\n'+body).encode('utf-16-le'))//2,4096)
        import re
        omitted=int(re.search(r'其余 (\d+) 条见 dashboard',body)[1])
        self.assertEqual(omitted+body.count('✅'),500)
        with patch('job_agent.notify.send_channels') as send:
            send_summary(cfg,title+'\n\n'+body)
        send.assert_called_once_with(cfg.notify,title,body)

    def test_legacy_snapshot_enrichment_is_read_only_and_does_not_mutate_report(self):
        import tempfile,os,json,sqlite3
        from pathlib import Path
        from datetime import datetime
        from unittest.mock import patch
        from job_agent.store import SQLiteDeliveryStore
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'jobs.sqlite3'
            with SQLiteDeliveryStore(path) as store:
                job=Job('boss','one','https://example.invalid/one')
                store.add(job);store.update(job,Status.SENT,Reason.NONE)
            Path(tmp,'config.yaml').write_text('engine:\n  platforms:\n    boss:\n      enabled: true\n      greeting_max_length: 10\n')
            report=dict(started_at=datetime.now().astimezone().isoformat(),status='deferred',
                        delivery={'boss':stats(sent=1,pending=1,reasons={'outside_hours':1})})
            before=json.dumps(report)
            original=sqlite3.connect
            def readonly(database,**kw):
                self.assertIn('mode=ro',database);self.assertTrue(kw['uri'])
                db=original(database,**kw)
                with self.assertRaises(sqlite3.OperationalError):db.execute('CREATE TABLE forbidden(x)')
                return db
            with patch.dict(os.environ,JOB_AGENT_HOME=tmp),patch('job_agent.store.reporting.sqlite3.connect',side_effect=readonly):
                title,text=delivery_message(report)
            self.assertIn('今日累计成功：BOSS 1',title)
            self.assertEqual(json.dumps(report),before)

    def test_zero_platform_holds_stay_in_the_single_collapsed_line(self):
        title,text=delivery_message(dict(today_success={},next_resume_at=None,
            delivery={'boss':stats(), 'liepin':stats(stop_reason='risk_control_paused'), 'maimai':stats()}))
        self.assertIn('本轮无动作：BOSS、猎聘（原站风控暂停，需本人处理）、脉脉',text)
        self.assertNotIn('猎聘：成功 0',text)
