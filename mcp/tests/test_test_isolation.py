"""Exercise safety in children so no test ever opens the real production store."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from job_agent.config import job_agent_home

ROOT = Path(__file__).resolve().parents[2]


class TestIsolationTest(unittest.TestCase):
    def test_package_bootstrap_redirects_missing_production_and_symlink_homes(self):
        with tempfile.TemporaryDirectory() as directory:
            link = Path(directory)/'alias'
            link.symlink_to(Path.home()/'.job-agent', target_is_directory=True)
            for value in (None, str(Path.home()/'.job-agent'), str(link)):
                env = {**os.environ, 'PYTHONPATH': str(ROOT/'mcp')}
                env.pop('JOB_AGENT_HOME', None)
                if value is not None:
                    env['JOB_AGENT_HOME'] = value
                child = subprocess.run([sys.executable, '-c',
                    'import tests; from job_agent.config import job_agent_home; '
                    'import json,os; print(json.dumps([str(job_agent_home()),os.environ["JOB_AGENT_TESTING"]]))'],
                    cwd=ROOT/'mcp', env=env, capture_output=True, text=True, timeout=30)
                self.assertEqual(child.returncode, 0, child.stderr)
                path, marker = json.loads(child.stdout)
                self.assertNotEqual(Path(path).resolve(), (Path.home()/'.job-agent').resolve())
                self.assertEqual(marker, '1')

    def test_single_integration_module_without_home_is_safe(self):
        env = {**os.environ, 'PYTHONPATH': str(ROOT/'mcp')}
        env.pop('JOB_AGENT_HOME', None)
        env.pop('JOB_AGENT_TESTING', None)
        child = subprocess.run([sys.executable, '-m', 'unittest', 'tests.test_integration_service'],
                               cwd=ROOT/'mcp', env=env, capture_output=True, text=True, timeout=40)
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertIn('OK', child.stderr)

    def test_config_refuses_production_after_test_changes_environment(self):
        with patch.dict(os.environ, JOB_AGENT_TESTING='1', JOB_AGENT_HOME=str(Path.home()/'.job-agent')):
            with self.assertRaisesRegex(RuntimeError, 'production'):
                job_agent_home()

    def test_store_cannot_bypass_config_with_explicit_production_path(self):
        from job_agent.store import Store
        # Fake production entirely inside a temporary directory, safe even on failure.
        with tempfile.TemporaryDirectory() as directory, patch('pathlib.Path.home', return_value=Path(directory)):
            with patch.dict(os.environ, JOB_AGENT_TESTING='1'):
                with self.assertRaisesRegex(RuntimeError, 'production'):
                    Store(Path(directory)/'.job-agent'/'jobs.sqlite3', migrate=True)
            self.assertFalse((Path(directory)/'.job-agent').exists())

    def test_production_upgrade_requires_deployment_context(self):
        from job_agent.store.database import authorize_schema_upgrade, deployment_migration
        with tempfile.TemporaryDirectory() as directory, patch('pathlib.Path.home', return_value=Path(directory)):
            path = Path(directory)/'.job-agent'/'jobs.sqlite3'
            with self.assertRaisesRegex(RuntimeError, 'deploy.py'):
                authorize_schema_upgrade(path)
            with deployment_migration():
                authorize_schema_upgrade(path)
            with self.assertRaisesRegex(RuntimeError, 'deploy.py'):
                authorize_schema_upgrade(path)

    def test_real_store_upgrade_guard_runs_before_any_mutation(self):
        from job_agent.store import Store
        from test_state_round2 import seed_v6
        with tempfile.TemporaryDirectory() as directory, patch('pathlib.Path.home', return_value=Path(directory)):
            home = Path(directory)/'.job-agent'
            home.mkdir()
            path = home/'jobs.sqlite3'
            seed_v6(path)
            original = path.read_bytes()
            # Exercise the ordinary-process guard separately from the test guard;
            # this fake production path is always inside TemporaryDirectory.
            with patch('job_agent.config.reject_test_production_path'):
                with self.assertRaisesRegex(RuntimeError,'deploy.py'):
                    Store(path,migrate=True)
            self.assertEqual(path.read_bytes(),original)
            self.assertEqual({p.name for p in home.iterdir()},{'jobs.sqlite3'})
