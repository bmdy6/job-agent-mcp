"""Engine queue and immutable previews on the owned job database."""
from contextlib import contextmanager
from dataclasses import asdict
import fcntl
import hashlib
import json
import os
import secrets
import time
from job_agent.engine.models import Job, Record, Status, Reason
from job_agent.platforms.spec import spec_for
from job_agent.store.database import Store, now
from job_agent.store.daily import DailyQueue


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def config_hash(config):
    return hashlib.sha256(canonical(config).encode()).hexdigest()


class SQLiteDeliveryStore(DailyQueue, Store):
    """One database per mode. A nonblocking flock covers the entire engine run.

    Default dry-run requires an explicit path, so it cannot migrate the live default.
    Live execution requires an immutable confirmed batch.
    """
    def __init__(self, path=None, *, dry_run=True, migrate=False):
        if dry_run and path is None:
            raise ValueError('Dry-run requires a separate explicit database path')
        super().__init__(path, dry_run=dry_run, migrate=migrate)
        self._notification_channels = []
        self._notification_renderer = None
        self.result_notification_hook = None
        self._run_config = None
        self._run_greetings = {}
        self._resume_selections = {}
        self._run_job_greetings = {}

    def configure_notifications(self, channels, renderer=None):
        self._notification_channels = list(channels)
        self._notification_renderer = renderer

    @contextmanager
    def exclusive(self):
        fd = os.open(str(self.path) + '.lock', os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError('Delivery queue already running') from exc
            yield
        finally:
            os.close(fd)

    def add(self, job):
        if not self.dry_run:
            raise ValueError('Live jobs must be enqueued by confirming a batch')
        with self.transaction():
            job_id = self.upsert_job(platform=job.platform, platform_job_id=job.job_id, url=job.url)
            return self.connection.execute('INSERT OR IGNORE INTO delivery_queue(job_id,status) VALUES(?,?)',
                                           (job_id, 'pending')).rowcount == 1

    def prepare_run(self, config, *, batch_token=None, greetings=None):
        # The engine already froze and validated its settings before acquiring the run lock.
        snapshot = json.loads(canonical(config))
        greetings = dict(greetings) if greetings is not None else {
            p: s.get('greeting', '') for p, s in snapshot.get('platforms', {}).items()}
        for row in self.connection.execute("SELECT DISTINCT batch_id FROM delivery_queue WHERE batch_id IS NOT NULL AND status='pending' AND (? IS NULL OR batch_id=?)", (batch_token,batch_token)):
            batch = self.get_batch(row[0])
            if batch['status'] != 'confirmed' or batch['config_hash'] != config_hash(snapshot):
                raise ValueError('Batch configuration changed; preview again')
            for job in self.batch_jobs(row[0]):
                platform = snapshot.get('platforms', {}).get(job.platform, {})
                key = job.platform + ':' + job.job_id
                if key in greetings and greetings[key] != batch['greetings'].get(key):
                    raise ValueError('Execution job greeting differs from preview')
                if (greetings.get(job.platform) != batch['greetings'][job.platform]
                        or (platform.get('attachment_name') and platform.get('attachment_name') != batch['attachment_name'])):
                    raise ValueError('Execution content differs from preview; preview again')
        self._run_job_greetings = {}
        if batch_token:
            batch = self.get_batch(batch_token)
            for job in self.batch_jobs(batch_token):
                key = job.platform + ':' + job.job_id
                text = batch['greetings'].get(key, batch['greetings'][job.platform])
                if not isinstance(text, str):
                    raise ValueError('Invalid per-job greeting snapshot')
                self._run_job_greetings[(job.platform, job.job_id)] = text
        self._run_config = snapshot
        self._run_greetings = greetings
        self._resume_selections = {}

    def execution_greeting(self, job):
        return self._run_job_greetings.get((job.platform, job.job_id), self._run_greetings[job.platform])

    def record_resume_selection(self,job,selection):
        # Use existing append-only delivery evidence; no core schema migration is needed.
        self._resume_selections[(job.platform,job.job_id)]=json.loads(canonical(selection))
        self.update(job,Status.UNVERIFIED,Reason.INTERRUPTED)

    def records(self):
        rows = self.connection.execute("""SELECT j.platform,j.platform_job_id,j.url,q.*
            FROM delivery_queue q JOIN jobs j ON j.id=q.job_id
            WHERE j.deleted_at IS NULL AND j.url<>'' ORDER BY j.id""")
        result = []
        for r in rows:
            job = Job(r['platform'], r['platform_job_id'], r['url'])
            if r['batch_id']:
                # Collection may refresh jobs.url after preview; execution uses the approved URL.
                job = next(j for j in self.batch_jobs(r['batch_id'])
                           if (j.platform,j.job_id)==(job.platform,job.job_id))
            reason = Reason(r['reason_code']) if r['reason_code'] in Reason._value2member_map_ else Reason.PRECHECK_ERROR
            result.append(Record(job, Status(r['status']), reason, r['attempted_at'], r['error_detail']))
        return result

    def update(self, job, status, reason, attempted_at=None, *, error_detail=''):
        with self.transaction():
            row = self.connection.execute('''SELECT q.* FROM delivery_queue q JOIN jobs j ON j.id=q.job_id
                WHERE j.platform=? AND j.platform_job_id=?''', (job.platform, job.job_id)).fetchone()
            if row is None:
                raise KeyError((job.platform, job.job_id))
            greeting = attachment = ''
            if row['batch_id']:
                batch = self.get_batch(row['batch_id'])
                if batch['status'] != 'confirmed':
                    raise ValueError('Batch must be confirmed')
                greeting = batch['greetings'].get(job.platform + ':' + job.job_id, batch['greetings'][job.platform])
                attachment = (self._run_config or {}).get('platforms', {}).get(job.platform, {}).get('attachment_name', batch['attachment_name'])
            elif not self.dry_run:
                raise ValueError('Live update requires a confirmed batch')
            elif self._run_config:
                platform_config = self._run_config['platforms'][job.platform]
                greeting = self._run_greetings[job.platform]
                attachment = platform_config['attachment_name']
            if row['batch_id'] and self.get_batch(row['batch_id'])['greetings'].get('_kind') == 'followup':
                attachment = ''
            selection=self._resume_selections.get((job.platform,job.job_id))
            if selection is not None:
                selected=selection['selected']
                attachment=selected['name'] if selected['attachment']==1 and selected['name'] else ''
                error_detail=canonical({'resume_selection':selection,'detail':error_detail})
            self.connection.execute('''UPDATE delivery_queue SET status=?,reason_code=?,
                attempted_at=COALESCE(?,attempted_at),error_detail=? WHERE job_id=?''',
                (status.value, reason.value, attempted_at, error_detail, row['job_id']))
            kind = 'followup' if row['batch_id'] and self.get_batch(row['batch_id'])['greetings'].get('_kind') == 'followup' else 'attempt'
            if attempted_at is not None and kind == 'followup':
                self.append_event({'job_id':row['job_id'],'at':attempted_at}, source_key='followup-attempt:'+str(row['job_id']))
            if attempted_at is not None:
                self.connection.execute('INSERT OR IGNORE INTO delivery_attempts VALUES(?,?)',(row['job_id'],attempted_at))
            self.record_delivery(row['job_id'], status=status.value, reason_code=reason.value,
                                 greeting=greeting, attachment_name=attachment, batch_id=row['batch_id'], error_detail=error_detail, kind=kind,
                                 notifications=[dict(key='delivery-summary:'+row['batch_id']+':'+channel,
                                    channel=channel, title='', text='') for channel in self._notification_channels]
                                 if row['batch_id'] and status != Status.PENDING and reason != Reason.INTERRUPTED else [])
            if self._notification_channels and self._notification_renderer and row['batch_id']:
                try:
                    title, body = self._notification_renderer(row['batch_id'])
                except Exception as error:
                    # A formatting failure must not erase a confirmed external effect.
                    title, body = 'job-agent 投递结果', '投递结果已入库，汇总暂不可用，请查看投递状态。'
                    self.append_event({'tool':'notification_render','result':{'status':'error','error_type':type(error).__name__}})
                for channel in self._notification_channels:
                    self.refresh_notification('delivery-summary:'+row['batch_id']+':'+channel, title, body)
            if self.result_notification_hook and status != Status.PENDING and reason != Reason.INTERRUPTED:
                self.result_notification_hook()

    def history_blocked(self, job_id):
        marker=self.connection.execute("SELECT MAX(id) FROM deliveries WHERE job_id=? AND kind='readonly_retry'",(job_id,)).fetchone()[0] or 0
        return bool(self.connection.execute("SELECT 1 FROM deliveries WHERE job_id=? AND (kind='reconcile' OR status='sent' OR (status IN ('unverified','skipped') AND id>?)) LIMIT 1",(job_id,marker)).fetchone())

    def requeue_verified_not_applied(self, job_id, evidence):
        """Explicit operator action after fresh original-site audit. History stays append-only."""
        import time
        with self.exclusive(), self.transaction():
            row=self.connection.execute('SELECT q.*,j.platform,j.platform_job_id FROM delivery_queue q JOIN jobs j ON j.id=q.job_id WHERE j.id=?',(job_id,)).fetchone()
            latest=self.connection.execute('SELECT MAX(id) FROM deliveries WHERE job_id=?',(job_id,)).fetchone()[0]
            resolved=self.connection.execute("SELECT MAX(id) FROM deliveries WHERE job_id=? AND kind='readonly_retry'",(job_id,)).fetchone()[0] or 0
            unknown=self.connection.execute("SELECT MAX(id) FROM deliveries WHERE job_id=? AND status='unverified'",(job_id,)).fetchone()[0] or 0
            pre_effect_skip=bool(row and row['status']=='skipped' and row['reason_code']=='cooldown' and resolved>unknown)
            attempted=row['attempted_at'] if row else None
            if attempted is None:
                attempted=self.connection.execute('SELECT MAX(attempted_at) FROM delivery_attempts WHERE job_id=?',(job_id,)).fetchone()[0]
            if (not row or not spec_for(row['platform']).supports_readonly_requeue or (row['status']!='unverified' and not pre_effect_skip)
                    or evidence.get('status')!='not_applied' or evidence.get('job_id')!=job_id
                    or evidence.get('platform_job_id')!=row['platform_job_id']
                    or evidence.get('attempted_at')!=attempted
                    or evidence.get('latest_delivery_id')!=latest
                    or type(evidence.get('checked_epoch')) not in (float,int)
                    or not 0<=time.time()-evidence['checked_epoch']<=300):
                raise ValueError('Fresh exact original-site not-applied evidence required')
            if self.connection.execute("SELECT 1 FROM deliveries WHERE job_id=? AND (status='sent' OR (status='skipped' AND reason_code<>'cooldown'))",(job_id,)).fetchone():
                raise ValueError('Confirmed history cannot be retried')
            self.connection.execute("INSERT INTO deliveries(job_id,status,reason_code,created_at,updated_at,kind,error_detail) VALUES(?,'pending','readonly_not_applied',?,?, 'readonly_retry',?)",(job_id,now(),now(),canonical(evidence)))
            self.connection.execute("UPDATE delivery_queue SET status='pending',reason_code='',attempted_at=NULL,batch_id=NULL,error_detail='' WHERE job_id=?",(job_id,))

    def create_batch(self, jobs, greetings, attachment_name, config):
        """Snapshot engine Jobs and per-platform full greetings; any edit needs a new token."""
        jobs = list(jobs)
        if not jobs or len({(j.platform,j.job_id) for j in jobs}) != len(jobs):
            raise ValueError('Batch must contain unique jobs')
        if any(not isinstance(greetings.get(j.platform), str) for j in jobs):
            raise ValueError('Each platform requires its full greeting')
        if not isinstance(attachment_name, str):
            raise ValueError('Attachment name must be text')
        token = secrets.token_urlsafe(32)
        self.connection.execute('''INSERT INTO batches
            (token,jobs_json,greetings_json,attachment_name,config_hash,created_at) VALUES(?,?,?,?,?,?)''',
            (token, canonical([asdict(j) for j in jobs]), canonical(dict(greetings)), attachment_name,
             config_hash(config), now()))
        return token

    def get_batch(self, token):
        row = self.connection.execute('SELECT * FROM batches WHERE token=?', (token,)).fetchone()
        if row is None:
            raise KeyError('Batch not found')
        result = dict(row)
        result['jobs'] = json.loads(result.pop('jobs_json'))
        result['greetings'] = json.loads(result.pop('greetings_json'))
        return result

    def batch_jobs(self, token):
        return [Job(**j) for j in self.get_batch(token)['jobs']]

    def execution_jobs(self, token):
        identities = {(r['platform'], r['platform_job_id']) for r in self.connection.execute(
            'SELECT j.platform,j.platform_job_id FROM delivery_queue q JOIN jobs j ON j.id=q.job_id WHERE q.batch_id=?', (token,))}
        return [j for j in self.batch_jobs(token) if (j.platform,j.job_id) in identities]

    def confirm_batch(self, token, *, config=None, exclude_ids=None, daily=False):
        """Atomically confirm and enqueue. Optional current config detects stale previews.

        The caller must use the stored content, never current mutable settings, to send.
        """
        with self.exclusive(), self.transaction():
            batch = self.get_batch(token)
            if config is not None and config_hash(config) != batch['config_hash']:
                raise ValueError('Configuration changed; preview again')
            if batch['status'] != 'pending':
                raise ValueError('Batch already confirmed or executed')
            excluded = set(exclude_ids or [])
            batch_jobs = self.batch_jobs(token)
            for job in batch_jobs:
                row = self.connection.execute('SELECT * FROM jobs WHERE platform=? AND platform_job_id=?', (job.platform,job.job_id)).fetchone()
                if row and (row['deleted_at'] or row['url'] != job.url):
                    raise ValueError('Job changed; preview again')
            from job_agent.store.followup import batch_options, execution_allowed
            followup = batch_options(self,token)
            if followup:
                from job_agent.config.engine import EngineSettings
                if not config or any(not execution_allowed(self,j,EngineSettings.model_validate(config),time.time(),token) for j in batch_jobs):
                    raise ValueError('Followup no longer eligible')
            job_ids = {(j.platform,j.job_id): self.upsert_job(platform=j.platform, platform_job_id=j.job_id, url=j.url) for j in batch_jobs}
            identities = {str(i) for i in job_ids.values()}
            if not excluded <= identities or excluded == identities:
                raise ValueError('exclude_ids must leave at least one previewed job')
            for job in batch_jobs:
                job_id = job_ids[job.platform,job.job_id]
                row = self.connection.execute('SELECT status,batch_id,attempted_at,reason_code FROM delivery_queue WHERE job_id=?', (job_id,)).fetchone()
                # failed 表示副作用前明确失败（没发出任何东西）；用户重新预览并确认后允许再投。
                # 手动确认保留历史约束；每日授权路径只放行冷静期满或旧预筛记录。
                history = self.history_blocked(job_id)
                rebindable = row is not None and ((row['status'] == 'pending' and row['attempted_at'] is None) or row['status'] == 'failed')
                if daily:
                    history = not self.daily_rebindable(row, job, config or {})
                    rebindable = not history
                if followup:
                    history = False
                    rebindable = True
                if history or (row and not rebindable and (row['status'] != 'pending' or row['batch_id'] is not None)):
                    raise ValueError('Job already attempted or bound to a batch')
                if str(job_id) in excluded:
                    continue
                self.connection.execute("""INSERT INTO delivery_queue(job_id,status,batch_id) VALUES(?,'pending',?)
                    ON CONFLICT(job_id) DO UPDATE SET batch_id=excluded.batch_id,
                    status='pending', reason_code='', attempted_at=NULL, error_detail='' """, (job_id,token))
            self.connection.execute("UPDATE batches SET status='confirmed' WHERE token=?", (token,))

    def complete_batch(self, token):
        with self.exclusive(), self.transaction():
            batch = self.get_batch(token)
            if batch['status'] != 'confirmed':
                raise ValueError('Batch must be confirmed')
            if self.connection.execute("SELECT 1 FROM delivery_queue WHERE batch_id=? AND status='pending'", (token,)).fetchone():
                raise ValueError('Batch still has pending jobs')
            self.connection.execute("UPDATE batches SET status='executed' WHERE token=?", (token,))
