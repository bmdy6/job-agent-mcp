from state_helpers import write_state_file
"""Monitor-only configuration reaches scheduling, alerts, notifications and settings."""
import asyncio
import contextlib
import io
import json
import time
import unittest
from unittest.mock import MagicMock, patch
from datetime import datetime
from zoneinfo import ZoneInfo
import test_monitor
from test_monitor import Source, event
from job_agent.config import load_config
from job_agent.config.monitor import MonitorSettings
from job_agent.monitor import Monitor
from job_agent.service.settings import update_settings


def stamp(clock):
    return datetime.fromisoformat('2026-11-28T'+clock).replace(tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()


class MonitorSettingsTests(unittest.TestCase):
    setUp = test_monitor.MonitorTests.setUp

    def settings(self):
        return MonitorSettings(allowed_hours=['08:00','24:00'], interval_minutes=5,
            interval_schedule=[{'allowed_hours':['21:00','08:00'],'interval_minutes':15}],
            platform_overrides={'liepin':{'interval_minutes':7,'backfill_hours':12,
                'failure_threshold':2,'notify':{'rejection':False}}, '51job':{'enabled':False}})

    def test_merge_schedule_midnight_and_validation(self):
        s=self.settings()
        self.assertEqual(s.for_platform('boss').interval_at(stamp('22:00')),15)
        self.assertEqual(s.for_platform('boss').interval_at(stamp('10:00')),5)
        self.assertEqual(s.for_platform('liepin').interval_at(stamp('22:00')),15)
        self.assertEqual(s.for_platform('liepin').interval_at(stamp('10:00')),7)
        self.assertEqual(s.for_platform('liepin').backfill_hours,12)
        self.assertTrue(s.for_platform('liepin').notify.interview)
        self.assertFalse(s.for_platform('liepin').notify.rejection)
        self.assertFalse(s.enabled('51job'))
        self.assertEqual(MonitorSettings().allowed_hours,('09:00','21:00'))
        for kwargs in [dict(allowed_hours=['24:00','21:00']),dict(allowed_hours=['08:00','24:01']),
                       dict(interval_minutes=0),dict(interval_schedule=[{'allowed_hours':['22:00','08:00'],'interval_minutes':0}]),
                       dict(platform_overrides={'unknown':{}}),dict(notify={'interview':'yes'})]:
            with self.subTest(kwargs=kwargs),self.assertRaises(ValueError):MonitorSettings(**kwargs)

    def test_sparse_overrides_survive_model_roundtrip(self):
        s=MonitorSettings(interval_minutes=11,notify={'interview':False},
            platform_overrides={'liepin':{'notify':{'rejection':False}}})
        restored=MonitorSettings.model_validate(s.model_dump())
        self.assertEqual(restored.for_platform('liepin').interval_minutes,11)
        self.assertFalse(restored.for_platform('liepin').notify.interview)
        self.assertFalse(restored.for_platform('liepin').notify.rejection)

    def test_scheduled_poll_honors_per_platform_intervals_and_disabled_sources(self):
        s=self.settings(); sources=[Source('boss'),Source('liepin'),Source('51job')]
        m=Monitor(self.browser,sources,self.store,self.config,s)
        with patch('job_agent.monitor.poller.time.time',return_value=stamp('10:00')):
            first=m.poll_once(scheduled=True)
        self.assertNotIn('51job',first['platforms'])
        with patch('job_agent.monitor.poller.time.time',return_value=stamp('10:05')):
            second=m.poll_once(scheduled=True)
        self.assertEqual(second['platforms']['boss']['status'],'ok')
        self.assertEqual(second['platforms']['liepin']['status'],'not_due')
        self.assertEqual([len(x.calls) for x in sources],[2,1,0])
        # Manual one-shot remains usable for diagnostics.
        m.poll_once()
        self.assertEqual([len(x.calls) for x in sources],[3,2,0])

    def test_platform_backfill_and_threshold_reach_consumers(self):
        s=self.settings();bad=Source('liepin',error=RuntimeError())
        with patch('job_agent.monitor.poller.ConversationCursor') as cursor:
            cursor.return_value.stats.return_value={}
            m=Monitor(self.browser,[bad],self.store,self.config,s)
            m.poll_once();self.sender.assert_not_called()
            m.poll_once();self.assertEqual(self.sender.call_count,1)
            self.assertEqual(cursor.call_args.args[2],12)

    def test_notification_switches_preserve_replies_and_can_be_reenabled(self):
        s=MonitorSettings(notify={'interview':False,'resume_request':False,'reply':False,'rejection':False})
        self.config.engine.monitor=s
        replies=[event(message='interview',body='邀请你明天来面试'),event(message='resume',body='请发一下简历'),
                 event(message='reject',body='不合适'),event(message='ordinary',body='请问你在哪里？')]
        m=Monitor(self.browser,[Source('boss',replies)],self.store,self.config,s)
        self.assertEqual(m.poll_once()['sent'],0)
        self.assertEqual(len(self.store.list_replies()),4)
        s.notify.interview=True
        self.assertEqual(m.poll_once()['sent'],1)
        self.assertEqual(m.poll_once()['sent'],0)
        s.notify.resume_request=s.notify.reply=s.notify.rejection=True
        self.assertEqual(m.poll_once()['sent'],3)

    def test_notify_platform_override_and_status_invitation(self):
        self.config.engine.monitor=MonitorSettings(notify={'interview':False,'reply':False},
            platform_overrides={'51job':{'notify':{'interview':True}}})
        row=self.store.upsert_job(platform='51job',platform_job_id='j1')
        self.store.record_delivery(row,status='sent')
        e=event(platform='51job',message='51job-status:j1:2026-11-28:邀面试',body='51job 状态变化，没有正文\n申请状态：邀面试')
        e.extra['id_origin']='application_status'
        m=Monitor(self.browser,[Source('51job',[e])],self.store,self.config)
        self.assertEqual(m.poll_once()['sent'],1)
        self.assertEqual(m.poll_once()['sent'],0)

    def test_notification_backfill_override_uses_fixed_activation_on_restart(self):
        from job_agent.monitor.notifications import poll_replies
        from datetime import timezone
        now=time.time()
        self.config.engine.monitor=MonitorSettings(backfill_hours=1,
            platform_overrides={'boss':{'backfill_hours':3}})
        self.store.add_reply(self.jid,message_id='older',direction='inbound',body='example',
            occurred_at=datetime.fromtimestamp(now-7200,timezone.utc).isoformat())
        with patch('job_agent.monitor.notifications.time.time',return_value=now):
            self.assertEqual(poll_replies(self.store,self.config)['sent'],1)
        # Keep baseline while changing the configured window.
        self.store.add_reply(self.jid,message_id='later-arrival',direction='inbound',body='example',
            occurred_at=datetime.fromtimestamp(now-7200,timezone.utc).isoformat())
        self.config.engine.monitor.platform_overrides['boss'].backfill_hours=1
        self.assertEqual(poll_replies(self.store,self.config)['sent'],0)
        self.config.engine.monitor.platform_overrides['boss'].backfill_hours=3
        with patch('job_agent.monitor.notifications.time.time',return_value=now+86400):
            self.assertEqual(poll_replies(self.store,self.config)['sent'],1)

    def test_full_day_and_midnight_do_not_change_shared_rate_model(self):
        from job_agent.config.rate import RatePolicy
        from unittest.mock import Mock
        for hours in [['00:00','24:00'],['08:00','24:00']]:
            s=MonitorSettings(allowed_hours=hours)
            limiter=Mock()
            m=Monitor(self.browser,[Source('boss')],self.store,self.config,s,limiter=limiter)
            m.poll_once()
            policy=limiter.acquire.call_args.args[2]
            self.assertTrue(policy.allowed(stamp('23:59')))
            self.assertEqual(policy.allowed(stamp('00:00')),hours[0]=='00:00')
        with self.assertRaises(ValueError):RatePolicy(allowed_hours=['00:00','24:00'])

    def test_expanding_backfill_reaches_source_without_resetting_cursor(self):
        write_state_file(self.home/'monitor-policy.json', json.dumps({'activated_at':1000000,'cutoff':1000000-3600,'backfill_hours':1}))
        self.store.save_monitor_conversations('boss',1000000-3600,[])
        self.config.engine.monitor.backfill_hours=3
        seen=[]
        class Capture(Source):
            def fetch(self,space,*,since=None,cursor=None):
                seen.append(since)
                return []
        Monitor(self.browser,[Capture('boss')],self.store,self.config).poll_once()
        self.assertEqual(seen,[1000000-3*3600])

    def test_monitor_loader_preserves_schedule_timezone(self):
        from job_agent.monitor.config import load_settings
        (self.home/'config.yaml').write_text('engine: {timezone: UTC}\n')
        self.assertEqual(load_settings()[0].engine.timezone,'UTC')

    def test_scheduling_state_write_failure_releases_platform_lock(self):
        from unittest.mock import Mock
        ownership=MagicMock()
        m=Monitor(self.browser,[Source('boss')],self.store,self.config)
        m._save=Mock(side_effect=OSError('fixture'))
        with patch('job_agent.monitor.poller.platform_lock',return_value=ownership),self.assertRaises(OSError):
            m.poll_once(scheduled=True)
        ownership.__exit__.assert_called_once()

    def test_cli_and_mcp_settings_preview_apply_readback(self):
        from job_agent.cli import main
        import job_agent.server as server
        path=self.home/'config.yaml';path.write_text('# keep\nnotify: {enabled: false}\n')
        changes={'engine.monitor.allowed_hours':['08:00','24:00'],
                 'engine.monitor.platforms':['51job','liepin'],
                 'engine.monitor.interval_schedule':[{'allowed_hours':['21:00','08:00'],'interval_minutes':15}],
                 'engine.monitor.platform_overrides.liepin.failure_threshold':4,
                 'engine.monitor.platform_overrides.liepin.backfill_hours':24,
                 'engine.monitor.platform_overrides.liepin.notify.rejection':False}
        async def check():
            preview=(await server.mcp.call_tool('update_settings',{'changes':changes}))[1]
            self.assertFalse(preview['applied']);self.assertNotIn('engine:',path.read_text())
            applied=(await server.mcp.call_tool('update_settings',{'changes':changes,'confirm':True}))[1]
            self.assertTrue(applied['applied'])
        asyncio.run(check())
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(['settings','set','engine.monitor.interval_minutes','9','--confirm']),0)
        self.assertTrue(json.loads(out.getvalue())['applied'])
        s=load_config().engine.monitor
        self.assertEqual(s.interval_minutes,9)
        self.assertEqual(s.for_platform('liepin').failure_threshold,4)
        self.assertFalse(s.for_platform('liepin').notify.rejection)
        self.assertIn('# keep',path.read_text())
