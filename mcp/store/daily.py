"""Daily queue policy and frozen delivery identities; no browser/network work here."""
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from engine.cooldown import duplicate, CooldownSettings


class DailyQueue:
    def attempt_times(self, platform):
        return [r[0] for r in self.connection.execute("""SELECT a.attempted_at FROM delivery_attempts a
            JOIN jobs j ON j.id=a.job_id WHERE j.platform=?""",(platform,))]

    def uncertain_events(self):
        # The pre-effect interrupted event is resolved by a later confirmed SENT.
        # A later legacy failed/pending snapshot cannot resolve an unknown receipt.
        return [dict(r) for r in self.connection.execute("""SELECT u.job_id,
            MAX(CAST(strftime('%s',u.created_at) AS REAL)) AS attempted_at
            FROM deliveries u WHERE u.status='unverified' GROUP BY u.job_id
            HAVING MAX(u.id)>COALESCE((SELECT MAX(s.id) FROM deliveries s
                WHERE s.job_id=u.job_id AND (s.status='sent' OR s.kind='readonly_retry')),0)""")]

    def identity(self, job):
        row = self.connection.execute('SELECT * FROM jobs WHERE platform=? AND platform_job_id=?',
                                      (job.platform,job.job_id)).fetchone()
        return dict(row) if row else {}

    def duplicate_for(self, job, settings=None, clock=None):
        settings = settings or CooldownSettings.model_validate((self._run_config or {}).get('cooldown', {}))
        at = time.time() if clock is None else clock
        target = self.identity(job)
        for row in self.connection.execute('SELECT * FROM delivery_identities WHERE attempted_at>?', (at-settings.days*86400,)):
            if duplicate(target,dict(row),settings,at):
                return True
        # A terminal uncertain queue entry is never retried and conservatively reserves its identity.
        for row in self.connection.execute("""SELECT j.*,COALESCE(q.attempted_at,?) AS attempted_at FROM jobs j
            JOIN delivery_queue q ON j.id=q.job_id WHERE q.status='unverified'""", (at,)):
            if duplicate(target,dict(row),settings,at):
                return True
        for event in self.uncertain_events():
            prior=self.get_job(event['job_id'])
            if prior and duplicate(target,{**prior,'attempted_at':event['attempted_at'] or at},settings,at):
                return True
        return False

    def daily_candidates(self, settings, *, cutoff, clock=None, max_per_platform=None, blocked=(),
                         planning_exclusions=None):
        from engine.models import Job
        at = time.time() if clock is None else clock
        day = datetime.fromtimestamp(at,ZoneInfo(settings.timezone)).date()
        blocked=set(blocked)|self.paused_platforms().keys()
        remaining = {}
        for p, cfg in settings.platforms.items():
            used = sum(datetime.fromtimestamp(ts,ZoneInfo(settings.timezone)).date()==day
                       for ts in self.attempt_times(p))
            remaining[p] = max(0,cfg.daily_limit-used)
            if max_per_platform is not None:
                remaining[p] = min(remaining[p], max_per_platform)
        uncertain={r['job_id'] for r in self.uncertain_events()}
        recent_sent={r[0] for r in self.connection.execute("""SELECT DISTINCT d.job_id
            FROM delivery_identities i JOIN deliveries d ON d.id=i.delivery_id
            WHERE d.status='sent' AND i.attempted_at>?""",(at-settings.cooldown.days*86400,))}
        def exclude(platform, reason):
            if planning_exclusions is not None:
                counts=planning_exclusions.setdefault(platform,{})
                counts[reason]=counts.get(reason,0)+1

        eligible=[]
        for row in self.connection.execute('''SELECT j.*,q.status AS queue_status,q.reason_code,q.attempted_at
                FROM jobs j LEFT JOIN delivery_queue q ON j.id=q.job_id
                WHERE j.deleted_at IS NULL AND j.url<>'' '''):
            j=dict(row); p=j['platform']; status=j['queue_status']
            if p not in settings.platforms or not settings.platforms[p].enabled:
                continue
            if j['id'] in uncertain or status=='unverified' or (status=='skipped' and j['reason_code'] not in {'cooldown','legacy_filtered','duplicate_company','company_duplicate'}):
                continue
            # Match the confirmed-batch rebind rule; an attempted pending/failed
            # snapshot is not a fresh job, even if its latest status is stale.
            if status in {'pending','failed'} and j['attempted_at'] is not None:
                exclude(p,'attempted_pending')
                continue
            job=Job(p,j['platform_job_id'],j['url'])
            # Frozen send history is authoritative even when a legacy queue snapshot
            # still says pending. If another job has no HR metadata, leave it for
            # the existing read-only HR enrichment and final locked cooldown check.
            known_identity=(status in {'sent','skipped'} or j['id'] in recent_sent
                            or bool(j['hr_name'] or j['hr_id']))
            if known_identity and self.duplicate_for(job,settings.cooldown,at):
                exclude(p,'known_cooldown')
                continue
            eligible.append(j)
        old=[j for j in eligible if j['id']<=cutoff]
        new=[j for j in eligible if j['id']>cutoff]
        # Keep the old/new cutoff; within each cohort activity precedes experience and score.
        from .scoring import scores
        native=scores(self)
        from engine.job_ranking import rank_jobs
        from engine.job_ranking import eligible_activity
        old=[j for j in old if eligible_activity(j,settings)]
        new=[j for j in new if eligible_activity(j,settings)]
        chosen=[]
        for group in (rank_jobs(old,settings,native),rank_jobs(new,settings,native)):
            for j in group:
                if j['platform'] not in blocked and remaining[j['platform']]>0:
                    chosen.append(j); remaining[j['platform']]-=1
            if any(j not in chosen for j in old):
                break
        return chosen

    def daily_rebindable(self, row, job, config):
        if self.identity(job)['id'] in {r['job_id'] for r in self.uncertain_events()}:
            return False
        if not row:
            return True
        if row['status'] in {'unverified'}:
            return False
        if row['status']=='skipped' and row['reason_code'] not in {'cooldown','legacy_filtered','duplicate_company','company_duplicate'}:
            return False
        if row['status'] in {'sent','skipped'}:
            return not self.duplicate_for(job,CooldownSettings.model_validate(config.get('cooldown',{})))
        return row['status'] in {'pending','failed'} and row['attempted_at'] is None
