"""Private SQLite storage. No browser or network dependency."""
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3

STATUSES = frozenset({'pending', 'sent', 'failed', 'unverified', 'skipped'})
SCHEMA_VERSION = 6


def now():
    return datetime.now(timezone.utc).isoformat()


def default_path():
    return Path(os.environ.get('JOB_AGENT_HOME', '~/.job-agent')).expanduser() / 'jobs.sqlite3'


# Migrations are ordered, transactional, and never downgrade a future database.
MIGRATIONS = [(1, [
    '''CREATE TABLE jobs (
        id INTEGER PRIMARY KEY, platform TEXT NOT NULL, platform_job_id TEXT NOT NULL,
        url TEXT NOT NULL DEFAULT '', company TEXT NOT NULL DEFAULT '', title TEXT NOT NULL DEFAULT '',
        city TEXT NOT NULL DEFAULT '', salary TEXT NOT NULL DEFAULT '', jd TEXT NOT NULL DEFAULT '',
        collected_at TEXT NOT NULL, deleted_at TEXT,
        legacy_status TEXT, legacy_score INTEGER DEFAULT 0, legacy_score_reason TEXT DEFAULT '',
        legacy_has_resume INTEGER NOT NULL DEFAULT 0,
        UNIQUE(platform, platform_job_id))''',
    '''CREATE TABLE deliveries (
        id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL REFERENCES jobs(id),
        status TEXT NOT NULL CHECK(status IN ('pending','sent','failed','unverified','skipped')),
        reason_code TEXT NOT NULL DEFAULT '', greeting TEXT NOT NULL DEFAULT '',
        attachment_name TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        source_key TEXT UNIQUE, kind TEXT NOT NULL DEFAULT 'attempt')''',
    '''CREATE TABLE replies (
        id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL REFERENCES jobs(id), message_id TEXT NOT NULL,
        direction TEXT NOT NULL CHECK(direction IN ('inbound','outbound','system')),
        body TEXT NOT NULL, occurred_at TEXT NOT NULL, is_excerpt INTEGER NOT NULL DEFAULT 0,
        id_origin TEXT NOT NULL DEFAULT 'platform', source_key TEXT UNIQUE,
        UNIQUE(job_id, message_id, direction))''',
    '''CREATE TABLE source_history (
        source_key TEXT PRIMARY KEY, job_id INTEGER NOT NULL REFERENCES jobs(id),
        action TEXT NOT NULL, detail TEXT, occurred_at TEXT NOT NULL)''',
    'CREATE INDEX deliveries_job_time ON deliveries(job_id, created_at)',
    'CREATE INDEX replies_job_time ON replies(job_id, occurred_at)',
])]
MIGRATIONS.append((2, [
    "CREATE TABLE store_mode (id INTEGER PRIMARY KEY CHECK(id=1), dry_run INTEGER NOT NULL)",
    "INSERT INTO store_mode VALUES(1,0)",
    """CREATE TABLE batches (
        token TEXT PRIMARY KEY, jobs_json TEXT NOT NULL, greetings_json TEXT NOT NULL,
        attachment_name TEXT NOT NULL, config_hash TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','confirmed','executed')),
        created_at TEXT NOT NULL)""",
    "ALTER TABLE deliveries ADD COLUMN batch_id TEXT REFERENCES batches(token)",
    """CREATE TABLE delivery_queue (
        job_id INTEGER PRIMARY KEY REFERENCES jobs(id),
        status TEXT NOT NULL CHECK(status IN ('pending','sent','failed','unverified','skipped')),
        reason_code TEXT NOT NULL DEFAULT '', attempted_at REAL,
        batch_id TEXT REFERENCES batches(token))""",
    # Imported snapshots describe current source state; events may be older or incomplete.
    """INSERT INTO delivery_queue(job_id,status,reason_code,attempted_at)
        SELECT d.job_id,d.status,d.reason_code,
            CASE WHEN d.status IN ('sent','unverified') THEN CAST(strftime('%s',d.updated_at) AS REAL) END
        FROM deliveries d WHERE d.id=(SELECT x.id FROM deliveries x WHERE x.job_id=d.job_id
            ORDER BY CASE x.kind WHEN 'attempt' THEN 0 WHEN 'legacy_snapshot' THEN 1 ELSE 2 END,
                x.id DESC LIMIT 1)""",
    """CREATE TRIGGER deliveries_no_update BEFORE UPDATE ON deliveries
        BEGIN SELECT RAISE(ABORT,'deliveries are append-only'); END""",
    """CREATE TRIGGER deliveries_no_delete BEFORE DELETE ON deliveries
        BEGIN SELECT RAISE(ABORT,'deliveries are append-only'); END""",
    """CREATE TRIGGER batches_frozen BEFORE UPDATE ON batches
        WHEN NEW.token IS NOT OLD.token OR NEW.jobs_json IS NOT OLD.jobs_json
          OR NEW.greetings_json IS NOT OLD.greetings_json
          OR NEW.attachment_name IS NOT OLD.attachment_name OR NEW.config_hash IS NOT OLD.config_hash
          OR NEW.created_at IS NOT OLD.created_at
          OR NOT (NEW.status=OLD.status OR (OLD.status='pending' AND NEW.status='confirmed')
                  OR (OLD.status='confirmed' AND NEW.status='executed'))
        BEGIN SELECT RAISE(ABORT,'batch is immutable; preview again'); END""",
    """CREATE TRIGGER batches_no_replace BEFORE INSERT ON batches
        WHEN EXISTS(SELECT 1 FROM batches WHERE token=NEW.token)
        BEGIN SELECT RAISE(ABORT,'batch is immutable'); END""",
    """CREATE TRIGGER batches_no_delete BEFORE DELETE ON batches
        BEGIN SELECT RAISE(ABORT,'batch is immutable'); END""",
]))

