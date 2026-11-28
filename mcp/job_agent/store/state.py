"""Runtime state owned by Store. JSON is a payload format, never a second store."""
import json
from contextlib import contextmanager, nullcontext
from pathlib import Path
import sqlite3

MIGRATION = (7, [
    '''CREATE TABLE runs(run_id TEXT PRIMARY KEY, kind TEXT NOT NULL,
        started_at TEXT, finished_at TEXT, status TEXT NOT NULL DEFAULT '',
        reason TEXT NOT NULL DEFAULT '', summary TEXT NOT NULL, updated_at TEXT NOT NULL)''',
    'CREATE INDEX runs_kind_time ON runs(kind, updated_at)',
    '''CREATE TABLE notifications(id INTEGER PRIMARY KEY, kind TEXT NOT NULL,
        dedupe_key TEXT NOT NULL UNIQUE, channel TEXT NOT NULL, title TEXT NOT NULL,
        body TEXT NOT NULL, url TEXT, status TEXT NOT NULL DEFAULT 'pending'
            CHECK(status IN ('pending','sent','failed')),
        attempts INTEGER NOT NULL DEFAULT 0, next_retry_at REAL, error TEXT NOT NULL DEFAULT '',
        delivery_id INTEGER REFERENCES deliveries(id), created_at TEXT NOT NULL, sent_at TEXT, receipt TEXT NOT NULL DEFAULT '{}')''',
    'CREATE INDEX notifications_pending ON notifications(channel,status,id)',
    'CREATE TABLE monitor_platform_state(platform TEXT PRIMARY KEY, summary TEXT NOT NULL)',
    'CREATE TABLE monitor_settings(id INTEGER PRIMARY KEY CHECK(id=1), policy TEXT NOT NULL, links TEXT NOT NULL)',
    "INSERT INTO monitor_settings VALUES(1,'null','{}')",
    '''CREATE TABLE events(id INTEGER PRIMARY KEY, occurred_at TEXT NOT NULL,
        summary TEXT NOT NULL, source_key TEXT UNIQUE)''',
    "CREATE TRIGGER events_no_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT,'events are append-only'); END",
    "CREATE TRIGGER events_no_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT,'events are append-only'); END",
    '''CREATE TABLE approvals(token TEXT PRIMARY KEY, payload TEXT NOT NULL,
        expires REAL NOT NULL, used INTEGER NOT NULL DEFAULT 0)''',
    '''CREATE TABLE worker_state(kind TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(run_id))''',
    '''CREATE TABLE worker_requests(id TEXT PRIMARY KEY, request TEXT NOT NULL,
        stop INTEGER NOT NULL DEFAULT 0)''',
    '''CREATE TABLE legacy_imports(name TEXT NOT NULL, digest TEXT NOT NULL,
        imported_at TEXT NOT NULL, PRIMARY KEY(name,digest))''',
])


def encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


