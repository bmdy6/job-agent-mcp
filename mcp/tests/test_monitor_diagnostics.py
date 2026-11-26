import json
import unittest
import test_monitor
from test_monitor import Source
from browser.base import BrowserError

class DiagnosticTests(unittest.TestCase):
    setUp = test_monitor.MonitorTests.setUp
    monitor = test_monitor.MonitorTests.monitor

    def error(self, reason, stage='read_summary'):
        error=BrowserError('PRIVATE message person https://private/path')
        error.stage=stage; error.reason_code=reason
        return error

    def test_reasons_restart_recovery_retry_and_runtime_fields(self):
        source=Source('liepin',error=self.error('summary_field_missing:latestMsgId'))
        self.monitor([source]).poll_once()
        row=self.monitor([source]).poll_once()['platforms']['liepin']
        self.assertEqual(row['stage'],'read_summary')
        self.assertEqual(row['consecutive_failures'],2)
        self.assertEqual(self.sender.call_count,1)
        body=self.sender.call_args.args[2]
        for value in ['liepin','read_summary','summary_field_missing:latestMsgId','连续失败次数：2','上次成功时间','处理建议']:
            self.assertIn(value,body)
        source.error=self.error('page_timeout','click_conversation')
        self.monitor([source]).poll_once()
        source.error=self.error('summary_field_missing:latestMsgId')
        self.monitor([source]).poll_once()
        self.assertEqual(self.sender.call_count,2)
        source.error=None
        self.sender.return_value={'success':False}
        self.monitor([source]).poll_once()
        self.assertIn('已恢复',self.sender.call_args.args[2])
        self.sender.return_value={'success':True}
        row=self.monitor([source]).poll_once()['platforms']['liepin']
        calls=self.sender.call_count
        self.monitor([source]).poll_once()
        self.assertEqual(self.sender.call_count,calls)
        self.assertEqual(row['consecutive_failures'],0)
        self.assertTrue(row['last_success_at'])
        self.assertEqual(row['last_failure']['reason_code'],'summary_field_missing:latestMsgId')
        self.assertNotIn('PRIVATE',str(self.sender.call_args_list))
        self.assertNotIn('PRIVATE',(self.home/'monitor-state.json').read_text())

    def test_no_recovery_without_delivered_alert(self):
        source=Source('liepin',error=self.error('page_timeout'))
        self.monitor([source]).poll_once()
        source.error=None
        self.monitor([source]).poll_once()
        self.sender.assert_not_called()

    def test_runtime_persists_failure_diagnostics(self):
        from monitor.runtime import run
        from unittest.mock import patch
        source=Source('liepin',error=self.error('summary_field_missing:latestMsgId'))
        with patch('monitor.runtime.create_browser',return_value=self.browser), \
             patch('monitor.runtime.create_sources',return_value=[source]), \
             patch('service.safety.PlatformSafety.require'), \
             patch('engine.rate.RateLimiter.acquire'):
            self.assertEqual(run(once=True),1)
        data=json.loads((self.home/'monitor-runtime.json').read_text())
        self.assertEqual(data['platforms']['liepin']['stage'],'read_summary')
        self.assertEqual(data['platforms']['liepin']['reason_code'],'summary_field_missing:latestMsgId')
        self.assertNotIn('PRIVATE',json.dumps(data))

    def test_alert_retries_each_channel_and_unknown_text_is_redacted(self):
        self.config.notify.channels['second']=self.config.notify.channels['tg'].model_copy()
        source=Source('liepin',error=self.error('https://private/person','PRIVATE name'))
        self.sender.return_value={'success':False}
        self.monitor([source]).poll_once()
        self.monitor([source]).poll_once()
        self.assertEqual(self.sender.call_count,2)
        self.sender.return_value={'success':True}
        self.monitor([source]).poll_once()
        self.monitor([source]).poll_once()
        self.assertEqual(self.sender.call_count,4)
        source.error=None
        self.monitor([source]).poll_once()
        self.assertEqual(self.sender.call_count,6)
        self.assertNotIn('PRIVATE',str(self.sender.call_args_list))
        self.assertNotIn('https://private',(self.home/'monitor-state.json').read_text())

    def test_legacy_space_receipts_recover_without_missing_fields(self):
        (self.home/'monitor-state.json').write_text(json.dumps({'space':{'count':1,'kind':'space','notified':['tg']}}))
        self.monitor([Source('liepin')]).poll_once()
        self.assertEqual(self.sender.call_count,1)
        self.assertIn('已恢复',self.sender.call_args.args[2])

    def test_risk_alert_retries_without_revisiting_site_or_inflating_count(self):
        error=self.error('risk_control','guard_page'); error.code='risk_control'
        source=Source('liepin',error=error)
        self.sender.return_value={'success':False}
        self.monitor([source]).poll_once()
        self.sender.return_value={'success':True}
        self.monitor([source]).poll_once()
        self.monitor([source]).poll_once()
        self.assertEqual(len(source.calls),1)
        self.assertEqual(self.sender.call_count,2)
        state=json.loads((self.home/'monitor-state.json').read_text())
        self.assertEqual(state['liepin']['count'],1)

    def test_session_preflight_uses_detailed_alert_and_rate_wait_does_not(self):
        from engine.rate import RateDeferred
        from unittest.mock import Mock
        source=Source('liepin')
        safety=Mock(); safety.require.side_effect=RateDeferred('login_required')
        monitor=self.monitor([source]); monitor.safety=safety
        row=monitor.poll_once()['platforms']['liepin']
        self.assertEqual(row['stage'],'session_preflight')
        self.assertEqual(row['reason_code'],'login_required')
        self.assertEqual(len(source.calls),0)
        self.assertEqual(self.sender.call_count,1)
        self.sender.reset_mock()
        limiter=Mock(); limiter.acquire.side_effect=RateDeferred('outside_hours')
        monitor=self.monitor([Source('boss')]); monitor.limiter=limiter
        monitor.poll_once()
        self.sender.assert_not_called()
