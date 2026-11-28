#!/usr/bin/env python3
"""Deploy a committed runtime for both agents; inventory never removes snapshots.

Quiesce MCP/CLI writers before deployment and do not pull/restart MCP until done.
Use --inventory for a read-only cleanup proposal. See docs/deploy.md.
"""
import argparse
from contextlib import ExitStack, closing, contextmanager
from datetime import datetime, timezone
import fcntl
import io
import json
import math
import os
from pathlib import Path
import plistlib
import re
import sqlite3
import subprocess
import tarfile
import tempfile
import time
import uuid

DAILY = 'com.job-agent.daily'
MONITOR = 'com.job-agent.monitor'
LEGACY = 'com.bmdy6.job-agent-watch'
LABELS = (DAILY, MONITOR)
REPO = Path(__file__).resolve().parents[1]
AGENTS = Path.home()/'Library/LaunchAgents'


def atomic(path, content):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix='.'+path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def read_json(path, default=None):
    return json.loads(path.read_text()) if path.exists() else default


def save_json(path, data):
    atomic(path, (json.dumps(data, indent=2, ensure_ascii=False)+'\n').encode())


def schema_version(path):
    if not path.exists():
        return None
    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True)) as db:
        return db.execute('PRAGMA user_version').fetchone()[0]


def backup_database(source, destination):
    if not source.exists():
        return
    atomic(destination, b'')
    with closing(sqlite3.connect(source.resolve().as_uri()+'?mode=ro', uri=True)) as src, closing(sqlite3.connect(destination)) as dst:
        src.backup(dst)
        if dst.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise RuntimeError('SQLite backup failed quick_check')


# v7 consolidates these into jobs.sqlite3. Retain originals for a v6 rollback.
LEGACY_STATE_FILES = ('confirmations.sqlite3', 'notify-outbox.jsonl', 'notified.jsonl',
    'activity.jsonl', 'monitor-state.json', 'monitor-runtime.json', 'monitor-policy.json',
    'heartbeat-state.json', 'daily-last-run.json', 'ego-startup-failure.json')


def snapshot_legacy_state(home, backup):
    paths = [home/name for name in LEGACY_STATE_FILES] + sorted((home/'own-tasks').glob('*.json'))
    saved = []
    for path in paths:
        if path.exists():
            if path.is_symlink() or not path.is_file():
                raise ValueError('Legacy state must be a regular file')
            name = str(path.relative_to(home))
            target = backup/'legacy-state'/name
            if path.suffix == '.sqlite3':
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                backup_database(path, target)
            else:
                atomic(target, path.read_bytes())
            saved.append(name)
    save_json(backup/'legacy-state-files.json', saved)


def restore_legacy_state(home, backup):
    for name in read_json(backup/'legacy-state-files.json', []):
        if name not in LEGACY_STATE_FILES and not (name.startswith('own-tasks/') and Path(name).name == name[len('own-tasks/'):]):
            raise ValueError('Unsafe legacy state backup path')
        path = home/name
        source = backup/'legacy-state'/name
        if path.suffix == '.sqlite3':
            restore_database(source, path)
        else:
            atomic(path, source.read_bytes())


def restore_database(source, destination):
    # All writers must be stopped. Never replay a stale WAL onto the restored DB.
    staging = destination.with_name('.jobs-restore.sqlite3')
    if source.exists():
        backup_database(source, staging)
    for suffix in ('-wal', '-shm'):
        Path(str(destination)+suffix).unlink(missing_ok=True)
    if source.exists():
        os.replace(staging, destination)
    else:
        destination.unlink(missing_ok=True)


@contextmanager
def exclusive(path):
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError(f'Busy: {path.name}; stop writers and retry') from None
        yield fd
    finally:
        os.close(fd)


def platform_ok(status):
    """A platform poll is healthy when it read cleanly or was deliberately paused."""
    return status == 'ok' or (isinstance(status, str) and status.endswith('_paused'))