class RuntimeState:
    def save_run(self, kind, summary, run_id=None):
        from job_agent.store.database import now
        with (nullcontext() if self.connection.in_transaction else self.transaction()):
            summary = dict(summary)
            if kind == 'monitor' and 'platforms' in summary:
                self.save_monitor_state(summary.pop('platforms'))
                summary['_platforms_ref'] = True
            run_id = run_id or summary.get('run_id') or kind + ':' + str(summary.get('started_at') or 'state')
            self.connection.execute('''INSERT INTO runs VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(run_id) DO UPDATE SET finished_at=excluded.finished_at,
                status=excluded.status,reason=excluded.reason,summary=excluded.summary,updated_at=excluded.updated_at''',
                (run_id, kind, str(summary.get('started_at') or ''), str(summary.get('finished_at') or ''),
                 summary.get('status') or summary.get('state') or '', summary.get('reason_code') or '', encode(summary), now()))
            return run_id

    def save_daily_report(self, summary, notifications=()):
        with (nullcontext() if self.connection.in_transaction else self.transaction()):
            self.save_run('daily', summary)
            queued = {}
            for item in notifications:
                queued[item['channel']] = self.enqueue_notification(**item, kind='daily')
                self.refresh_notification(item['key'], item['title'], item['text'])
            return queued

    def latest_run(self, kind):
        row = self.connection.execute('SELECT summary FROM runs WHERE kind=? ORDER BY updated_at DESC,run_id DESC LIMIT 1', (kind,)).fetchone()
        result = json.loads(row[0]) if row else None
        if result and result.pop('_platforms_ref', False):
            result['platforms'] = {k:v for k,v in self.monitor_state().items() if k!='_links'}
        return result

    def monitor_state(self):
        result = {r[0]: json.loads(r[1]) for r in self.connection.execute('SELECT * FROM monitor_platform_state')}
        links = json.loads(self.connection.execute('SELECT links FROM monitor_settings WHERE id=1').fetchone()[0])
        if links:
            result['_links'] = links
        return result

    def save_monitor_state(self, state):
        # Per-platform upsert deliberately preserves rows owned by other workers.
        for platform, value in state.items():
            if platform == '_links':
                self.connection.execute('UPDATE monitor_settings SET links=? WHERE id=1', (encode(value),))
            else:
                self.connection.execute('INSERT INTO monitor_platform_state VALUES(?,?) ON CONFLICT(platform) DO UPDATE SET summary=excluded.summary', (platform, encode(value)))

    def monitor_policy(self, default=None):
        if default is not None:
            self.connection.execute("UPDATE monitor_settings SET policy=? WHERE id=1 AND policy='null'", (encode(default),))
        return json.loads(self.connection.execute('SELECT policy FROM monitor_settings WHERE id=1').fetchone()[0])

    def append_event(self, record, source_key=None):
        from job_agent.store.database import now
        self.connection.execute('INSERT OR IGNORE INTO events(occurred_at,summary,source_key) VALUES(?,?,?)', (record.get('time') or now(), encode(record), source_key))

    def events(self):
        return [json.loads(r[0]) for r in self.connection.execute('SELECT summary FROM events ORDER BY id')]

    def create_approval(self, token, payload, expires):
        self.connection.execute('INSERT INTO approvals(token,payload,expires) VALUES(?,?,?)', (token,encode(payload),expires))

    def approval(self, token):
        row = self.connection.execute('SELECT payload,used FROM approvals WHERE token=?', (token,)).fetchone()
        return {'payload':json.loads(row[0]),'used':bool(row[1])} if row else None

    def consume_approval(self, token):
        return self.connection.execute('UPDATE approvals SET used=1 WHERE token=? AND used=0', (token,)).rowcount == 1

    def latest_business_progress(self):
        values = []
        for query in ("SELECT MAX(created_at) FROM deliveries",
                      "SELECT MAX(at) FROM operation_rate WHERE scope IN ('delivery','collect')"):
            try:
                values.append(self.connection.execute(query).fetchone()[0])
            except sqlite3.OperationalError:
                pass
        return values

    def delete_worker_request(self, ident):
        self.connection.execute('DELETE FROM worker_requests WHERE id=?', (ident,))

    def worker_state(self, kind):
        row = self.connection.execute('SELECT r.summary FROM worker_state w JOIN runs r ON r.run_id=w.run_id WHERE w.kind=?', (kind,)).fetchone()
        return json.loads(row[0]) if row else None

    def save_worker_state(self, kind, state):
        with (nullcontext() if self.connection.in_transaction else self.transaction()):
            run_id = 'worker:'+kind+':'+str(state.get('id') or 'state')
            self.save_run({'delivery':'deliver','scoring':'score','monitor':'monitor_worker'}[kind], state, run_id=run_id)
            self.connection.execute('INSERT INTO worker_state VALUES(?,?) ON CONFLICT(kind) DO UPDATE SET run_id=excluded.run_id', (kind,run_id))

    def worker_request(self, ident, request=None):
        if request is not None:
            self.connection.execute('INSERT INTO worker_requests(id,request) VALUES(?,?)', (ident, encode(request)))
        row = self.connection.execute('SELECT request FROM worker_requests WHERE id=?', (ident,)).fetchone()
        return json.loads(row[0]) if row else None

    def request_stop(self, ident):
        self.connection.execute("INSERT INTO worker_requests VALUES(?,'{}',1) ON CONFLICT(id) DO UPDATE SET stop=1", (ident,))

    def stop_requested(self, ident):
        row = self.connection.execute('SELECT stop FROM worker_requests WHERE id=?', (ident,)).fetchone()
        return bool(row and row[0])


@contextmanager
def snapshot(home=None, *, store_type=None):
    """Read-only connection. Missing databases return None and are never initialized."""
    from job_agent.store.database import Store, default_path
    path = Path(home)/'jobs.sqlite3' if home is not None else default_path()
    if not path.exists():
        yield None
        return
    store_type = store_type or Store
    store = store_type.__new__(store_type)
    store.path = path.resolve()
    store.connection = sqlite3.connect(store.path.as_uri()+'?mode=ro', uri=True, timeout=30)
    store.connection.row_factory = sqlite3.Row
    try:
        store.connection.execute('BEGIN')
        yield store
    finally:
        store.close()


def read_run(kind, home=None):
    with snapshot(home) as store:
        return store.latest_run(kind) if store and store.schema_version >= 7 else None
