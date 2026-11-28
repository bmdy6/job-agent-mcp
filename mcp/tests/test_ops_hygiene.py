"""Ops command boundaries and public export run with fixture repositories only."""
import fcntl
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT/'scripts'/filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class OpsTests(unittest.TestCase):
    def test_legacy_deploy_commands_refuse_without_subprocess_side_effects(self):
        for filename in ('install-daily.py', 'install-monitor.py'):
            module = load(filename, filename)
            with patch.object(module.subprocess, 'run') as run, patch.object(sys, 'argv', [filename]):
                with self.assertRaises(SystemExit) as error: module.main()
                self.assertIn('deploy.py', str(error.exception))
                run.assert_not_called()

    def test_wait_for_daily_is_read_only_bounded_and_does_not_kill_service(self):
        deploy = load('ops_deploy', 'deploy.py')
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            deploy.wait_for_daily(home, .1)
            self.assertEqual(list(home.iterdir()), [])
            lock = home/'daily.lock'
            lock.write_bytes(b'keep')
            with lock.open('rb') as held:
                fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaisesRegex(RuntimeError, 'daily'):
                    deploy.wait_for_daily(home, .01)
                with patch.object(deploy.time, 'sleep', side_effect=lambda _: fcntl.flock(held, fcntl.LOCK_UN)):
                    deploy.wait_for_daily(home, 1)
            self.assertEqual(lock.read_bytes(), b'keep')

    def test_public_export_excludes_process_files_and_retains_documents(self):
        # Run the real shell script's export-only path, with local git history and
        # no remotes, publication, or access to the real public checkout.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, dest = root/'source', root/'public'
            source.mkdir()
            for name in ('README.md', 'config.example.yaml', 'result.md', 'result-2026.md', 'result-before-old.md', 'DONE'):
                (source/name).write_text('fixture')
            for folder in ('shots', 'docs/tasks', 'docs/superpowers', 'docs/migrations', 'docs/guide'):
                (source/folder).mkdir(parents=True)
                (source/folder/'sample.md').write_text('fixture')
            def git(*args):
                return subprocess.run(['git', '-C', str(source), *args], check=True, capture_output=True)
            git('init', '-b', 'main')
            git('add', '.')
            git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-m', 'fixture')
            git('update-ref', 'refs/remotes/origin/main', 'HEAD')
            env = {**os.environ, 'JOB_AGENT_PRIVATE_REPO': str(source), 'JOB_AGENT_PUBLIC_REPO': str(dest)}
            run = subprocess.run(['zsh', str(ROOT/'scripts/job-agent-sync-public.sh'), '--export-only'], env=env,
                                 capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual({p.name for p in dest.iterdir()}, {'README.md', 'config.example.yaml', 'docs'})
            self.assertTrue((dest/'docs/guide/sample.md').exists())
            self.assertFalse((dest/'.git').exists())

    def test_root_handoff_files_are_ignored_without_ignoring_source_docs(self):
        for name in ('result.md', 'result-2026.md', 'result-before-old.md', 'DONE', 'shots/test.png'):
            run = subprocess.run(['git', '-C', str(ROOT), 'check-ignore', '--no-index', name], capture_output=True)
            self.assertEqual(run.returncode, 0, name)
        for name in ('README.md', 'docs/settings.md', 'docs/deploy.md', 'config.example.yaml'):
            run = subprocess.run(['git', '-C', str(ROOT), 'check-ignore', '--no-index', name], capture_output=True)
            self.assertEqual(run.returncode, 1, name)