MIGRATIONS.append((3, [
    "ALTER TABLE replies ADD COLUMN platform TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE replies ADD COLUMN chat_url TEXT NOT NULL DEFAULT ''",
    "UPDATE replies SET platform=(SELECT platform FROM jobs WHERE jobs.id=replies.job_id)",
    "CREATE TABLE replies_legacy_duplicates AS SELECT * FROM replies WHERE id NOT IN (SELECT MIN(id) FROM replies GROUP BY platform,message_id)",
    "DELETE FROM replies WHERE id IN (SELECT id FROM replies_legacy_duplicates)",
    "CREATE UNIQUE INDEX replies_platform_message ON replies(platform,message_id)",
    "CREATE TRIGGER replies_platform_insert BEFORE INSERT ON replies WHEN NEW.platform IS NOT (SELECT platform FROM jobs WHERE id=NEW.job_id) BEGIN SELECT RAISE(ABORT,'reply platform mismatch'); END",
    "CREATE TRIGGER replies_platform_update BEFORE UPDATE ON replies WHEN NEW.platform IS NOT (SELECT platform FROM jobs WHERE id=NEW.job_id) BEGIN SELECT RAISE(ABORT,'reply platform mismatch'); END",
    "ALTER TABLE delivery_queue ADD COLUMN error_detail TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE deliveries ADD COLUMN error_detail TEXT NOT NULL DEFAULT ''",
]))


from .scoring import MIGRATION as SCORING_MIGRATION
MIGRATIONS.append(SCORING_MIGRATION)

MIGRATIONS.append((5, [
    '''CREATE TABLE monitor_baselines (
        platform TEXT PRIMARY KEY, cutoff REAL NOT NULL)''',
    '''CREATE TABLE monitor_conversations (
        platform TEXT NOT NULL, conversation_id TEXT NOT NULL,
        last_message_id TEXT NOT NULL, unread_count INTEGER NOT NULL CHECK(unread_count>=0),
        last_time REAL, PRIMARY KEY(platform,conversation_id))''',
]))


MIGRATIONS.append((6, [
    "ALTER TABLE jobs ADD COLUMN hr_activity TEXT NOT NULL DEFAULT 'unknown' CHECK(hr_activity IN ('now','today','three_days','week','unknown','inactive'))",
    "ALTER TABLE jobs ADD COLUMN hr_activity_raw TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE jobs ADD COLUMN experience TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE jobs ADD COLUMN education TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE replies ADD COLUMN classification TEXT CHECK(classification IN ('interview','resume_request','rejection','question','other'))",
    "ALTER TABLE replies ADD COLUMN confidence REAL CHECK(confidence>=0 AND confidence<=1)",
    "ALTER TABLE replies ADD COLUMN classification_source TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE replies ADD COLUMN classification_model TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE replies ADD COLUMN classified_at TEXT",
    "ALTER TABLE replies ADD COLUMN classification_error TEXT NOT NULL DEFAULT ''",
    """CREATE TABLE interview_invitations (
        reply_id INTEGER PRIMARY KEY REFERENCES replies(id),
        time_text TEXT NOT NULL DEFAULT '', form TEXT NOT NULL DEFAULT 'unknown'
            CHECK(form IN ('online','onsite','phone','unknown')),
        status TEXT NOT NULL DEFAULT 'pending_confirmation' CHECK(status='pending_confirmation'),
        created_at TEXT NOT NULL)""",
]))


