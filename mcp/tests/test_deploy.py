"""Deployment transactions use isolated homes and injected launchd, never user services."""
from contextlib import closing
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('deploy', ROOT/'scripts/deploy.py')
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)


class FakeLaunchd:
    def __init__(self):
        self.loaded = set(deploy.LABELS)
        self.events = []
        self.fail = None

    def loaded_service(self, label):
        return label in self.loaded

    def stop(self, label):
        self.events.append(('stop', label))
        self.loaded.discard(label)

    def start(self, path):
        label = path.stem
        self.events.append(('start', label))
        if self.fail == label:
            self.fail = None
            raise RuntimeError('injected bootstrap failure')
        self.loaded.add(label)

    def healthy(self, home, since, timeout):
        self.events.append(('healthy', deploy.MONITOR))
        return {'last_poll_at': since + 1, 'state': 'running', 'result': {'failed': 0}}


class DeployTests(unittest.TestCase):
    def test_health_timeout_default_and_cli_override(self):
        with tempfile.TemporaryDirectory() as home:
            app = deploy.Deployer(ROOT, home, home+'/agents', FakeLaunchd())
            self.assertEqual(app.health_timeout, 600)
            self.assertEqual(deploy.Deployer(ROOT, home, home+'/agents', FakeLaunchd(), health_timeout=900).health_timeout, 900)
        for args, expected in ((['--inventory'], 600), (['--inventory', '--health-timeout', '900'], 900)):
            with patch('sys.argv', ['deploy.py', *args]), patch.object(deploy, 'Deployer') as factory, \
                    patch('builtins.print'):
                factory.return_value.inventory.return_value = []
                deploy.main()
                self.assertEqual(factory.call_args.kwargs['health_timeout'], expected)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)/'home'
        self.home.mkdir()
        self.agents = Path(self.tmp.name)/'agents'
        self.agents.mkdir()
        self.config = b'daily:\n  enabled: false\n'
        (self.home/'config.yaml').write_bytes(self.config)
        self.old = {}
        for label, launcher in ((deploy.DAILY, 'daily'), (deploy.MONITOR, 'monitor')):
            self.old[label] = dict(Label=label, ProgramArguments=['/old/python', f'/old/{launcher}-launch.py'],
                EnvironmentVariables={'JOB_AGENT_HOME': str(self.home), 'CUSTOM': 'keep'},
                StartCalendarInterval={'Hour': 9, 'Minute': 12})
            if label == deploy.DAILY:
                self.old[label]['ProgramArguments'] += ['--max-per-platform', '1']
            (self.agents/(label+'.plist')).write_bytes(plistlib.dumps(self.old[label]))
        self.launchd = FakeLaunchd()
        self.app = deploy.Deployer(ROOT, self.home, self.agents, self.launchd)
        with closing(sqlite3.connect(self.home/'jobs.sqlite3')) as db, db:
            db.execute('PRAGMA user_version=3')
            db.execute('CREATE TABLE evidence(value TEXT)')
            db.execute("INSERT INTO evidence VALUES('before')")
        self.schema = 4

    def build(self, sha):
        self.launchd.events.append(('build', sha))
        runtime = self.home/'runtime'/sha[:12]
        runtime.mkdir(parents=True, exist_ok=True)
        (runtime/'deployment.json').write_text(json.dumps({'commit': sha, 'schema_version': self.schema}))
        return runtime

    def migrate(self, runtime):
        self.launchd.events.append(('migrate', runtime.name))
        with closing(sqlite3.connect(self.home/'jobs.sqlite3')) as db, db:
            db.execute(f'PRAGMA user_version={self.schema}')

    def perform(self, sha='a'*40):
        with patch.object(self.app, 'resolve', return_value=sha), \
             patch.object(self.app, 'build', side_effect=self.build), \
             patch.object(self.app, 'smoke', return_value={'hour': 8, 'minute': 0}), \
             patch.object(self.app, 'migrate', side_effect=self.migrate):
            return self.app.deploy('HEAD')

    def test_order_shared_current_and_no_immediate_daily(self):
        receipt = self.perform()
        self.assertEqual(self.launchd.events[:2], [('stop', deploy.MONITOR), ('stop', deploy.DAILY)])
        self.assertEqual(self.launchd.events[2][0], 'build')
        self.assertLess([e[0] for e in self.launchd.events].index('migrate'), [e[0] for e in self.launchd.events].index('start'))
        self.assertEqual((self.home/'runtime/current').resolve().name, 'a'*12)
        for label in deploy.LABELS:
            data = plistlib.loads((self.agents/(label+'.plist')).read_bytes())
            self.assertIn('/runtime/current/', data['ProgramArguments'][0])
            self.assertEqual(data['EnvironmentVariables']['CUSTOM'], 'keep')
            if label == deploy.DAILY:
                self.assertNotIn('RunAtLoad', data)
                self.assertNotIn('KeepAlive', data)
                self.assertEqual(data['StartCalendarInterval'], {'Hour': 9, 'Minute': 12})
                self.assertEqual(data['ProgramArguments'][2:], ['--max-per-platform', '1'])
        self.assertEqual((self.home/'config.yaml').read_bytes(), self.config)
        self.assertEqual(deploy.schema_version(Path(receipt['backup'])/'jobs.sqlite3'), 3)
        self.assertEqual(deploy.schema_version(self.home/'jobs.sqlite3'), 4)

    def test_failed_start_restores_database_plists_and_loaded_state(self):
        self.launchd.fail = deploy.MONITOR
        with self.assertRaisesRegex(RuntimeError, 'bootstrap'):
            self.perform()
        self.assertEqual(deploy.schema_version(self.home/'jobs.sqlite3'), 3)
        self.assertFalse((self.home/'runtime/current').exists())
        for label in deploy.LABELS:
            self.assertEqual(plistlib.loads((self.agents/(label+'.plist')).read_bytes()), self.old[label])
        self.assertEqual(self.launchd.loaded, set(deploy.LABELS))

    def test_cross_schema_rollback_requires_opt_in_and_preserves_new_data(self):
        self.perform()
        with closing(sqlite3.connect(self.home/'jobs.sqlite3')) as db, db:
            db.execute("INSERT INTO evidence VALUES('after')")
        events = list(self.launchd.events)
        with self.assertRaisesRegex(ValueError, 'restore-database'):
            self.app.rollback()
        self.assertEqual(events, self.launchd.events)
        result = self.app.rollback(restore_database=True)
        self.assertEqual(deploy.schema_version(self.home/'jobs.sqlite3'), 3)
        with closing(sqlite3.connect(Path(result['safety_backup'])/'jobs.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT value FROM evidence').fetchall(), [('before',), ('after',)])
        self.assertFalse((self.home/'runtime/current').exists())

    def test_same_schema_rollback_keeps_post_deploy_writes_and_redeploy(self):
        self.perform()
        self.perform('b'*40)
        with closing(sqlite3.connect(self.home/'jobs.sqlite3')) as db, db:
            db.execute("INSERT INTO evidence VALUES('keep')")
        self.app.rollback()
        self.assertEqual((self.home/'runtime/current').resolve().name, 'a'*12)
        with closing(sqlite3.connect(self.home/'jobs.sqlite3')) as db, db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM evidence').fetchone()[0], 2)
        self.perform('c'*40)
        self.app.rollback()
        self.assertEqual((self.home/'runtime/current').resolve().name, 'a'*12)

    def test_build_failure_does_not_rewrite_or_migrate_database(self):
        with patch.object(self.app, 'resolve', return_value='a'*40), \
             patch.object(self.app, 'build', side_effect=RuntimeError('build failed')):
            with self.assertRaisesRegex(RuntimeError, 'build failed'):
                self.app.deploy('HEAD')
        self.assertEqual(deploy.schema_version(self.home/'jobs.sqlite3'), 3)
        self.assertEqual(self.launchd.loaded, set(deploy.LABELS))

    def test_migration_failure_restores_and_does_not_start_new_runtime(self):
        def fail(runtime):
            self.migrate(runtime)
            raise RuntimeError('migration failed')
        with patch.object(self.app, 'migrate', side_effect=fail):
            # perform patches migrate itself; exercise deployment directly here.
            with patch.object(self.app, 'resolve', return_value='a'*40), \
                 patch.object(self.app, 'build', side_effect=self.build), \
                 patch.object(self.app, 'smoke', return_value={'hour': 8, 'minute': 0}):
                with self.assertRaisesRegex(RuntimeError, 'migration failed'):
                    self.app.deploy('HEAD')
        self.assertEqual(deploy.schema_version(self.home/'jobs.sqlite3'), 3)
        self.assertFalse(self.app.current.exists())

    def test_failed_health_poll_restores_predeploy_state(self):
        with patch.object(self.launchd, 'healthy', side_effect=RuntimeError('unhealthy poll')):
            with self.assertRaisesRegex(RuntimeError, 'unhealthy poll'):
                self.perform()
        self.assertEqual(deploy.schema_version(self.home/'jobs.sqlite3'), 3)
        self.assertEqual(self.launchd.loaded, set(deploy.LABELS))

    def test_rollback_bootstrap_failure_recovers_newer_version_and_data(self):
        self.perform()
        self.perform('b'*40)
        self.launchd.fail = deploy.MONITOR
        with self.assertRaisesRegex(RuntimeError, 'bootstrap'):
            self.app.rollback()
        self.assertEqual(self.app.current.resolve().name, 'b'*12)
        self.assertEqual(deploy.schema_version(self.home/'jobs.sqlite3'), 4)
        self.assertEqual(len(self.app.state()['history']), 2)

    def test_busy_daily_refuses_migration_and_restores_unloaded_services(self):
        with deploy.exclusive(self.home/'daily.lock'):
            with self.assertRaisesRegex(RuntimeError, 'Busy: daily.lock'):
                self.perform()
        self.assertEqual(deploy.schema_version(self.home/'jobs.sqlite3'), 3)
        self.assertFalse(self.app.current.exists())
        self.assertEqual(self.launchd.loaded, set(deploy.LABELS))

    def test_plist_failure_after_partial_switch_restores_both(self):
        original = self.app.install_plists
        def fail(settings):
            original(settings)
            raise RuntimeError('plist failed')
        with patch.object(self.app, 'install_plists', side_effect=fail):
            with self.assertRaisesRegex(RuntimeError, 'plist failed'):
                self.perform()
        for label in deploy.LABELS:
            self.assertEqual(plistlib.loads((self.agents/(label+'.plist')).read_bytes()), self.old[label])
        self.assertFalse(self.app.current.exists())

    def test_recovery_holds_daily_and_collection_locks_until_restore_finishes(self):
        original = self.app.restore
        def guarded_restore(*args, **kwargs):
            for name in ('daily.lock', 'jobs.sqlite3.collect.lock'):
                with self.assertRaisesRegex(RuntimeError, 'Busy:'):
                    with deploy.exclusive(self.home/name):
                        pass
            return original(*args, **kwargs)
        self.launchd.fail = deploy.MONITOR
        with patch.object(self.app, 'restore', side_effect=guarded_restore):
            with self.assertRaisesRegex(RuntimeError, 'bootstrap'):
                self.perform()
        self.assertEqual(deploy.schema_version(self.home/'jobs.sqlite3'), 3)

    def test_loaded_legacy_watch_refuses_before_stop(self):
        self.launchd.loaded.add(deploy.LEGACY)
        with self.assertRaisesRegex(ValueError, 'Legacy watch'):
            self.perform()
        self.assertEqual(self.launchd.events, [])

    def test_loaded_without_plist_refuses_before_stop(self):
        (self.agents/(deploy.MONITOR+'.plist')).unlink()
        with self.assertRaisesRegex(ValueError, 'recoverable plist'):
            self.perform()
        self.assertEqual(self.launchd.events, [])

    def test_custom_paths_cannot_implicitly_target_user_launchd(self):
        with self.assertRaisesRegex(ValueError, 'injected'):
            deploy.Deployer(ROOT, self.home, self.agents)

    def test_reject_wrong_home_before_stopping_services(self):
        data = self.old[deploy.MONITOR]
        data['EnvironmentVariables']['JOB_AGENT_HOME'] = '/different/home'
        (self.agents/(deploy.MONITOR+'.plist')).write_bytes(plistlib.dumps(data))
        with self.assertRaisesRegex(ValueError, 'JOB_AGENT_HOME'):
            self.perform()
        self.assertEqual(self.launchd.events, [])

    def test_read_only_inventory_keeps_referenced_and_last_three(self):
        for char in 'abcd':
            self.perform(char*40)
        by_name = {r['directory']: r for r in self.app.inventory()}
        self.assertEqual(by_name['a'*12]['retention'], 'candidate')
        for char in 'bcd':
            self.assertEqual(by_name[char*12]['retention'], 'keep')
        self.assertEqual(set(by_name['d'*12]['plist_references']), set(deploy.LABELS))
        self.assertTrue((self.home/'runtime'/('a'*12)).is_dir())


