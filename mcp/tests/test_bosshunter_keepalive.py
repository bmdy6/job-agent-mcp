"""Offline tests only: never launch Ego or touch the user's configuration."""
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[2] / 'examples' / 'bosshunter_keepalive.py'
spec = importlib.util.spec_from_file_location('bosshunter_keepalive', SOURCE)
keepalive = importlib.util.module_from_spec(spec)
spec.loader.exec_module(keepalive)


class BossHunterKeepaliveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = Path(self.tmp.name) / 'config.yaml'
        self.original = ('# keep comments\r\ndelivery:\r\n  ego_task_space_id: 28 # old\r\n'
                         'collection:\r\n  ego_task_space_id: 88\r\nego_task_space_id: 99\r\nother: "原样"\r\n').encode()
        self.config.write_bytes(self.original)

    def test_backup_and_only_target_field_changes(self):
        backup = keepalive.update_config(self.config, self.original, 55)
        self.assertEqual(backup.read_bytes(), self.original)
        self.assertEqual(self.config.read_bytes(), self.original.replace(b'28 # old', b'55 # old'))
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
        self.assertIsNone(keepalive.update_config(self.config, self.config.read_bytes(), 55))
        self.assertEqual(len(list(self.config.parent.glob('*.bak-*'))), 1)

    def test_missing_key_is_added_without_reformatting(self):
        self.config.write_text('delivery:\n  enabled: true\nother: 1')
        keepalive.update_config(self.config, self.config.read_bytes(), 55)
        self.assertEqual(self.config.read_text(), 'delivery:\n  ego_task_space_id: 55\n  enabled: true\nother: 1')
        self.config.write_text('delivery:\n    ego_task_space_id: null # default\nother: 1\n')
        keepalive.update_config(self.config, self.config.read_bytes(), 55)
        self.assertEqual(self.config.read_text(), 'delivery:\n    ego_task_space_id: 55 # default\nother: 1\n')

    def test_invalid_and_ambiguous_yaml_rejected_before_browser(self):
        for text in ['[1,2]', 'ego_task_space_id: [', 'ego_task_space_id: 1\n',
                     'delivery:\n  ego_task_space_id: 1\n  ego_task_space_id: 2\n',
                     'delivery:\n  ego_task_space_id: &id 1\nother: *id\n',
                     'delivery: {ego_task_space_id: 1}\n',
                     'delivery:\n  ego_task_space_id: 1\n  "ego_task_space_id": 2\n']:
            with self.subTest(text=text), patch.object(keepalive, 'pulse') as pulse:
                self.config.write_text(text)
                with self.assertRaises(ValueError):
                    keepalive.run(self.config, '/fake/ego-browser')
                pulse.assert_not_called()
                self.assertEqual(self.config.read_text(), text)

    def test_concurrent_edit_is_preserved(self):
        self.config.write_text('other: changed\n')
        with self.assertRaises(ValueError):
            keepalive.update_config(self.config, self.original, 55)
        self.assertEqual(self.config.read_text(), 'other: changed\n')

    def test_cli_failure_does_not_write_config(self):
        with patch.object(keepalive.subprocess, 'run', side_effect=subprocess.TimeoutExpired('ego', 30)) as call:
            with self.assertRaises(subprocess.TimeoutExpired):
                keepalive.run(self.config, '/fake/ego-browser')
            self.assertEqual(call.call_count, 1)
        self.assertEqual(self.config.read_bytes(), self.original)
        self.assertFalse(list(self.config.parent.glob('*.bak-*')))

    def test_existing_space_repairs_stale_id_after_prior_write_failure(self):
        with patch.object(keepalive, 'pulse', return_value={'space_id': 55, 'created': False}):
            keepalive.run(self.config, '/fake/ego-browser')
        self.assertIn(b'ego_task_space_id: 55', self.config.read_bytes())

    def test_atomic_replace_failure_keeps_original_and_backup(self):
        with patch.object(keepalive.os, 'replace', side_effect=OSError('disk error')):
            with self.assertRaises(OSError):
                keepalive.update_config(self.config, self.original, 55)
        self.assertEqual(self.config.read_bytes(), self.original)
        self.assertEqual(len(list(self.config.parent.glob('*.bak-*'))), 1)
        self.assertFalse(list(self.config.parent.glob('*.tmp-*')))

    @unittest.skipUnless(shutil.which('node'), 'Node needed for offline Ego API simulation')
    def test_browser_script_lifecycle(self):
        for mode in ['existing', 'missing', 'user', 'delegated', 'inactive', 'duplicate', 'no_owned_page']:
            with self.subTest(mode=mode):
                # This is plain Node with fake public APIs, never ego-browser.
                setup = '''
const actions=[];
const mode=MODE;
const page={label:"p1", goto:async u=>actions.push(["goto",u]), evaluate:async s=>{actions.push(["evaluate",s]);return 1;}};
const metadata={id:55,name:"BossHunter 投递",ownership:mode==="user"?"user":mode==="delegated"?"agentDelegatedToUser":"agent"};
globalThis.listTaskSpaces=async()=>mode==="missing"?[]:mode==="duplicate"?[metadata,metadata]:[metadata];
globalThis.taskSpace=async value=>{actions.push(["taskSpace",value]);if(mode==="inactive")throw Error("inactive");return {
 spaceId:55, ownership:metadata.ownership,
 tabs:async()=>mode==="missing"?[]:[{label:"p1",openedBy:mode==="no_owned_page"?"unknown":"agent",url:"about:blank"}],
 page:()=>page
};};
'''.replace('MODE', json.dumps(mode))
                proc = subprocess.run(['node', '--input-type=module'],
                    input=setup + keepalive.SCRIPT + '\nconsole.log("__ACTIONS__"+JSON.stringify(actions));',
                    text=True, capture_output=True, timeout=5)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                lines = proc.stdout.splitlines()
                result = json.loads(next(s.removeprefix('__BOSSHUNTER_KEEPALIVE__') for s in lines if s.startswith('__BOSSHUNTER_KEEPALIVE__')))
                actions = json.loads(next(s.removeprefix('__ACTIONS__') for s in lines if s.startswith('__ACTIONS__')))
                self.assertEqual(result['ok'], mode in {'existing', 'missing'})
                if mode == 'existing':
                    self.assertEqual(actions, [['taskSpace', 55], ['evaluate', '1']])
                elif mode == 'missing':
                    self.assertEqual(actions, [['taskSpace', 'BossHunter 投递'],
                        ['goto', 'https://www.zhipin.com/web/geek/chat'], ['evaluate', '1']])
                elif mode in {'user', 'delegated', 'duplicate'}:
                    self.assertEqual(actions, [])