class Store:
    """One connection per worker. Single writes autocommit; transaction() groups a batch."""
    def __init__(self, path=None, *, dry_run=False):
        self.path = Path(path or default_path()).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Create privately before SQLite opens; never change an existing user's permissions.
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
        self.connection = sqlite3.connect(self.path, isolation_level=None, timeout=30)
        self.connection.row_factory = sqlite3.Row
        try:
            self.connection.execute('PRAGMA foreign_keys=ON')
            self.connection.execute('PRAGMA recursive_triggers=ON')
            with self.transaction():
                version = self.schema_version
                # Reject mode mismatch before migration can touch an existing live v1 database.
                if version == 1 and dry_run:
                    raise ValueError('Dry-run and live queues must use separate databases')
                if version >= 2 and version <= SCHEMA_VERSION:
                    mode = self.connection.execute('SELECT dry_run FROM store_mode WHERE id=1').fetchone()[0]
                    if mode != int(dry_run):
                        raise ValueError('Dry-run and live queues must use separate databases')
                if version > SCHEMA_VERSION:
                    raise ValueError('Unsupported future store schema')
                self.connection.execute('CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)')
                for target, statements in MIGRATIONS:
                    if target > version:
                        for statement in statements:
                            self.connection.execute(statement)
                        self.connection.execute('INSERT INTO schema_migrations VALUES(?,?)', (target, now()))
                        self.connection.execute(f'PRAGMA user_version={target}')
                if version == 0:
                    self.connection.execute('UPDATE store_mode SET dry_run=? WHERE id=1', (int(dry_run),))
                # Additive extension: preserve v3 compatibility with the independently
                # deployed monitor (#38), which rejects future core schema versions.
                self.connection.execute('CREATE TABLE IF NOT EXISTS schema_extensions(name TEXT PRIMARY KEY, version INTEGER NOT NULL)')
                if not self.connection.execute("SELECT 1 FROM schema_extensions WHERE name='daily' AND version=1").fetchone():
                    columns={r['name'] for r in self.connection.execute('PRAGMA table_info(jobs)')}
                    for field in ('hr_name','hr_title','hr_id'):
                        if field not in columns:
                            self.connection.execute(f"ALTER TABLE jobs ADD COLUMN {field} TEXT NOT NULL DEFAULT ''")
                    self.connection.execute("""CREATE TABLE IF NOT EXISTS delivery_identities (
                        delivery_id INTEGER PRIMARY KEY REFERENCES deliveries(id), platform TEXT NOT NULL,
                        company TEXT NOT NULL, title TEXT NOT NULL, hr_name TEXT NOT NULL DEFAULT '',
                        hr_id TEXT NOT NULL DEFAULT '', attempted_at REAL NOT NULL)""")
                    self.connection.execute("""INSERT OR IGNORE INTO delivery_identities
                        SELECT d.id,j.platform,j.company,j.title,'','',CAST(strftime('%s',d.updated_at) AS REAL)
                        FROM deliveries d JOIN jobs j ON j.id=d.job_id
                        WHERE d.status='sent' AND strftime('%s',d.updated_at) IS NOT NULL""")
                    self.connection.execute("INSERT OR REPLACE INTO schema_extensions VALUES('daily',1)")
                self.connection.execute("""CREATE TABLE IF NOT EXISTS delivery_attempts(
                    job_id INTEGER NOT NULL REFERENCES jobs(id), attempted_at REAL NOT NULL,
                    PRIMARY KEY(job_id,attempted_at))""")
                self.connection.execute("""INSERT OR IGNORE INTO delivery_attempts
                    SELECT job_id,attempted_at FROM delivery_queue WHERE attempted_at IS NOT NULL""")
                self.connection.execute("""CREATE TABLE IF NOT EXISTS platform_pauses(
                    platform TEXT PRIMARY KEY, reason TEXT NOT NULL, paused_at TEXT NOT NULL)""")
                self.dry_run = dry_run
        except BaseException:
            self.close()
            raise

    @property
    def schema_version(self):
        return self.connection.execute('PRAGMA user_version').fetchone()[0]

    @contextmanager
    def transaction(self):
        self.connection.execute('BEGIN IMMEDIATE')
        try:
            yield self
            self.connection.execute('COMMIT')
        except BaseException:
            self.connection.execute('ROLLBACK')
            raise

    def close(self):
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def paused_platforms(self):
        return dict(self.connection.execute('SELECT platform,reason FROM platform_pauses'))

    def monitor_baseline(self, platform):
        row = self.connection.execute('SELECT cutoff FROM monitor_baselines WHERE platform=?', (platform,)).fetchone()
        return row['cutoff'] if row else None

    def monitor_conversations(self, platform):
        return {r['conversation_id']: dict(r) for r in self.connection.execute(
            'SELECT * FROM monitor_conversations WHERE platform=?', (platform,))}

    def save_monitor_conversations(self, platform, cutoff, summaries):
        # Caller groups these acknowledgements with reply ingestion in transaction().
        self.connection.execute('INSERT OR IGNORE INTO monitor_baselines VALUES(?,?)', (platform, cutoff))
        self.connection.executemany('''INSERT INTO monitor_conversations VALUES(?,?,?,?,?)
            ON CONFLICT(platform,conversation_id) DO UPDATE SET
                last_message_id=excluded.last_message_id, unread_count=excluded.unread_count,
                last_time=excluded.last_time''',
            [(platform, s.conversation_id, s.last_message_id, s.unread_count, s.last_time) for s in summaries])

    def pause_platform(self, platform, reason='risk_control'):
        self.connection.execute('INSERT OR REPLACE INTO platform_pauses VALUES(?,?,?)',(platform,reason,now()))

    def resume_platform(self, platform):
        self.connection.execute('DELETE FROM platform_pauses WHERE platform=?',(platform,))

    def upsert_job(self, *, platform, platform_job_id, **fields):
        allowed = {'url','company','title','city','salary','jd','collected_at','deleted_at','hr_name','hr_title','hr_id',
                   'legacy_status','legacy_score','legacy_score_reason','legacy_has_resume',
                   'hr_activity','hr_activity_raw','experience','education'}
        if not platform or not platform_job_id or fields.keys() - allowed:
            raise ValueError('Invalid job identity or fields')
        # Preserve collected_at on an ordinary metadata refresh.
        names = ['platform', 'platform_job_id', *fields]
        values = [platform, platform_job_id, *fields.values()]
        updates = ','.join(f'{key}=excluded.{key}' for key in fields) or 'platform=excluded.platform'
        if 'collected_at' not in fields:
            names.append('collected_at')
            values.append(now())
        row = self.connection.execute(
            f"INSERT INTO jobs({','.join(names)}) VALUES({','.join('?' for _ in names)}) "
            f'ON CONFLICT(platform,platform_job_id) DO UPDATE SET {updates} RETURNING id', values).fetchone()
        return row[0]

    def get_job(self, job_id):
        row = self.connection.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        return dict(row) if row else None

    def list_jobs(self, *, platform=None, include_deleted=False):
        clauses, args = ['1=1'], []
        if not include_deleted:
            clauses.append('deleted_at IS NULL')
        if platform:
            clauses.append('platform=?')
            args.append(platform)
        return [dict(r) for r in self.connection.execute('SELECT * FROM jobs WHERE '+' AND '.join(clauses)+' ORDER BY id', args)]

    def record_delivery(self, job_id, *, status, reason_code='', greeting='', attachment_name='',
                        created_at=None, source_key=None, kind='attempt', batch_id=None, error_detail=''):
        with (nullcontext() if self.connection.in_transaction else self.transaction()):
            if status not in STATUSES:
                raise ValueError('Invalid delivery status')
            stamp = created_at or now()
            row = self.connection.execute('''INSERT INTO deliveries
                (job_id,status,reason_code,greeting,attachment_name,created_at,updated_at,source_key,kind,batch_id,error_detail)
                VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(source_key) DO NOTHING RETURNING id''',
                (job_id,status,reason_code,greeting,attachment_name,stamp,stamp,source_key,kind,batch_id,error_detail)).fetchone()
            if row and status == 'sent':
                job = self.get_job(job_id)
                attempted = self.connection.execute('SELECT attempted_at FROM delivery_queue WHERE job_id=?', (job_id,)).fetchone()
                parsed = datetime.fromisoformat(stamp.replace('Z','+00:00'))
                when = attempted[0] if kind == 'attempt' and attempted and attempted[0] is not None else (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).timestamp()
                self.connection.execute('INSERT INTO delivery_identities VALUES(?,?,?,?,?,?,?)',
                    (row[0],job['platform'],job['company'],job['title'],job['hr_name'],job['hr_id'],when))
            if kind == 'legacy_snapshot':
                self.connection.execute("INSERT OR IGNORE INTO delivery_queue(job_id,status,reason_code,attempted_at) VALUES(?,?,?,CASE WHEN ? IN ('sent','unverified') THEN CAST(strftime('%s',?) AS REAL) END)", (job_id,status,reason_code,status,stamp))
            return row[0] if row else self.connection.execute('SELECT id FROM deliveries WHERE source_key=?',(source_key,)).fetchone()[0]

    def update_delivery(self, delivery_id, *, status, reason_code=''):
        raise ValueError('deliveries are append-only; append a new event instead')

    def get_delivery(self, delivery_id):
        row = self.connection.execute('SELECT * FROM deliveries WHERE id=?',(delivery_id,)).fetchone()
        return dict(row) if row else None

    def list_deliveries(self, job_id=None):
        return [dict(r) for r in self.connection.execute('SELECT * FROM deliveries'+
                (' WHERE job_id=?' if job_id is not None else '')+' ORDER BY id',
                (job_id,) if job_id is not None else ())]

    def add_reply(self, job_id, *, message_id, direction, body, occurred_at=None,
                  is_excerpt=False, id_origin='platform', source_key=None, chat_url=''):
        if not message_id or direction not in {'inbound','outbound','system'}:
            raise ValueError('Invalid message identity or direction')
        platform = self.get_job(job_id)['platform']
        self.connection.execute("""INSERT INTO replies
            (job_id,message_id,direction,body,occurred_at,is_excerpt,id_origin,source_key,platform,chat_url,classification_source)
            VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(platform,message_id) DO NOTHING""",
            (job_id,message_id,direction,body,occurred_at or now(),int(is_excerpt),id_origin,source_key,platform,chat_url,'pending' if direction=='inbound' else ''))
        return self.connection.execute('SELECT id FROM replies WHERE platform=? AND message_id=?',
                                       (platform,message_id)).fetchone()[0]

    def list_replies(self, job_id=None):
        return [dict(r) for r in self.connection.execute('SELECT * FROM replies'+
                (' WHERE job_id=?' if job_id is not None else '')+' ORDER BY id',
                (job_id,) if job_id is not None else ())]

    def counts(self):
        c = self.connection
        result = {t:c.execute(f'SELECT count(*) FROM {t}').fetchone()[0] for t in ('jobs','deliveries','replies')}
        result['history'] = c.execute('SELECT count(*) FROM source_history').fetchone()[0]
        result['jobs_active'] = c.execute('SELECT count(*) FROM jobs WHERE deleted_at IS NULL').fetchone()[0]
        result['jobs_by_platform'] = dict(c.execute('SELECT platform,count(*) FROM jobs GROUP BY platform'))
        result['active_by_platform'] = dict(c.execute('SELECT platform,count(*) FROM jobs WHERE deleted_at IS NULL GROUP BY platform'))
        result['deliveries_by_status'] = dict(c.execute('SELECT status,count(*) FROM deliveries GROUP BY status'))
        result['deliveries_by_platform_status'] = [dict(r) for r in c.execute('''SELECT j.platform,d.status,count(*) AS count
            FROM deliveries d JOIN jobs j ON j.id=d.job_id GROUP BY j.platform,d.status ORDER BY j.platform,d.status''')]
        return result

    def legacy_stats(self):
        return dict(self.connection.execute('SELECT legacy_status,count(*) FROM jobs WHERE deleted_at IS NULL AND legacy_status IS NOT NULL GROUP BY legacy_status'))

    def funnel_stats(self):
        """Compatibility view of imported source state; not native engine analytics."""
        rows = [r for r in self.list_jobs() if r['legacy_status'] is not None]
        sent = {'sent','replied','resume_sent','needs_resume','follow_up_sent'}
        scored = sent | {'scored','ready','approved','rejected'}
        result = dict.fromkeys(('采集总数','初筛通过','AI评分','人工确认','发送','回复','简历已发','待手动发简历','简历生成','跟进','拒绝'), 0)
        for r in rows:
            s = r['legacy_status']
            reason = r['legacy_score_reason'] or ''
            checks = [True, s != 'filtered' or (r['legacy_score'] or 0)>0,
                      s in scored or (s=='filtered' and bool(reason) and not reason.startswith(('预筛不通过:','AI评分失败:','AI 评分失败:','评分失败:'))),
                      s in sent|{'approved'}, s in sent, s in {'replied','resume_sent','needs_resume'},
                      s=='resume_sent', s=='needs_resume', bool(r['legacy_has_resume']), s=='follow_up_sent', s=='rejected']
            for key, passed in zip(result, checks):
                result[key] += int(passed)
        return result