class DeploymentStatusTests(unittest.TestCase):
    def test_status_reads_commit_without_opening_database(self):
        from service.deployment import status
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, JOB_AGENT_HOME=tmp):
            self.assertIsNone(status()['commit'])
            runtime = Path(tmp)/'runtime/abc'
            runtime.mkdir(parents=True)
            (runtime/'deployment.json').write_text(json.dumps({'commit': 'a'*40, 'schema_version': 4}))
            (runtime.parent/'current').symlink_to(runtime.name)
            self.assertEqual(status()['commit'], 'a'*40)
            self.assertFalse((Path(tmp)/'jobs.sqlite3').exists())


class LocalLaunchd(FakeLaunchd):
    """Persistent real monitor subprocess, fake browser, no launchctl/vault/network."""
    def __init__(self):
        super().__init__()
        self.loaded.clear()
        self.process = None
        self.log = None

    def start(self, path):
        import subprocess
        super().start(path)
        if path.stem != deploy.MONITOR:
            return
        data = plistlib.loads(path.read_bytes())
        runtime = Path(data['ProgramArguments'][1]).parents[1]
        env = {k:v for k,v in os.environ.items() if k not in {'PYTHONPATH', 'PYTHONHOME'}}
        env['JOB_AGENT_HOME'] = data['EnvironmentVariables']['JOB_AGENT_HOME']
        self.log = open(Path(env['JOB_AGENT_HOME'])/'rehearsal-monitor.log', 'ab')
        self.process = subprocess.Popen([data['ProgramArguments'][0], '-c', '''
from browser.base import FakeBrowser
import monitor.runtime as runtime
runtime.create_browser = lambda config: FakeBrowser()
runtime.create_sources = lambda browser, settings: []
raise SystemExit(runtime.run())
'''], cwd=runtime, env=env, stdout=self.log, stderr=self.log)

    def stop(self, label):
        if label == deploy.MONITOR and self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
            self.process.wait(timeout=15)
            self.process = None
            self.log.close()
            self.log = None
        super().stop(label)

    def healthy(self, home, since, timeout):
        import time
        deadline = time.monotonic()+timeout
        while time.monotonic() < deadline:
            if self.process is None or self.process.poll() is not None:
                raise RuntimeError('Rehearsal monitor exited; inspect rehearsal-monitor.log')
            state = deploy.read_json(home/'monitor-runtime.json', {})
            if state.get('pid') == self.process.pid and state.get('started_at', 0) >= since and state.get('last_poll_at', 0) >= since:
                if state['state'] == 'running' and not state['result']['failed']:
                    self.events.append(('healthy', deploy.MONITOR))
                    return state
            time.sleep(.1)
        raise RuntimeError('Rehearsal monitor poll timed out')


