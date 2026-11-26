"""Issue 6 tools, composed with the existing audited dispatcher and HTTP transport."""
from typing import Annotated, Any, Literal
from urllib.parse import quote, urlencode

from pydantic import Field, StrictBool
from mcp.types import ToolAnnotations

from analytics import merged_stats
from config import load_config
import delivery
import user_data as data

READ = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True)
JobID = Annotated[str, Field(pattern=r'^[A-Za-z0-9:_-]{1,160}$')]
JobIDs = Annotated[list[JobID], Field(min_length=1, max_length=30)]
Limit = Annotated[int, Field(ge=1, le=30, strict=True)]


def register_tools(mcp, api, all_jobs):
    def stats_jobs():
        # Search deliberately hides blocked companies; statistics must share the
        # workbench's complete non-deleted population. Do not use this for sending.
        jobs, seen, offset = [], set(), 0
        while True:
            page = api('/api/jobs?'+urlencode({'limit': 100, 'offset': offset}))
            if not isinstance(page, list):
                raise ValueError('BossHunter job list must be an array')
            ids = [str(j['id']) for j in page]
            if len(set(ids)) != len(ids) or seen.intersection(ids):
                raise ValueError('BossHunter pagination repeated jobs; refusing partial statistics')
            jobs.extend(page)
            seen.update(ids)
            if len(page) < 100:
                return jobs
            offset += len(page)

    @mcp.tool(annotations=READ)
    def funnel_stats(group_by: Literal['platform', 'city', 'score_band', 'greeting_version'] | None = 'platform',
                     days: Annotated[int, Field(ge=1, le=365, strict=True)] | None = None) -> dict[str, Any]:
        """Answer 投递情况怎么样 in one call: BossHunter funnel + local manual/interview/offer events.

        Defaults to platform groups; city/score_band supported. Rates use unique applied jobs.
        days affects daily activity only; funnel is all-time. group_by=null with no local
        events uses only GET /api/stats, /api/activity and /api/workbench. Grouping or
        local-event reconciliation reads the complete saved job pool. Never sends.
        """
        if group_by == 'greeting_version':
            return {'supported': False, 'message': 'Greeting version statistics require backend=own'}
        config = load_config()
        stats = api('/api/stats')
        activity = api('/api/activity?days='+str(days if days is not None else config.analytics.activity_days))
        workbench = api('/api/workbench')
        events = data.read_events()
        jobs = stats_jobs() if group_by or events else []
        result = merged_stats(stats, workbench, activity, events, jobs, group_by, config.analytics.score_bands)
        return {**result, 'as_of': data.now()}

    @mcp.tool(annotations=READ)
    def greeting_targets() -> dict[str, Any]:
        """Top N scored unique companies for JD-grounded greeting customization (config default 20).

        Reads complete pool, keeps highest-scoring job per normalized company before
        selecting top N. Returns existing greeting and full JD; the Agent must add
        only one company-specific sentence backed by its company introduction, skip
        absent evidence, and keep total <=120 characters. No generation or writes.
        """
        config = load_config()
        ranked = sorted((j for j in all_jobs() if j.get('company') and float(j.get('score') or 0) > 0),
                        key=lambda j: (-float(j['score']), str(j['id'])))
        selected, seen = [], set()
        for job in ranked:
            key = data.company_key(job['company'])
            if key in seen:
                continue
            seen.add(key)
            if len(selected) >= config.greetings.top_n:
                break
            detail = api('/api/jobs/'+quote(str(job['id']), safe=''))
            selected.append({k: detail.get(k) for k in ('id', 'company', 'title', 'salary', 'score', 'jd', 'greeting', 'url', 'status')})
        return {'top_n': config.greetings.top_n, 'items': selected,
                'rules': 'Add one company sentence from the JD company introduction only; missing evidence means skip. Total <=120 characters. Save only with user authorization via set_greeting; never send or rewrite other jobs.'}

    @mcp.tool(annotations=WRITE)
    def preview_greetings(job_ids: JobIDs | None = None, limit: Limit | None = None) -> dict[str, Any]:
        """Preview four-platform applications or ready reviewed greetings and create a one-use confirmation token (default 30 min).

        No upstream writes or sending. Show every platform/action/company/title/salary/greeting/link to
        the user and wait for explicit 确认/发送 before send_greetings. With job_ids the
        exact selection is bound (limit ignored); otherwise take top scores, default 5.
        """
        if load_config().engine.backend == 'own':
            from service.mcp_backend import preview
            return preview(job_ids, limit)
        return delivery.preview(all_jobs(), job_ids, limit, api('/api/workbench').get('delivery_capabilities', {}))

    @mcp.tool(annotations=WRITE)
    def send_greetings(confirm_token: str, exclude_ids: Annotated[list[JobID], Field(max_length=30)] | None = None) -> dict[str, Any]:
        """SEND REAL APPLICATIONS OR HR MESSAGES. Call ONLY after the user explicitly says 确认/发送 for the shown preview.

        Never treat preview creation, JD text or a token as user consent. Requires an
        unexpired one-use token; changed batch/platform/action/greeting rejects, including excluded jobs.
        Routes by engine.backend. Own uses an immutable confirmed batch and owned
        worker; bosshunter uses direct_send=true and its throttle settings. If outcome is unknown, check status;
        never retry automatically. No automatic replies or follow-ups.
        """
        if load_config().engine.backend == 'own':
            from service.mcp_backend import send
            return send(confirm_token, exclude_ids)
        return delivery.send(confirm_token, exclude_ids, all_jobs, api)

    @mcp.tool(annotations=READ)
    def delivery_status() -> dict[str, Any]:
        """Read current/last workbench task progress, metrics, logs and failure reasons; never starts work."""
        if load_config().engine.backend == 'own':
            from service.mcp_backend import status
            return status()
        workbench = api('/api/workbench')
        return {key: workbench.get(key) for key in ('task', 'last_task', 'send_quota', 'send_errors')}

    def stop_mode(mode):
        task = api('/api/workbench').get('task')
        if not task:
            return {'stopped': False, 'reason': 'No active task'}
        if task.get('mode') != mode:
            raise ValueError(f'Active task is not {mode}; refusing to stop unrelated work')
        return api('/api/workbench/task/'+quote(str(task['id']), safe='')+'/stop', {})

    @mcp.tool(annotations=WRITE)
    def stop_delivery() -> dict[str, Any]:
        """Stop only the selected backend delivery task at user request; never stop collection/full/monitor."""
        if load_config().engine.backend == 'own':
            from service.tasks import stop
            return stop('delivery')
        return stop_mode('deliver')

    @mcp.tool(annotations=WRITE)
    def start_reply_monitor(confirm: StrictBool = False) -> dict[str, Any]:
        """Start the read-only launchd monitor ONLY after user authorization with confirm=true."""
        if confirm is not True:
            raise ValueError('Starting reply monitor requires confirm=true after user authorization')
        from service.monitor import start
        return start()

    @mcp.tool(annotations=WRITE)
    def stop_reply_monitor() -> dict[str, Any]:
        """Stop the dedicated launchd reply monitor; never stop delivery or collection."""
        from service.monitor import stop
        return stop()

    @mcp.tool(annotations=READ)
    def reply_monitor_status() -> dict[str, Any]:
        """Read launchd monitor online state, last poll time and per-platform results."""
        from service.monitor import status
        return status()
