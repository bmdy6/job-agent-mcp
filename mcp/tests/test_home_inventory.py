"""Metadata inventory and cleanup execution are tested only in temporary homes."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('home_inventory', ROOT/'scripts/home_inventory.py')


class InventoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def fixture(self, root):
        home = root/'home'
        home.mkdir()
        for name in ('config.yaml', 'daily.yaml', 'jobs.sqlite3', 'config.yaml.bak-old',
                     'daily.yaml.bak-enable', 'config.before-live-20261124.yaml', 'config.yaml.smallco-pending',
                     'manual-once.log', 'deploy-once.log', 'daily.log', 'wk.sh', 'mystery.bak'):
            (home/name).write_text('fixture')
        (home/'logs').mkdir()
        (home/'logs/import-bosshunter.log').write_text('fixture')
        (home/'logs/daily.log.1').write_text('fixture')
        (home/'jobs.sqlite3.collect-int-old.bak').write_text('fixture')
        (home/'runtime').mkdir()
        (home/'runtime/private-file').write_text('fixture')
        return home

    def test_classification_never_reads_file_bodies_or_mutates_home(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            home = self.fixture(Path(tmp))
            before = sorted((p.relative_to(home).as_posix(), p.lstat().st_mtime_ns) for p in home.rglob('*'))
            with patch.object(Path, 'read_text', side_effect=AssertionError('no bodies')), \
                 patch.object(Path, 'read_bytes', side_effect=AssertionError('no bodies')):
                result = self.module.inventory(home)
            rows = {r['path']: r['category'] for r in result['entries']}
            self.assertEqual(rows['config.yaml'], 'in_use')
            self.assertEqual(rows['mystery.bak'], 'in_use')
            self.assertEqual(rows['logs/daily.log.1'], 'in_use')
            self.assertEqual(rows['config.yaml.bak-old'], 'archive')
            self.assertEqual(rows['config.before-live-20261124.yaml'], 'archive')
            self.assertEqual(rows['config.yaml.smallco-pending'], 'archive')
            self.assertEqual(rows['logs/import-bosshunter.log'], 'obsolete')
            self.assertEqual(rows['jobs.sqlite3.collect-int-old.bak'], 'obsolete')
            self.assertNotIn('runtime/private-file', rows)
            self.assertEqual(before, sorted((p.relative_to(home).as_posix(), p.lstat().st_mtime_ns) for p in home.rglob('*')))

    def test_script_defaults_to_dry_run_and_archives_without_deletion(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = self.fixture(root)
            script = root/'archive.py'
            script.write_text(self.module.archive_script(self.module.inventory(home), '20990101'))
            dry = subprocess.run([sys.executable, str(script)], capture_output=True, text=True)
            self.assertEqual(dry.returncode, 0, dry.stderr)
            self.assertTrue((home/'wk.sh').exists())
            self.assertFalse((home/'backups').exists())
            run = subprocess.run([sys.executable, str(script), '--execute'], capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            archive = home/'backups/archive-20990101'
            self.assertEqual((archive/'wk.sh').read_text(), 'fixture')
            self.assertEqual((archive/'logs/import-bosshunter.log').read_text(), 'fixture')
            self.assertFalse((home/'wk.sh').exists())
            self.assertTrue((home/'config.yaml').exists())
            again = subprocess.run([sys.executable, str(script), '--execute'], capture_output=True)
            self.assertNotEqual(again.returncode, 0)
            self.assertEqual((archive/'wk.sh').read_text(), 'fixture')

    def test_changed_candidate_aborts_before_moving_anything(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = self.fixture(root)
            script = root/'archive.py'
            script.write_text(self.module.archive_script(self.module.inventory(home), '20990101'))
            (home/'wk.sh').write_text('changed since inventory')
            run = subprocess.run([sys.executable, str(script), '--execute'], capture_output=True)
            self.assertNotEqual(run.returncode, 0)
            self.assertTrue((home/'manual-once.log').exists())
            self.assertFalse((home/'backups').exists())

    def test_symlinked_paths_are_kept_and_archive_symlink_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = self.fixture(root)
            target = root/'target'
            target.mkdir()
            (home/'dep.sh').symlink_to(target/'private')
            result = self.module.inventory(home)
            row = next(r for r in result['entries'] if r['path'] == 'dep.sh')
            self.assertEqual(row['category'], 'in_use')
            script = root/'archive.py'
            script.write_text(self.module.archive_script(result, '20990101'))
            (home/'backups').symlink_to(target, target_is_directory=True)
            run = subprocess.run([sys.executable, str(script), '--execute'], capture_output=True)
            self.assertNotEqual(run.returncode, 0)
            self.assertTrue((home/'wk.sh').exists())
            self.assertEqual(list(target.iterdir()), [])

    def test_cli_rejects_reports_or_scripts_inside_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = self.fixture(Path(tmp))
            for option in ('--report', '--archive-script'):
                run = subprocess.run([sys.executable, str(ROOT/'scripts/home_inventory.py'), '--home', str(home),
                                      option, str(home/'output')], capture_output=True)
                self.assertNotEqual(run.returncode, 0)
                self.assertFalse((home/'output').exists())