@unittest.skipUnless(os.environ.get('JOB_AGENT_DEPLOY_REHEARSE') == '1', 'opt-in real archive/venv rehearsal')
class RehearsalTests(unittest.TestCase):
    def test_archive_build_v5_v6_switch_restart_and_rollback(self):
        import subprocess
        import time
        directory = os.environ.get('JOB_AGENT_REHEARSAL_DIR')
        if directory:
            root = Path(directory).resolve()
            root.mkdir(parents=True, exist_ok=False)
        else:
            temp = tempfile.TemporaryDirectory(prefix='job-agent-deploy-')
            self.addCleanup(temp.cleanup)
            root = Path(temp.name)
        home, agents = root/'home', root/'LaunchAgents'
        home.mkdir()
        agents.mkdir()
        (home/'config.yaml').write_text('engine:\n  backend: own\nnotify:\n  enabled: false\ndaily:\n  enabled: false\n')
        config = (home/'config.yaml').read_bytes()
        controller = LocalLaunchd()
        self.addCleanup(controller.stop, deploy.MONITOR)
        app = deploy.Deployer(ROOT, home, agents, controller, health_timeout=15)
        # Production base is schema v5; exercise the actual new migration/rollback.
        old = app.deploy('b60a78d')
        self.assertEqual(deploy.schema_version(home/'jobs.sqlite3'), 5)
        first_pid = old['health']['pid']
        new = app.deploy(os.environ.get('JOB_AGENT_DEPLOY_REVISION', 'HEAD'))
        self.assertEqual(deploy.schema_version(home/'jobs.sqlite3'), 6)
        self.assertNotEqual(new['health']['pid'], first_pid)
        self.assertEqual(new['health']['deployment']['executing_commit'], new['commit'])
        self.assertGreater(new['health']['last_poll_at'], old['health']['last_poll_at'])
        for label in deploy.LABELS:
            data = plistlib.loads((agents/(label+'.plist')).read_bytes())
            self.assertIn('/runtime/current/', data['ProgramArguments'][0])
        with self.assertRaisesRegex(ValueError, 'restore-database'):
            app.rollback()
        back = app.rollback(restore_database=True)
        self.assertEqual(deploy.schema_version(home/'jobs.sqlite3'), 5)
        self.assertEqual((home/'runtime/current').resolve(), Path(old['runtime']))
        self.assertGreater(back['health']['last_poll_at'], new['health']['last_poll_at'])
        self.assertNotEqual(back['health']['pid'], new['health']['pid'])
        self.assertEqual((home/'config.yaml').read_bytes(), config)
        # Redeployment reuses the complete immutable runtime, then rollback again.
        again = app.deploy(os.environ.get('JOB_AGENT_DEPLOY_REVISION', 'HEAD'))
        app.rollback(restore_database=True)
        deploy.save_json(root/'receipt.json', dict(old=old, new=new, rollback=back, redeploy=again,
                                                   events=controller.events, inventory=app.inventory()))
        print('rehearsal_receipt='+str(root/'receipt.json'))


