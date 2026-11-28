"""Daily queue policy and frozen delivery identities; no browser/network work here."""
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from job_agent.engine.cooldown import duplicate, CooldownSettings
from job_agent.config.calendar import effective_limit


class DailyQueue:
    def attempt_times(self, platform):
        return [r[0] for r in self.connection.execute("""SELECT a.attempted_at FROM delivery_attempts a
            JOIN jobs j ON j.id=a.job_id WHERE j.platform=?""",(platform,))]

    def not_started_proven(self, job_id):
        """Only adapter-proven no-effect evidence resolves its own pre-effect marker.

        A real unknown receipt (including one from another batch) always wins.
        """
        latest=self.connection.execute('SELECT * FROM deliveries WHERE job_id=? ORDER BY id DESC LIMIT 1',(job_id,)).fetchone()
        if not latest or latest['status']!='skipped' or latest['reason_code']!='not_started':
            return False
        return not self.connection.execute("""SELECT 1 FROM deliveries u WHERE job_id=? AND
            (status='sent' OR (status='unverified' AND
                (reason_code<>'interrupted' OR batch_id IS NULL OR NOT EXISTS (
                    SELECT 1 FROM deliveries n WHERE n.job_id=u.job_id AND n.batch_id=u.batch_id
                    AND n.id>u.id AND n.status='skipped' AND n.reason_code='not_started')))) LIMIT 1""",
            (job_id,)).fetchone()

    def uncertain_events(self):
        events=[dict(r) for r in self.connection.execute("""SELECT u.job_id,
            MAX(CAST(strftime('%s',u.created_at) AS REAL)) AS attempted_at
            FROM deliveries u WHERE u.status='unverified' GROUP BY u.job_id
            HAVING MAX(u.id)>COALESCE((SELECT MAX(s.id) FROM deliveries s
                WHERE s.job_id=u.job_id AND (s.status='sent' OR s.kind='readonly_retry')),0)""")]
        return [r for r in events if not self.not_started_proven(r['job_id'])]

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
                         planning_exclusions=None, candidate_counts=None):
        from job_agent.engine.models import Job
        at = time.time() if clock is None else clock
        day = datetime.fromtimestamp(at,ZoneInfo(settings.timezone)).date()
        blocked=set(blocked)|self.paused_platforms().keys()
        remaining = {}
        for p, cfg in settings.platforms.items():
            used = sum(datetime.fromtimestamp(ts,ZoneInfo(settings.timezone)).date()==day
                       for ts in self.attempt_times(p))
            limit = effective_limit(settings, p, at)
            remaining[p] = max(0,limit-used)
            if max_per_platform is not None:
                remaining[p] = min(remaining[p], max_per_platform)
        sent_today={r['job_id'] for r in self.connection.execute(
            "SELECT job_id,created_at FROM deliveries WHERE status='sent'")
            if datetime.fromisoformat(r['created_at'].replace('Z','+00:00')).astimezone(ZoneInfo(settings.timezone)).date()==day}
        uncertain={r['job_id'] for r in self.uncertain_events()}
        reconciled={r[0] for r in self.connection.execute("SELECT DISTINCT job_id FROM deliveries WHERE kind='reconcile'")}
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
            if j['id'] in reconciled:
                continue  # An audit outcome never authorizes another application.
            if p not in settings.platforms or not settings.platforms[p].enabled:
                continue
            if any(w.strip() and w.casefold() in (j['company'] or '').casefold() for w in settings.company_exclude):
                exclude(p,'company_excluded')
                continue
            if any(w.strip() and w.casefold() in (j['title'] or '').casefold() for w in settings.title_exclude):
                exclude(p,'title_excluded')
                continue
            if settings.jd_exclude:
                jd=(j.get('jd') or '').casefold()
                for phrase in settings.jd_exclude_ignore:
                    if phrase.strip():
                        jd=jd.replace(phrase.casefold(),'')
                if any(w.strip() and w.casefold() in jd for w in settings.jd_exclude):
                    exclude(p,'jd_excluded')
                    continue
            if settings.salary_floor_max_k is not None:
                from job_agent.collect.filters import salary_lower_k
                floor=salary_lower_k(j['salary'] or '')
                if floor is not None and floor>settings.salary_floor_max_k:
                    exclude(p,'salary_floor_too_high')
                    continue
            if j['id'] in sent_today:
                continue
            if j['id'] in uncertain or status=='unverified' or (status=='skipped' and j['reason_code'] not in {'cooldown','legacy_filtered','duplicate_company','company_duplicate','not_started'}):
                continue
            if status=='skipped' and j['reason_code']=='not_started' and not self.not_started_proven(j['id']):
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
        from job_agent.store.scoring import scores
        native=scores(self)
        from job_agent.engine.job_ranking import rank_jobs
        from job_agent.engine.job_ranking import eligible_activity
        old=[j for j in old if eligible_activity(j,settings)]
        new=[j for j in new if eligible_activity(j,settings)]
        if candidate_counts is not None:
            from collections import Counter
            candidate_counts.update(Counter(j["platform"] for j in old+new))
        chosen=[]
        for group in (rank_jobs(old,settings,native),rank_jobs(new,settings,native)):
            for j in group:
                if j['platform'] not in blocked and remaining[j['platform']]>0:
                    chosen.append(j); remaining[j['platform']]-=1
            if any(j not in chosen for j in old):
                break
        return chosen

    def daily_rebindable(self, row, job, config):
        if self.connection.execute("SELECT 1 FROM deliveries WHERE job_id=? AND kind='reconcile' LIMIT 1", (self.identity(job)['id'],)).fetchone():
            return False
        if self.identity(job)['id'] in {r['job_id'] for r in self.uncertain_events()}:
            return False
        if not row:
            return True
        if row['status'] in {'unverified'}:
            return False
        if row['status']=='skipped' and row['reason_code'] not in {'cooldown','legacy_filtered','duplicate_company','company_duplicate','not_started'}:
            return False
        if row['status']=='skipped' and row['reason_code']=='not_started':
            return self.not_started_proven(self.identity(job)['id'])
        if row['status'] in {'sent','skipped'}:
            return not self.duplicate_for(job,CooldownSettings.model_validate(config.get('cooldown',{})))
        return row['status'] in {'pending','failed'} and row['attempted_at'] is None
