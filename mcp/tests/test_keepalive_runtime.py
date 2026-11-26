import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from config import Settings
from browser.ego import _SCRIPT
from browser.notifications import space_alert


class KeepaliveRuntimeTests(unittest.TestCase):
    def test_settings_strict_and_default(self):
        self.assertEqual(Settings().browser.keepalive_seconds, 300)
        self.assertEqual(Settings(browser={'keepalive_seconds': 0}).browser.keepalive_seconds, 0)
        for value in [-1, 0.5, True, '300', float('nan')]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                Settings(browser={'keepalive_seconds': value})

    def test_monitor_and_delivery_receive_interval_and_notification_callback(self):
        from monitor.runtime import create_browser
        from service.runtime import dependencies
        config = Settings(browser={'keepalive_seconds': 123})
        with patch('browser.ego.EgoBrowser') as factory:
            create_browser(config)
            self.assertEqual(factory.call_args.kwargs['keepalive_seconds'], 123)
            self.assertTrue(callable(factory.call_args.kwargs['on_event']))
        with patch('service.runtime.EgoBrowser') as factory:
            dependencies(config.engine, browser_settings=config.browser, notify_settings=config.notify)
            self.assertEqual(factory.call_args.kwargs['keepalive_seconds'], 123)
            self.assertTrue(callable(factory.call_args.kwargs['on_event']))

    def test_collect_receives_interval_and_stops_driver(self):
        from collect.runtime import collect_once
        import os
        config = Settings(browser={'keepalive_seconds': 123}, collect={
            'keywords': ['test'], 'cities': ['test'], 'platforms': {'boss': {'enabled': True}}})
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {'JOB_AGENT_HOME': temp}), \
                patch('collect.runtime.load_config', return_value=config), \
                patch('browser.ego.EgoBrowser') as factory, \
                patch('collect.runtime.create_sources', return_value=[]):
            collect_once()
            self.assertEqual(factory.call_args.kwargs['keepalive_seconds'], 123)
            factory.return_value.shutdown.assert_called_once_with(config.collect.task_space_name)

    def test_alert_is_opt_in_telegram_only_and_reports_failure(self):
        settings = Settings(notify={'enabled': True, 'channels': {
            'tg': {'type': 'telegram', 'enabled': True, 'chat_id': 'fake'},
            'off': {'type': 'telegram', 'enabled': False},
            'web': {'type': 'webhook', 'enabled': True}}}).notify
        with patch('browser.notifications.notify.send_one', return_value={'success': True}) as send:
            space_alert(Settings().notify, 'job-agent 监听', 'recreated')
            send.assert_not_called()
            for event in ['recreated', 'unavailable', 'keepalive_failed']:
                space_alert(settings, 'job-agent 监听', event)
            self.assertEqual(send.call_count, 3)
            self.assertIn('重建', send.call_args_list[0].args[2])
            send.return_value = {'success': False}
            with self.assertRaises(RuntimeError):
                space_alert(settings, 'job-agent 监听', 'recreated')

    @unittest.skipUnless(shutil.which('node'), 'Node needed for offline public API simulation')
    def test_driver_script_only_touches_owned_anchor_and_recreates_missing_name(self):
        for mode in ['existing', 'missing', 'user', 'inactive', 'no_anchor', 'duplicate']:
            with self.subTest(mode=mode):
                setup = '''
const request={op:"keepalive",name:"job-agent 测试",anchor:"p1"};
const mode=MODE,actions=[];
const meta={id:47,name:request.name,ownership:mode==="user"?"user":"agent"};
const page={label:"p1",goto:async u=>actions.push(["goto",u]),evaluate:async js=>{actions.push(["evaluate",js]);return 1;}};
globalThis.listTaskSpaces=async()=>[{id:1,name:"other",ownership:"user"},...(mode==="missing"?[]:mode==="duplicate"?[meta,meta]:[meta])];
globalThis.taskSpace=async n=>{actions.push(["taskSpace",n]);if(mode==="inactive")throw {code:"EGO_TASK_SPACE_INACTIVE"};return {
 spaceId:47,ownership:"agent",tabs:async()=>[...(mode==="existing"?[{label:"p2",openedBy:"agent",url:"about:blank"}]:[]),{label:"p1",openedBy:mode==="no_anchor"?"user":"agent",url:mode==="missing"?"chrome://newtab/":"about:blank"}],
 page:label=>{if(label!==(mode==="no_anchor"?"p2":"p1"))throw Error("wrong_anchor");return page;},newPage:async()=>{actions.push(["newPage"]);return {...page,label:"p2"};}
};};
'''.replace('MODE', json.dumps(mode))
                proc = subprocess.run(['node', '--input-type=module'], input=setup + _SCRIPT +
                    '\nconsole.log("__ACTIONS__"+JSON.stringify(actions));', text=True, capture_output=True, timeout=5)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                lines = proc.stdout.splitlines()
                result = json.loads(next(s.removeprefix('__JOB_AGENT_RESULT__') for s in lines if s.startswith('__JOB_AGENT_RESULT__')))
                actions = json.loads(next(s.removeprefix('__ACTIONS__') for s in lines if s.startswith('__ACTIONS__')))
                self.assertEqual(result['ok'], mode in {'existing', 'missing', 'no_anchor'})
                if mode in {'user', 'duplicate'}:
                    self.assertEqual(actions, [])
                elif mode == 'existing':
                    self.assertEqual(actions, [['taskSpace', 'job-agent 测试'], ['evaluate', '1']])
                elif mode == 'missing':
                    self.assertEqual(actions, [['taskSpace', 'job-agent 测试'], ['goto', 'about:blank'], ['evaluate', '1']])
                elif mode == 'no_anchor':
                    self.assertEqual(actions, [['taskSpace', 'job-agent 测试'], ['newPage'], ['goto', 'about:blank'], ['evaluate', '1']])