class DatabaseBackupTests(unittest.TestCase):
    def test_backup_includes_uncheckpointed_wal_and_restore_discards_stale_sidecars(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, backup = Path(tmp)/'jobs.sqlite3', Path(tmp)/'backup.sqlite3'
            with closing(sqlite3.connect(path)) as db:
                db.execute('PRAGMA journal_mode=WAL')
                db.execute('CREATE TABLE evidence(value TEXT)')
                db.execute("INSERT INTO evidence VALUES('in WAL')")
                db.commit()
                deploy.backup_database(path, backup)
            with closing(sqlite3.connect(backup)) as db:
                self.assertEqual(db.execute('SELECT value FROM evidence').fetchall(), [('in WAL',)])
            for suffix in ('-wal', '-shm'):
                Path(str(path)+suffix).write_bytes(b'stale')
            deploy.restore_database(backup, path)
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute('SELECT value FROM evidence').fetchall(), [('in WAL',)])
            self.assertEqual(backup.stat().st_mode & 0o777, 0o600)


class LaunchdTests(unittest.TestCase):
    def test_bootstrap_retry_and_wait_for_process_exit(self):
        import subprocess
        controller = deploy.Launchd()
        success = subprocess.CompletedProcess([], 0, 'pid = 123\n', '')
        absent = subprocess.CompletedProcess([], 113, '', '')
        retry = subprocess.CompletedProcess([], 5, '', '')
        with patch.object(controller, 'command', side_effect=[success, success]), \
             patch.object(deploy.os, 'kill', side_effect=[None, ProcessLookupError]), \
             patch.object(deploy.time, 'sleep'):
            controller.stop(deploy.MONITOR)
        with patch.object(controller, 'command', side_effect=[retry, absent, success]) as command, \
             patch.object(deploy.time, 'sleep'):
            controller.start(Path('/tmp')/(deploy.MONITOR+'.plist'))
        self.assertEqual([c.args[0] for c in command.call_args_list], ['bootstrap', 'print', 'bootstrap'])

    def test_health_requires_fresh_poll_same_live_pid(self):
        import subprocess
        controller = deploy.Launchd()
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            state = {'pid': 123, 'started_at': 200, 'last_poll_at': 100,
                     'state': 'running', 'result': {'failed': 0}}
            deploy.save_json(home/'monitor-runtime.json', state)
            with patch.object(controller, 'inspect', return_value=subprocess.CompletedProcess([], 0, 'pid = 123\n', '')), \
                 patch.object(deploy.time, 'sleep', side_effect=lambda _: deploy.save_json(home/'monitor-runtime.json', dict(state, last_poll_at=201))):
                self.assertEqual(controller.healthy(home, 200, 1)['last_poll_at'], 201)
            with patch.object(controller, 'inspect', return_value=subprocess.CompletedProcess([], 0, 'pid = 999\n', '')):
                with self.assertRaisesRegex(RuntimeError, 'fresh healthy poll'):
                    controller.healthy(home, 200, .01)


class MCPDeploymentTests(unittest.IsolatedAsyncioTestCase):
    async def test_setup_and_status_expose_current_and_executing_identity(self):
        import server
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, JOB_AGENT_HOME=tmp):
            home = Path(tmp)
            (home/'config.yaml').write_text('engine: {backend: own}\n')
            runtime = home/'runtime/example'
            runtime.mkdir(parents=True)
            deploy.save_json(runtime/'deployment.json', {'commit':'a'*40, 'schema_version':4})
            (runtime.parent/'current').symlink_to(runtime.name)
            with patch('service.read_tools.setup_checks', return_value={}), \
                 patch('service.read_tools.status', return_value={}), \
                 patch('service.monitor.status', return_value={}):
                for name in ('setup_check', 'status'):
                    result = (await server.mcp.call_tool(name, {}))[1]
                    self.assertEqual(result['deployment']['commit'], 'a'*40)
                    # MCP still executes from this worktree; it must not claim new runtime code.
                    self.assertIsNone(result['deployment']['executing_commit'])
