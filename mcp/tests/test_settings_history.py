"""History integration uses a disposable JOB_AGENT_HOME."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from job_agent.config import load_config
from job_agent.service import settings
from job_agent.service.daily import run_lock


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name).resolve()
        env = patch.dict(os.environ, JOB_AGENT_HOME=str(self.home))
        env.start()
        self.addCleanup(env.stop)
        self.main, self.daily = self.home/'config.yaml', self.home/'daily.yaml'
        self.original = b'# keep\r\nmode: job\r\nanalytics: {activity_days: 7}\r\n'
        self.main.write_bytes(self.original)

    def change(self, changes=None, **kw):
        return settings.update_settings(changes or {'analytics.activity_days': 8}, confirm=True, **kw)

    def history(self):
        return settings.settings_history()['history']

    def test_audit_metadata_values_and_private_exact_backup(self):
        result = self.change(actor='operator', reason='weekly adjustment', source='cli')
        row = self.history()[0]
        self.assertEqual(row['id'], result['history_id'])
        self.assertEqual((row['actor'], row['source'], row['reason']), ('operator', 'cli', 'weekly adjustment'))
        self.assertEqual(row['status'], 'applied')
        self.assertEqual(row['changes'], result['changes'])
        self.assertTrue(row['available'])
        backup = Path(result['backups']['config.yaml'])
        self.assertEqual(backup.parent, self.home/'config-history')
        self.assertIn('analytics.activity_days', backup.name)
        self.assertEqual(backup.read_bytes(), self.original)
        for path, mode in ((backup, 0o600), (backup.parent, 0o700), (backup.parent/'changes.jsonl', 0o600)):
            self.assertEqual(path.stat().st_mode & 0o777, mode)
        self.assertFalse(list(self.home.glob('*.backup-*')))

    def test_preview_noop_and_invalid_changes_never_create_history(self):
        settings.update_settings({'analytics.activity_days': 8})
        self.change({'analytics.activity_days': 7})
        with self.assertRaises(ValueError): self.change({'analytics.activity_days': -1})
        self.assertFalse((self.home/'config-history').exists())

    def test_exact_two_file_rollback_and_undo_of_rollback(self):
        raw = b'# additive\ndaily: {enabled: false, minute: 15}\n'
        self.daily.write_bytes(raw)
        first = self.change({'mode': 'practice', 'daily.enabled': True})
        changed = [p.read_bytes() for p in (self.main, self.daily)]
        self.assertFalse(settings.rollback_settings(first['history_id'])['applied'])
        self.assertEqual(self.main.read_bytes(), changed[0])
        self.assertEqual(len(self.history()), 1)
        back = settings.rollback_settings(first['history_id'], confirm=True, reason='undo test')
        self.assertEqual([p.read_bytes() for p in (self.main, self.daily)], [self.original, raw])
        self.assertEqual(self.history()[0]['rollback_of'], first['history_id'])
        self.assertEqual(self.history()[0]['operation'], 'rollback')
        settings.rollback_settings(back['history_id'], confirm=True)
        self.assertEqual([p.read_bytes() for p in (self.main, self.daily)], changed)

    def test_rollback_restores_absence(self):
        self.main.unlink()
        change = self.change({'mode': 'practice'})
        settings.rollback_settings(change['history_id'], confirm=True)
        self.assertFalse(self.main.exists())
        self.assertEqual(load_config().mode, 'job')

    def test_daily_only_change_also_backs_up_main(self):
        self.daily.write_text('daily: {enabled: false}\n')
        change = self.change({'daily.enabled': True})
        self.assertEqual(set(change['backups']), {'config.yaml', 'daily.yaml'})
        self.assertEqual(self.main.read_bytes(), self.original)

    def test_retention_keeps_fifty_snapshots_and_all_audit_rows(self):
        first = None
        for i in range(52):
            result = self.change({'analytics.activity_days': i+8})
            first = first or result['history_id']
        rows = self.history()
        self.assertEqual(len(rows), 52)
        self.assertEqual(len(list((self.home/'config-history').glob('*.yaml'))), 50)
        self.assertTrue(rows[0]['available'])
        self.assertFalse(rows[-1]['available'])
        with self.assertRaisesRegex(ValueError, 'available'):
            settings.rollback_settings(first, confirm=True)
        self.assertEqual(load_config().analytics.activity_days, 59)

    def test_retention_does_not_split_two_file_versions(self):
        self.daily.write_text('daily: {minute: 0}\n')
        for i in range(27): self.change({'daily.minute': i+1})
        self.assertEqual(len(list((self.home/'config-history').glob('*.yaml'))), 50)
        self.assertEqual(sum(row['available'] for row in self.history()), 25)

    def test_audit_failure_restores_config(self):
        from job_agent.service import settings_history as history
        real_event = history.append_event
        def event(home, data):
            if data.get('status') == 'applied': raise OSError('private diagnostic')
            return real_event(home, data)
        with patch.object(history, 'append_event', side_effect=event):
            with self.assertRaisesRegex(ValueError, 'rolled back'): self.change()
        self.assertEqual(self.main.read_bytes(), self.original)
        self.assertEqual(self.history()[0]['status'], 'failed')
        self.assertFalse(self.history()[0]['available'])

    def test_error_and_interrupt_restore_both_files(self):
        self.daily.write_text('daily: {enabled: false}\n')
        original_write = settings.atomic_write
        for failure in (OSError('private input'), KeyboardInterrupt()):
            calls = []
            def write(path, content, mode=0o600):
                calls.append(path.name)
                if path.name == 'daily.yaml' and calls.count('daily.yaml') == 1: raise failure
                return original_write(path, content, mode)
            with patch.object(settings, 'atomic_write', side_effect=write):
                with self.assertRaises((ValueError, KeyboardInterrupt)):
                    self.change({'mode': 'practice', 'daily.enabled': True})
            self.assertEqual(self.main.read_bytes(), self.original)
            self.assertEqual(self.daily.read_text(), 'daily: {enabled: false}\n')
        self.assertTrue(all(row['status'] == 'failed' for row in self.history()))

    def test_tampering_and_invalid_ids_are_refused(self):
        change = self.change()
        backup = Path(change['backups']['config.yaml'])
        raw = backup.read_bytes()
        backup.write_bytes(b'mode: practice\n')
        with self.assertRaises(ValueError): settings.rollback_settings(change['history_id'], confirm=True)
        backup.unlink()
        external = self.home/'external'
        external.write_bytes(raw)
        backup.symlink_to(external)
        with self.assertRaises(ValueError): settings.rollback_settings(change['history_id'], confirm=True)
        for key in ('../../config', '/config', '', 'unknown'):
            with self.assertRaises(ValueError): settings.rollback_settings(key, confirm=True)
        self.assertEqual(load_config().analytics.activity_days, 8)

    def test_history_and_journal_symlinks_are_refused(self):
        external = self.home/'external'
        external.mkdir()
        directory = self.home/'config-history'
        directory.symlink_to(external, target_is_directory=True)
        with self.assertRaises(ValueError): self.change()
        self.assertEqual(list(external.iterdir()), [])
        directory.unlink()
        directory.mkdir()
        target = external/'target'
        target.write_text('keep')
        (directory/'changes.jsonl').symlink_to(target)
        with self.assertRaises(ValueError): self.change()
        self.assertEqual(target.read_text(), 'keep')
        self.assertEqual(self.main.read_bytes(), self.original)

    def test_rollback_lock_and_confirm_type(self):
        change = self.change()
        with run_lock(self.home):
            with self.assertRaises(RuntimeError): settings.rollback_settings(change['history_id'], confirm=True)
        with self.assertRaises(ValueError): settings.rollback_settings(change['history_id'], confirm='true')

    def test_cli_settings_history_rollback_and_config_alias(self):
        from job_agent.cli import main
        def call(args):
            out = io.StringIO()
            with contextlib.redirect_stdout(out): self.assertEqual(main(args), 0)
            return json.loads(out.getvalue())
        change = call(['settings', 'set', 'mode', '"practice"', '--reason', 'test', '--actor', 'operator', '--confirm'])
        row = call(['settings', 'history'])['history'][0]
        self.assertEqual((row['actor'], row['source'], row['reason']), ('operator', 'cli', 'test'))
        call(['settings', 'rollback', change['history_id'], '--confirm'])
        self.assertEqual(self.main.read_bytes(), self.original)
        self.assertEqual(call(['config', 'show', 'mode'])['settings'], 'job')
        self.assertFalse((self.home/'logs').exists())

    def test_backups_and_pending_audit_exist_before_first_config_write(self):
        original_write = settings.atomic_write
        def write(path, content, mode=0o600):
            row = self.history()[0]
            self.assertEqual(row['status'], 'pending')
            self.assertFalse(row['available'])
            backup = self.home/'config-history'/(row['id']+'-config.yaml')
            self.assertEqual(backup.read_bytes(), self.original)
            return original_write(path, content, mode)
        with patch.object(settings, 'atomic_write', side_effect=write): self.change()

    def test_audit_omits_private_text_urls_and_nested_restoration_values(self):
        self.main.write_text('mode: job\n')
        change = self.change({'notify.ai.base_url': 'https://model.example.invalid/v1',
                              'engine.platforms.boss.greeting': 'PRIVATE-MESSAGE',
                              'engine.platforms.boss.greeting_max_length': 100,
                              'greetings.generate.facts': ['PRIVATE-FACT']})
        audit = (self.home/'config-history/changes.jsonl').read_text()
        for value in ('PRIVATE-MESSAGE', 'PRIVATE-FACT', 'https://model.example.invalid/v1'):
            self.assertNotIn(value, audit)
        back = settings.rollback_settings(change['history_id'], confirm=True)
        preview = settings.rollback_settings(back['history_id'])
        for value in ('PRIVATE-MESSAGE', 'PRIVATE-FACT', 'https://model.example.invalid/v1'):
            self.assertNotIn(value, json.dumps(preview))
        self.assertTrue(any(row['path'] == 'notify.ai.base_url' for row in preview['changes']))

    def test_rollback_model_validation_failure_restores_current_version(self):
        change = self.change()
        current = self.main.read_bytes()
        real_loader = load_config
        def loader():
            if self.main.read_bytes() == self.original: raise ValueError('retired schema')
            return real_loader()
        with patch.object(settings, 'load_config', side_effect=loader):
            with self.assertRaisesRegex(ValueError, 'rolled back'):
                settings.rollback_settings(change['history_id'], confirm=True)
        self.assertEqual(self.main.read_bytes(), current)
        self.assertEqual(self.history()[0]['status'], 'failed')

    def test_retention_error_reports_success_with_warning(self):
        from job_agent.service import settings_history as history
        with patch.object(history, 'prune', side_effect=OSError('private diagnostic')):
            result = self.change()
        self.assertTrue(result['applied'])
        self.assertIn('retention_warning', result)
        self.assertEqual(self.history()[0]['status'], 'applied')
        self.assertEqual(load_config().analytics.activity_days, 8)

    def test_corrupt_journal_and_invalid_metadata_fail_before_write(self):
        self.change()
        current = self.main.read_bytes()
        for kwargs in ({'actor': ''}, {'reason': '\n'}, {'source': 'x'*41}):
            with self.assertRaises(ValueError): self.change({'mode': 'practice'}, **kwargs)
        (self.home/'config-history/changes.jsonl').write_text('broken PRIVATE-DIAGNOSTIC')
        with self.assertRaises(ValueError) as error: self.change({'mode': 'practice'})
        self.assertNotIn('PRIVATE', str(error.exception))
        self.assertEqual(self.main.read_bytes(), current)

    def test_file_modes_and_format_only_rollback_are_preserved(self):
        self.main.chmod(0o640)
        change = self.change()
        self.assertEqual(self.main.stat().st_mode & 0o777, 0o640)
        settings.rollback_settings(change['history_id'], confirm=True)
        self.main.write_bytes(self.original.replace(b'# keep', b'# changed comment'))
        result = settings.rollback_settings(change['history_id'], confirm=True)
        self.assertTrue(result['applied'])
        self.assertEqual(self.main.read_bytes(), self.original)
        self.assertEqual(self.main.stat().st_mode & 0o777, 0o640)
