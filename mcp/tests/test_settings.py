"""All fixtures and subprocesses stay in a temporary JOB_AGENT_HOME."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from job_agent.config import load_config
from job_agent.service.daily import run_lock
from job_agent.service.settings import get_settings, update_settings


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.env = patch.dict(os.environ, JOB_AGENT_HOME=str(self.home), PRIVATE_TEST_TOKEN='NEVER-RETURN-ME')
        self.env.start()
        self.addCleanup(self.env.stop)
        self.path = self.home/'config.yaml'
        self.original = ('# private config\nmode: job # mode comment\nengine:\n'
                         '  platforms:\n    boss: {daily_limit: 10} # flow comment\n'
                         'analytics:\n  activity_days: 7 # keep days\n'
                         'notify:\n  channels:\n    tg:\n      type: telegram\n'
                         '      bot_token_env: PRIVATE_TEST_TOKEN # credential comment\n')
        self.path.write_text(self.original)

    def test_read_redacts_credentials_and_paths_and_has_today(self):
        result = get_settings()
        self.assertEqual(result['settings']['notify']['channels']['tg']['bot_token_env'], {'configured': True})
        self.assertNotIn('PRIVATE_TEST_TOKEN', json.dumps(result))
        self.assertNotIn('NEVER-RETURN-ME', json.dumps(result))
        self.assertEqual(result['today']['platforms']['boss']['limit'], 10)
        self.assertEqual(result['today']['platforms']['boss']['used'], 0)
        self.assertFalse((self.home/'jobs.sqlite3').exists())
        self.assertEqual(get_settings('engine.platforms.boss')['settings']['daily_limit'], 10)
        with self.assertRaises(ValueError): get_settings('engine.no_such_setting')

    def test_preview_has_diff_and_never_writes(self):
        before = {p.name: p.read_bytes() for p in self.home.iterdir()}
        result = update_settings({'engine.platforms.boss.daily_limit': 3})
        self.assertFalse(result['applied'])
        self.assertEqual(result['changes'], [{'path': 'engine.platforms.boss.daily_limit', 'before': 10,
                                             'after': 3, 'file': 'config.yaml'}])
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.home.iterdir()})

    def test_confirm_edits_only_target_spans_and_backs_up_original(self):
        result = update_settings({'engine.platforms.boss.daily_limit': 3, 'analytics.activity_days': 14}, confirm=True)
        expected = self.original.replace('daily_limit: 10', 'daily_limit: 3').replace('activity_days: 7', 'activity_days: 14')
        self.assertTrue(result['applied'])
        self.assertEqual(self.path.read_text(), expected)
        self.assertEqual(Path(result['backups']['config.yaml']).read_text(), self.original)
        self.assertEqual(load_config().analytics.activity_days, 14)

    def test_extension_ownership_and_main_precedence(self):
        extension = self.home/'daily.yaml'
        extension.write_text('# additive\ndaily:\n  enabled: false # opt-in\n  minute: 15\n')
        self.path.write_text(self.original + 'daily:\n  minute: 5 # main wins\n')
        update_settings({'daily.enabled': True, 'daily.minute': 10}, confirm=True)
        self.assertEqual(extension.read_text(), '# additive\ndaily:\n  enabled: true # opt-in\n  minute: 15\n')
        self.assertIn('minute: 10 # main wins', self.path.read_text())
        self.assertTrue(load_config().daily.enabled)
        self.assertEqual(load_config().daily.minute, 10)

    def test_new_mapping_paths_and_multiline_greeting(self):
        update_settings({'engine.day_limits.sat': 2, 'engine.day_allowed_hours.sun': ['10:00', '16:00'],
                         'engine.platforms.boss.greeting_max_length': 100,
                         'engine.platforms.boss.greeting': '你好\n想了解这个岗位',
                         'collect.platforms.boss.city_codes.上海': '101020100'}, confirm=True)
        cfg = load_config()
        self.assertEqual(cfg.engine.day_limits, {'sat': 2})
        self.assertEqual(cfg.engine.platforms['boss'].greeting, '你好\n想了解这个岗位')
        self.assertIn('# credential comment', self.path.read_text())

    def test_model_validation_and_whitelist_reject_without_writing(self):
        for changes in ({'engine.platforms.boss.daily_limit': -1}, {'engine.platforms.boss.daily_limit': True},
                        {'unknown': 1}, {'engine.platforms.other.enabled': True},
                        {'notify.channels.tg.bot_token_env': 'OTHER'}, {'scoring.resume_path': '/tmp/no'},
                        {'scoring.base_url': 'https://user:SECRET@other.invalid'}, {'scoring.key_source': 'env'},
                        {'notify.channels.tg': {'enabled': True, 'bot_token_env': 'OTHER'}},
                        {'scoring': {}}, {'engine.day_limits.nope': 2},
                        {'engine.platforms.boss.enabled': True}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                update_settings(changes, confirm=True)
            self.assertEqual(self.path.read_text(), self.original)
            self.assertEqual(list(self.home.glob('*.backup-*')), [])
        with self.assertRaises(ValueError): update_settings({'mode': 'job'}, confirm='true')

    def test_write_validation_failure_rolls_back_all_files(self):
        extension = self.home/'daily.yaml'
        extension.write_text('daily: {enabled: false}\n')
        real_loader = load_config
        def loader():
            if 'daily_limit: 3' in self.path.read_text():
                raise ValueError('INTERNAL PRIVATE INPUT')
            return real_loader()
        with patch('job_agent.service.settings.load_config', side_effect=loader):
            with self.assertRaisesRegex(ValueError, 'rolled back') as error:
                update_settings({'engine.platforms.boss.daily_limit': 3, 'daily.enabled': True}, confirm=True)
        self.assertNotIn('PRIVATE', str(error.exception))
        self.assertEqual(self.path.read_text(), self.original)
        self.assertEqual(extension.read_text(), 'daily: {enabled: false}\n')
        self.assertTrue(list((self.home/'config-history').glob('*-config.yaml')))

    def test_daily_lock_rejects_confirmation_but_preview_is_available(self):
        with run_lock(self.home):
            with self.assertRaisesRegex(RuntimeError, 'Daily round already running'):
                update_settings({'mode': 'practice'}, confirm=True)
            self.assertFalse(update_settings({'mode': 'practice'})['applied'])
        self.assertEqual(self.path.read_text(), self.original)
        self.assertEqual(list(self.home.glob('*.backup-*')), [])

    def test_alias_duplicate_and_merge_are_refused_for_edit(self):
        for raw in ('mode: job\nmode: practice\n', 'cities: &x [上海]\ndirections: *x\n',
                    'engine: {<<: {dry_run: true}}\n'):
            self.path.write_text(raw)
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                update_settings({'mode': 'practice'}, confirm=True)
            self.assertEqual(self.path.read_text(), raw)

    def test_cli_matches_service_and_preview_creates_no_logs(self):
        from job_agent.cli import main
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(main(['config', 'set', 'engine.platforms.boss.daily_limit', '3']), 0)
        self.assertFalse(json.loads(out.getvalue())['applied'])
        self.assertFalse((self.home/'logs').exists())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(main(['config', 'set', 'engine.platforms.boss.daily_limit', '3', '--confirm']), 0)
        self.assertTrue(json.loads(out.getvalue())['applied'])
        self.assertEqual(load_config().engine.platforms['boss'].daily_limit, 3)

    def test_block_scalar_edit_preserves_header_comment_and_neighbor(self):
        self.path.write_text('engine:\n  platforms:\n    boss:\n      greeting_max_length: 100\n'
                             '      greeting: |- # keep greeting comment\n        old text\n'
                             'analytics: {activity_days: 7} # neighbor\n')
        update_settings({'engine.platforms.boss.greeting': 'new\nsecond line'}, confirm=True)
        self.assertEqual(load_config().engine.platforms['boss'].greeting, 'new\nsecond line')
        self.assertIn('# keep greeting comment', self.path.read_text())
        self.assertIn('analytics: {activity_days: 7} # neighbor', self.path.read_text())

    def test_insert_after_block_scalar_preserves_parent_indentation(self):
        self.path.write_text('engine:\n  platforms:\n    boss:\n      greeting_max_length: 100\n'
                             '      greeting: |- # keep\n        old text\nanalytics: {activity_days: 7}\n')
        update_settings({'engine.platforms.boss.daily_limit': 3}, confirm=True)
        self.assertEqual(load_config().engine.platforms['boss'].daily_limit, 3)
        self.assertEqual(load_config().analytics.activity_days, 7)

    def test_read_only_usage_uses_engine_calendar_day_and_attempts(self):
        from job_agent.store import SQLiteDeliveryStore
        from datetime import datetime
        from zoneinfo import ZoneInfo
        at = datetime(2026, 11, 28, 12, tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
        with SQLiteDeliveryStore(self.home/'jobs.sqlite3', dry_run=False) as db:
            jid = db.upsert_job(platform='boss', platform_job_id='1')
            for offset in (0, -60, -86400):
                db.connection.execute('INSERT INTO delivery_attempts VALUES(?,?)', (jid, at+offset))
        with patch('job_agent.service.settings.time.time', return_value=at):
            self.assertEqual(get_settings()['today']['platforms']['boss']['used'], 2)

    def test_invalid_source_never_exposes_credential_in_error(self):
        self.path.write_text('notify: {channels: {tg: {type: telegram, bot_token_env: "SECRET/NOT-AN-ENV"}}}\n')
        for operation in (get_settings, lambda: update_settings({'mode': 'practice'})):
            with self.assertRaises(ValueError) as error:
                operation()
            self.assertNotIn('SECRET', str(error.exception))

    def test_crlf_and_quoted_scalars_survive_targeted_updates(self):
        raw = b'# windows\r\nmode: "job" # keep quote\r\nengine: {day_limits: {sat: 3}}\r\n'
        self.path.write_bytes(raw)
        update_settings({'mode': 'practice', 'engine.day_limits.sun': 2}, confirm=True)
        self.assertEqual(self.path.read_bytes(), raw.replace(b'"job"', b'"practice"').replace(b'sat: 3', b'sat: 3, sun: 2'))

    def test_failure_removes_new_config_file(self):
        self.path.unlink()
        real_loader = load_config
        def loader():
            if self.path.exists():
                raise ValueError('fail after write')
            return real_loader()
        with patch('job_agent.service.settings.load_config', side_effect=loader):
            with self.assertRaisesRegex(ValueError, 'rolled back'):
                update_settings({'mode': 'practice'}, confirm=True)
        self.assertFalse(self.path.exists())

    def test_dynamic_channel_safe_fields_and_schema_optional_rate(self):
        update_settings({'notify.channels.extra': {'type': 'webhook', 'enabled': False, 'format': 'generic'},
                         'engine.platforms.boss.rate.allowed_hours': ['10:00', '17:00']}, confirm=True)
        self.assertEqual(load_config().notify.channels['extra'].type, 'webhook')
        self.assertEqual(load_config().engine.platforms['boss'].rate.allowed_hours, ('10:00', '17:00'))
        with self.assertRaises(ValueError):
            update_settings({'notify.channels': {}}, confirm=True)

    def test_add_top_level_section_after_nested_mapping_without_final_newline(self):
        self.path.write_text('engine:\n  platforms:\n    boss: {daily_limit: 10}')
        update_settings({'daily.enabled': True}, confirm=True)
        self.assertTrue(load_config().daily.enabled)
        self.assertEqual(load_config().engine.platforms['boss'].daily_limit, 10)

    def test_public_endpoint_urls_editable_but_embedded_credentials_redacted(self):
        update_settings({'scoring.base_url': 'https://model.example.invalid/v1',
                         'browser.cdp_url': 'http://127.0.0.1:9333'}, confirm=True)
        self.assertEqual(get_settings('scoring')['settings']['base_url'], 'https://model.example.invalid/v1')
        self.path.write_text('notify: {ai: {base_url: "https://user:SECRET@api.example.invalid/v1?key=SECRET"}}\n')
        self.assertEqual(get_settings('notify.ai.base_url')['settings'], {'configured': True})
        self.assertNotIn('SECRET', json.dumps(get_settings()))
        with self.assertRaises(ValueError):
            update_settings({'notify.ai.base_url': 'https://api.example.invalid?key=SECRET'}, confirm=True)

    def test_replacing_legacy_credential_url_does_not_leak_old_value_in_diff(self):
        self.path.write_text('notify: {ai: {base_url: "https://user:SECRET@api.example.invalid/v1?key=SECRET"}}\n')
        result = update_settings({'notify.ai.base_url': 'https://api.example.invalid/v1'})
        self.assertEqual(result['changes'][0]['before'], {'configured': True})
        self.assertNotIn('SECRET', json.dumps(result))

    def test_today_includes_durable_rate_reservations_without_double_counting(self):
        from job_agent.store import SQLiteDeliveryStore
        from job_agent.store.automation import AutomationRepository
        from datetime import datetime
        from zoneinfo import ZoneInfo
        at = datetime(2026, 11, 28, 12, tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
        with SQLiteDeliveryStore(self.home/'jobs.sqlite3', dry_run=False) as db:
            jid = db.upsert_job(platform='boss', platform_job_id='1')
            repo = AutomationRepository(db)
            # Legacy attempt before reservations, reserved attempt and a reservation
            # interrupted before an attempt. The collector must not consume delivery quota.
            for stamp in (at-180, at-60):
                db.connection.execute('INSERT INTO delivery_attempts VALUES(?,?)', (jid, stamp))
            repo.record('boss', 'delivery', at-61, {})
            repo.record('boss', 'delivery', at-30, {})
            repo.record('boss', 'collect', at-20, {})
        with patch('job_agent.service.settings.time.time', return_value=at):
            row = get_settings()['today']['platforms']['boss']
        self.assertEqual(row['attempts_used'], 2)
        self.assertEqual(row['rate_used'], 3)
        self.assertEqual(row['used'], 3)
        self.assertEqual(row['remaining'], 7)