def monitor_runtime(home):
    path = Path(home)/'jobs.sqlite3'
    if path.exists():
        with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True)) as db:
            if db.execute('PRAGMA user_version').fetchone()[0] >= 7:
                row = db.execute("SELECT summary FROM runs WHERE kind='monitor' ORDER BY updated_at DESC,run_id DESC LIMIT 1").fetchone()
                result = json.loads(row[0]) if row else {}
                if result.pop('_platforms_ref', False):
                    result['platforms'] = {r[0]:json.loads(r[1]) for r in db.execute('SELECT * FROM monitor_platform_state')}
                return result
    return read_json(Path(home)/'monitor-runtime.json', {})


def baseline_ok_platforms(home):
    """Platforms healthy in the last poll before deploy; None when there is no record."""
    state = monitor_runtime(home)
    platforms = state.get('result', {}).get('platforms')
    if not isinstance(platforms, dict) or not platforms:
        return None
    return sorted(p for p, row in platforms.items() if platform_ok((row or {}).get('status')))


def poll_healthy(state, baseline):
    """A new runtime must keep every previously healthy platform healthy.

    Platforms that were already failing before the deploy (for example a site
    whose page changed) do not block it; without a baseline every failure does.
    """
    result = state.get('result', {})
    if result.get('busy'):
        return False
    if baseline is None:
        return not result.get('failed')
    rows = result.get('platforms', {})
    return all(platform_ok((rows.get(p) or {}).get('status')) for p in baseline)


class Launchd:
    def __init__(self, executable='/bin/launchctl'):
        self.baseline = None
        self.executable = executable
        self.domain = f'gui/{os.getuid()}'

    def command(self, *args):
        return subprocess.run([self.executable, *args], text=True, capture_output=True, timeout=20)

    def inspect(self, label):
        return self.command('print', self.domain+'/'+label)

    def loaded_service(self, label):
        result = self.inspect(label)
        if result.returncode not in (0, 113):
            # launchctl uses 113 for a missing service. Other failures are not absence.
            raise RuntimeError(f'Cannot inspect service {label}: exit {result.returncode}')
        return result.returncode == 0

    def stop(self, label):
        current = self.inspect(label)
        if current.returncode == 113:
            return
        if current.returncode:
            raise RuntimeError(f'Cannot inspect service {label}')
        match = re.search(r'^\s*pid = (\d+)\s*$', current.stdout, re.M)
        if self.command('bootout', self.domain+'/'+label).returncode:
            raise RuntimeError(f'Cannot stop {label}')
        if match:
            deadline = time.monotonic()+45
            while True:
                try:
                    os.kill(int(match[1]), 0)
                except ProcessLookupError:
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError(f'{label} process has not exited')
                time.sleep(.2)

    def start(self, path):
        for attempt in range(15):
            result = self.command('bootstrap', self.domain, str(path))
            if result.returncode == 0:
                return
            if result.returncode != 5 or self.loaded_service(path.stem) or attempt == 14:
                raise RuntimeError(f'Bootstrap failed: {path.stem}')
            time.sleep(1)

    def healthy(self, home, since, timeout):
        deadline = time.monotonic()+timeout
        while time.monotonic() < deadline:
            current = self.inspect(getattr(self,'monitor',MONITOR))
            match = re.search(r'^\s*pid = (\d+)\s*$', current.stdout, re.M)
            state = monitor_runtime(home)
            if current.returncode == 0 and match and state.get('pid') == int(match[1]):
                if state.get('started_at', 0) >= since and state.get('last_poll_at', 0) >= since:
                    if state.get('state') == 'running' and poll_healthy(state, self.baseline):
                        return state
            time.sleep(.25)
        raise RuntimeError('Monitor did not complete a fresh healthy poll; see monitor.log')


class Deployer:
    def __init__(self, repo, home, agents, launchd=None, *, health_timeout=600):
        self.repo = Path(repo).resolve()
        self.home = Path(home).expanduser().resolve()
        self.agents = Path(agents).expanduser().resolve()
        if launchd is None and (self.home != (Path.home()/'.job-agent').resolve() or self.agents != AGENTS.resolve()):
            raise ValueError('Custom paths require an injected launchd controller; never target real service labels in a rehearsal')
        self.launchd = launchd or Launchd()
        self.health_timeout = health_timeout
        self.runtimes = self.home/'runtime'
        self.current = self.runtimes/'current'
        self.state_path = self.home/'deployment-state.json'
        self.backups = self.home/'deployments'
        self.configure_labels()

    def configure_labels(self):
        import yaml
        raw=yaml.safe_load((self.home/'config.yaml').read_text()) if (self.home/'config.yaml').exists() else {}
        prefix=(raw or {}).get('deploy',{}).get('label_prefix','com.job-agent')
        if not isinstance(prefix,str) or not re.fullmatch(r'[A-Za-z0-9]+(?:[.-][A-Za-z0-9]+)*',prefix):
            raise ValueError('Invalid deploy.label_prefix')
        self.monitor=prefix+('.job-agent-monitor' if prefix!='com.job-agent' else '.monitor')
        self.labels=(DAILY,self.monitor)
        # Never silently orphan/rename an existing monitor for this data home.
        for path in self.agents.glob('*.plist'):
            if not path.stem.endswith(('.job-agent-monitor','.monitor')):
                continue
            data=plistlib.loads(path.read_bytes())
            configured=data.get('EnvironmentVariables',{}).get('JOB_AGENT_HOME')
            if configured and Path(configured).expanduser().resolve()==self.home and ('monitor' in path.stem):
                if path.stem!=self.monitor:
                    raise ValueError('Existing monitor label differs; preserve deploy.label_prefix before deployment')
        if isinstance(self.launchd,Launchd):self.launchd.monitor=self.monitor

    def resolve(self, revision):
        return subprocess.check_output(['git', '-C', str(self.repo), 'rev-parse', '--verify', revision+'^{commit}'], text=True).strip()

    def python(self, runtime, code, *, home=None):
        env = {k: v for k, v in os.environ.items() if k not in {'PYTHONPATH', 'PYTHONHOME'}}
        env['JOB_AGENT_HOME'] = str(home or self.home)
        # Never import the developer's cwd in place of the installed package.
        result = subprocess.run([str(runtime/'.venv/bin/python'), '-c', code], cwd=runtime,
                                env=env, text=True, capture_output=True)
        if result.returncode:
            # Config validation exceptions can include tokens in the input repr.
            raise RuntimeError(f'Installed runtime check failed (exit {result.returncode}); validate configuration privately')
        return result.stdout

    def build(self, sha):
        runtime = self.runtimes/sha[:12]
        manifest = runtime/'deployment.json'
        if runtime.exists():
            data = read_json(manifest, {})
            if data.get('commit') != sha or not data.get('ready') or not (runtime/'.venv/bin/python').exists():
                raise ValueError(f'Incomplete or mismatched runtime: {runtime}; inspect it manually')
            return runtime
        runtime.mkdir(parents=True, mode=0o700)
        archive = subprocess.check_output(['git', '-C', str(self.repo), 'archive', sha])
        with tarfile.open(fileobj=io.BytesIO(archive)) as stream:
            # Repository runtimes need only regular files/directories. Validate
            # members explicitly so extraction also works on supported Python 3.10.
            members = stream.getmembers()
            for member in members:
                destination = (runtime / member.name).resolve()
                if not destination.is_relative_to(runtime) or not (member.isfile() or member.isdir()):
                    raise ValueError('Unsafe runtime archive member')
            stream.extractall(runtime, members=members, **({'filter': 'data'} if hasattr(tarfile, 'data_filter') else {}))
        if not (runtime/'mcp/uv.lock').is_file():
            raise ValueError('Committed mcp/uv.lock is required for deployment')
        env = {**os.environ, 'UV_PROJECT_ENVIRONMENT': str(runtime/'.venv')}
        subprocess.run(['uv', 'sync', '--project', str(runtime/'mcp'), '--frozen', '--no-editable'],
                       check=True, env=env)
        version = int(self.python(runtime, 'from job_agent.store import SCHEMA_VERSION; print(SCHEMA_VERSION)'))
        save_json(manifest, dict(commit=sha, schema_version=version, ready=True,
                                 built_at=datetime.now(timezone.utc).isoformat()))
        return runtime

    def smoke(self, runtime):
        settings = json.loads(self.python(runtime, '''import json
import math
from job_agent.config import load_config
from job_agent.monitor.config import load_settings
config = load_config()
load_settings()
print(json.dumps(dict(hour=config.daily.hour, minute=config.daily.minute)))
'''))
        # This exercises the installed store and real poller with no browser/network/secrets.
        with tempfile.TemporaryDirectory(prefix='job-agent-smoke-') as directory:
            self.python(runtime, '''from job_agent.browser.base import FakeBrowser
from job_agent.config import Settings
from job_agent.store import Store
from job_agent.monitor.poller import Monitor
with Store() as store:
    result = Monitor(FakeBrowser(), [], store, Settings()).poll_once()
    assert result['failed'] == 0 and result['sent'] == 0
''', home=Path(directory))
        return settings

    def migrate(self, runtime):
        self.python(runtime, '''from job_agent.store import Store, SCHEMA_VERSION
from contextlib import nullcontext
if SCHEMA_VERSION >= 7:
    from job_agent.store.database import deployment_migration
with deployment_migration() if SCHEMA_VERSION >= 7 else nullcontext():
 with Store(**({'migrate': True} if SCHEMA_VERSION >= 7 else {})) as store:
    assert store.schema_version == SCHEMA_VERSION
    assert store.connection.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
''')

    def validate(self):
        self.configure_labels()
        if self.launchd.loaded_service(LEGACY):
            raise ValueError('Legacy watch is loaded; retire it explicitly before unified deployment')
        if self.current.exists() and not self.current.is_symlink():
            raise ValueError('runtime/current must be a symlink')
        if self.current.is_symlink() and (not self.current.exists() or self.current.resolve().parent != self.runtimes):
            raise ValueError('runtime/current must point to a complete local runtime')
        for label in (*self.labels, LEGACY):
            path = self.agents/(label+'.plist')
            if path.exists():
                data = plistlib.loads(path.read_bytes())
                configured = data.get('EnvironmentVariables', {}).get('JOB_AGENT_HOME')
                if not configured or Path(configured).expanduser().resolve() != self.home:
                    raise ValueError(f'{label} JOB_AGENT_HOME differs from deployment target')
                if data.get('Label') != label:
                    raise ValueError(f'Incorrect Label in {path}')
                if label == DAILY and data.get('KeepAlive'):
                    raise ValueError('Daily KeepAlive is unsupported; use bounded periodic resume')
                if label == LEGACY:
                    raise ValueError('Legacy watch is installed; retire it explicitly before unified deployment')
        if (self.home/'jobs.sqlite3').is_symlink():
            raise ValueError('Database must not be a symlink')

    def state(self):
        return read_json(self.state_path, {'history': []})

    def snapshot(self, kind):
        backup = self.backups/(datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')+'-'+uuid.uuid4().hex[:8])
        backup.mkdir(parents=True, mode=0o700)
        loaded = {label: self.launchd.loaded_service(label) for label in self.labels}
        if any(loaded[label] and not (self.agents/(label+'.plist')).exists() for label in self.labels):
            raise ValueError('Loaded service has no recoverable plist; export it before deploying')
        before = dict(kind=kind, loaded=loaded, current=os.readlink(self.current) if self.current.is_symlink() else None,
                      home=str(self.home), agents=str(self.agents), history=self.state()['history'],
                      plists={}, schema_version=schema_version(self.home/'jobs.sqlite3'))
        for label in self.labels:
            source = self.agents/(label+'.plist')
            before['plists'][label] = source.exists()
            if source.exists():
                atomic(backup/source.name, source.read_bytes())
        for name in ('config.yaml', 'daily.yaml'):
            if (self.home/name).exists():
                atomic(backup/name, (self.home/name).read_bytes())
        save_json(backup/'before.json', before)
        return backup, before

    def stop(self):
        self.launchd.stop(self.monitor)
        self.launchd.stop(DAILY)  # disarm calendar while schema changes; never kickstart daily

    def switch(self, target):
        self.runtimes.mkdir(parents=True, exist_ok=True, mode=0o700)
        if target is None:
            self.current.unlink(missing_ok=True)
            return
        staging = self.runtimes/('.current-'+uuid.uuid4().hex)
        try:
            staging.symlink_to(target)
            os.replace(staging, self.current)
        finally:
            staging.unlink(missing_ok=True)

    def install_plists(self, settings):
        for label, launcher in ((DAILY, 'daily'), (self.monitor, 'monitor')):
            path = self.agents/(label+'.plist')
            if path.exists():
                data = plistlib.loads(path.read_bytes())
            elif label == self.monitor:
                data = plistlib.loads((self.current/'examples/job-agent-monitor.plist').read_bytes())
            else:
                data = dict(Label=label, StartCalendarInterval={'Hour': settings['hour'], 'Minute': settings['minute']}, ProcessType='Background')
            data['Label'] = label
            extras = data.get('ProgramArguments', [])[2:] if label == DAILY else []
            data['ProgramArguments'] = [str(self.current/'.venv/bin/python'), str(self.current/f'scripts/{launcher}-launch.py'), *extras]
            data['WorkingDirectory'] = str(self.current)
            data.setdefault('EnvironmentVariables', {})['JOB_AGENT_HOME'] = str(self.home)
            for key in ('StandardOutPath', 'StandardErrorPath'):
                data[key] = str(self.home/f'{launcher}.log')
            if label == DAILY:
                # Preserve safety caps/calendar/environment; daily.lock guards schema changes.
                for key in ('RunAtLoad', 'KeepAlive', 'StartInterval', 'QueueDirectories', 'WatchPaths', 'StartOnMount', 'Sockets'):
                    data.pop(key, None)
                data.update(RunAtLoad=True, StartInterval=1800)
                data.setdefault('StartCalendarInterval', {'Hour': settings['hour'], 'Minute': settings['minute']})
            else:
                data.update(RunAtLoad=True, KeepAlive={'SuccessfulExit': False}, ThrottleInterval=60)
            atomic(path, plistlib.dumps(data))
            fd = os.open(self.home/f'{launcher}.log', os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
            os.close(fd)

    def restore(self, backup, before, *, database):
        if database:
            restore_database(backup/'jobs.sqlite3', self.home/'jobs.sqlite3')
            restore_legacy_state(self.home, backup)
        self.switch(before['current'])
        for label in self.labels:
            path = self.agents/(label+'.plist')
            if before['plists'][label]:
                atomic(path, (backup/path.name).read_bytes())
            else:
                path.unlink(missing_ok=True)
        save_json(self.state_path, {'history': before['history']})
        self.release_monitor()
        for label in self.labels:
            if before['loaded'][label]:
                self.launchd.start(self.agents/(label+'.plist'))

    def release_monitor(self):
        if getattr(self, 'monitor_fd', None) is not None:
            fcntl.flock(self.monitor_fd, fcntl.LOCK_UN)

    def check_writers(self):
        # Independent workers are not stopped by the daily/monitor service locks.
        path = self.home/'jobs.sqlite3'
        if schema_version(path) is not None and schema_version(path) >= 7:
            with closing(sqlite3.connect(path.as_uri()+'?mode=ro', uri=True)) as db:
                states = [json.loads(r[0]) for r in db.execute('SELECT r.summary FROM worker_state w JOIN runs r ON r.run_id=w.run_id')]
        else:
            states = [read_json(p, {}) for p in (self.home/'own-tasks').glob('*.json')]
        if any(state.get('status') in {'running','stopping'} for state in states):
            raise ValueError('Quiesce own-tasks writers before deployment')

    @contextmanager
    def transaction(self, kind, prepare=lambda: None):
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        with exclusive(self.home/'deploy.lock'):
            self.validate()
            self.check_writers()
            prepare()
            backup, before = self.snapshot(kind)
            stopped = False
            database_ready = False
            locks = ExitStack()
            try:
                self.stop()
                stopped = True
                locks.enter_context(exclusive(self.home/'daily.lock'))
                locks.enter_context(exclusive(self.home/'jobs.sqlite3.collect.lock'))
                self.monitor_fd = locks.enter_context(exclusive(self.home/'monitor-run.lock'))
                backup_database(self.home/'jobs.sqlite3', backup/'jobs.sqlite3')
                snapshot_legacy_state(self.home, backup)
                database_ready = True
                yield backup, before
            except BaseException:
                # If stop/lock acquisition fails, do not touch the database or claim recovery.
                if stopped and database_ready:
                    self.stop()
                    safety = backup/'failed-state'
                    safety.mkdir(mode=0o700)
                    backup_database(self.home/'jobs.sqlite3', safety/'jobs.sqlite3')
                    self.restore(backup, before, database=True)
                    save_json(backup/'outcome.json', {'status': 'failed_restored', 'safety_backup': str(safety)})
                else:
                    # Restore only the services actually unloaded; avoid bootstrap collisions.
                    for label in self.labels:
                        if before['loaded'][label] and not self.launchd.loaded_service(label):
                            self.launchd.start(self.agents/(label+'.plist'))
                raise
            finally:
                self.monitor_fd = None
                locks.close()

    def deploy(self, revision='origin/main'):
        sha = self.resolve(revision)
        with self.transaction('deploy') as (backup, before):
            if hasattr(self.launchd, 'baseline'):
                self.launchd.baseline = baseline_ok_platforms(self.home)
            runtime = self.build(sha)
            settings = self.smoke(runtime)
            self.migrate(runtime)
            self.switch(runtime.name)
            self.install_plists(settings)
            since = time.time()
            self.release_monitor()
            self.launchd.start(self.agents/(DAILY+'.plist'))
            self.launchd.start(self.agents/(self.monitor+'.plist'))
            health = self.launchd.healthy(self.home, since, self.health_timeout)
            result = dict(status='deployed', commit=sha, runtime=str(runtime), backup=str(backup), health=health)
            save_json(backup/'outcome.json', result)
            save_json(self.state_path, {'history': [*before['history'], backup.name]})
        return result

    def rollback(self, *, restore_database=False):
        selected = {}
        def prepare():
            history = self.state()['history']
            if not history:
                raise ValueError('No deployment to roll back')
            target = self.backups/history[-1]
            before = read_json(target/'before.json')
            if before['home'] != str(self.home) or before['agents'] != str(self.agents):
                raise ValueError('Rollback paths differ from original deployment')
            incompatible = schema_version(self.home/'jobs.sqlite3') != before['schema_version']
            if incompatible and not restore_database:
                raise ValueError('Schema differs: --restore-database required; newer writes are archived before restore')
            if before['current'] and not (self.runtimes/before['current']).is_dir():
                raise ValueError('Rollback runtime is missing; no services were stopped')
            for label in self.labels:
                if before['plists'][label] and not (target/(label+'.plist')).is_file():
                    raise ValueError('Rollback plist backup is missing')
            if (incompatible or restore_database) and before['schema_version'] is not None and not (target/'jobs.sqlite3').is_file():
                raise ValueError('Rollback database backup is missing')
            selected.update(target=target, before=before, incompatible=incompatible)
        with self.transaction('rollback', prepare) as (safety, _):
            target, before, incompatible = selected['target'], selected['before'], selected['incompatible']
            since = time.time()
            self.restore(target, before, database=incompatible or restore_database)
            result = dict(status='rolled_back', restored=str(target), safety_backup=str(safety),
                          database_restored=incompatible or restore_database)
            if before['loaded'][self.monitor]:
                result['health'] = self.launchd.healthy(self.home, since, self.health_timeout)
            save_json(safety/'outcome.json', result)
        return result

    def inventory(self):
        paths = sorted(p for p in self.runtimes.iterdir() if p.is_dir() and not p.is_symlink()) if self.runtimes.exists() else []
        references = {}
        # Include every plist, even nonstandard services which might reference a snapshot.
        for path in sorted(self.agents.glob('*.plist')):
            try:
                data = plistlib.loads(path.read_bytes())
            except (ValueError, plistlib.InvalidFileException):
                raise ValueError(f'Cannot safely inventory invalid plist: {path.name}') from None
            references[path.stem] = data
        latest = []
        for ident in reversed(self.state()['history']):
            data = read_json(self.backups/ident/'outcome.json', {})
            name = Path(data.get('runtime', '')).name
            if name and name not in latest:
                latest.append(name)
        recent = set(latest[:3])
        # Existing daily/monitor snapshots predate deployment-state.json.
        for path in sorted(paths, key=lambda p: p.stat().st_mtime, reverse=True):
            if len(recent) >= 3:
                break
            recent.add(path.name)
        rollback_refs = []
        for ident in self.state()['history'][-2:]:
            backup = self.backups/ident
            before = read_json(backup/'before.json', {})
            if before.get('current'):
                rollback_refs.append(self.runtimes/before['current'])
            for label in self.labels:
                saved = backup/(label+'.plist')
                if saved.exists():
                    data = plistlib.loads(saved.read_bytes())
                    rollback_refs.extend(Path(v) for v in data.get('ProgramArguments', []) if v.startswith('/'))
        rows = []
        for path in paths:
            data = read_json(path/'deployment.json', {})
            sha = data.get('commit')
            identity = 'manifest' if sha else 'unknown'
            if not sha:
                match = re.search(r'(?:^|-)([0-9a-f]{7,40})$', path.name)
                if match:
                    try:
                        sha = self.resolve(match[1])
                        identity = 'directory-name (contents unverified)'
                    except subprocess.CalledProcessError:
                        pass
            used = []
            for label, plist in references.items():
                strings = [*plist.get('ProgramArguments', []), plist.get('WorkingDirectory', '')]
                for value in strings:
                    if not isinstance(value, str) or not value.startswith('/'):
                        continue
                    candidate = Path(value)
                    if candidate.is_relative_to(path) or candidate.resolve().is_relative_to(path.resolve()):
                        used.append(label)
                        break
            is_current = self.current.is_symlink() and self.current.resolve() == path
            rollback_needed = any(p.is_relative_to(path) or p.resolve().is_relative_to(path.resolve()) for p in rollback_refs)
            reason = 'referenced' if used or is_current else 'recent-three' if path.name in recent else 'rollback' if rollback_needed else 'unknown-identity' if not sha else None
            rows.append(dict(directory=path.name, commit=sha, identity=identity, plist_references=used,
                             current=is_current, retention='keep' if reason else 'candidate', reason=reason))
        return rows


def wait_for_daily(home, timeout):
    """Wait for the daily flock without creating files or signalling services.

    This replaces night-deploy.sh's open-ended launchctl polling. The deployment
    transaction still acquires its own locks after this advisory wait.
    """
    if timeout < 0 or not math.isfinite(timeout):
        raise ValueError('Daily wait timeout must be nonnegative')
    deadline = time.monotonic() + timeout
    path = Path(home)/'daily.lock'
    while True:
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                pass
        finally:
            os.close(fd)
        remaining = deadline-time.monotonic()
        if remaining <= 0:
            raise RuntimeError('Timed out waiting for daily; no services were changed')
        time.sleep(min(.5, remaining))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--revision', default='origin/main', help='Resolved locally; fetch origin before deploying')
    parser.add_argument('--home', type=Path, default=Path(os.environ.get('JOB_AGENT_HOME', '~/.job-agent')).expanduser())
    parser.add_argument('--agents-dir', type=Path, default=AGENTS)
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--rollback', action='store_true')
    action.add_argument('--inventory', action='store_true')
    parser.add_argument('--restore-database', action='store_true', help='Rollback only: restore pre-deploy DB; newer writes are saved in safety_backup')
    parser.add_argument('--health-timeout', type=float, default=600)
    parser.add_argument('--wait-idle', type=float, default=0, metavar='SECONDS', help='Bounded wait for daily to finish; never kills or kickstarts it')
    args = parser.parse_args()
    if args.restore_database and not args.rollback:
        parser.error('--restore-database requires --rollback')
    if args.wait_idle < 0 or not math.isfinite(args.wait_idle):
        parser.error('--wait-idle must be finite and nonnegative')
    if args.inventory and args.wait_idle:
        parser.error('--wait-idle is not used with --inventory')
    if args.wait_idle:
        wait_for_daily(args.home, args.wait_idle)
    # Inventory is always read-only, with no launchctl calls or path restrictions.
    app = Deployer(REPO, args.home, args.agents_dir, launchd=Launchd() if args.inventory else None,
                   health_timeout=args.health_timeout)
    result = app.inventory() if args.inventory else app.rollback(restore_database=args.restore_database) if args.rollback else app.deploy(args.revision)
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()
