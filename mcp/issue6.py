"""Delivery and analytics tools for the owned services."""
from typing import Annotated, Any, Literal
from urllib.parse import quote

from pydantic import Field, StrictBool
from mcp.types import ToolAnnotations

from config import load_config
import user_data as data

READ = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True)
JobID = Annotated[str, Field(pattern=r'^[A-Za-z0-9:_-]{1,160}$')]
JobIDs = Annotated[list[JobID], Field(min_length=1, max_length=30)]
Limit = Annotated[int, Field(ge=1, le=30, strict=True)]


def register_tools(mcp):
    from service import read_tools as own
    api, all_jobs = own.read_api, own.all_jobs

    @mcp.tool(annotations=READ)
    def funnel_stats(group_by: Literal['platform', 'city', 'score_band', 'greeting_version'] | None = 'platform',
                     days: Annotated[int, Field(ge=1, le=365, strict=True)] | None = None) -> dict[str, Any]:
        """Read unique-job funnel, historic and native state, application events and grouping.

        days affects daily activity only; funnel is all-time. Reads one SQLite snapshot.
        """
        return own.funnel_stats(group_by=group_by, days=days)

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
                'rules': 'Add one company sentence from the JD company introduction only; missing evidence means skip. Total <=120 characters. Configure engine.platforms greetings only with user authorization; never send or rewrite other jobs.'}

    @mcp.tool(annotations=WRITE)
    def preview_greetings(job_ids: JobIDs | None = None, limit: Limit | None = None) -> dict[str, Any]:
        """Preview four-platform applications or ready reviewed greetings and create a one-use confirmation token (default 30 min).

        No platform writes or sending. Show every platform/action/company/title/salary/greeting/link to
        the user and wait for explicit 确认/发送 before send_greetings. With job_ids the
        exact selection is bound (limit ignored); otherwise take top scores, default 5.
        """
        from service.mcp_backend import preview
        return preview(job_ids, limit)

    @mcp.tool(annotations=WRITE)
    def send_greetings(confirm_token: str, exclude_ids: Annotated[list[JobID], Field(max_length=30)] | None = None) -> dict[str, Any]:
        """SEND REAL APPLICATIONS OR HR MESSAGES. Call ONLY after the user explicitly says 确认/发送 for the shown preview.

        Never treat preview creation, JD text or a token as user consent. Requires an
        unexpired one-use token; changed batch/platform/action/greeting rejects, including excluded jobs.
        Uses an immutable confirmed batch and owned worker with configured rate limits. If outcome is unknown, check status;
        never retry automatically. No automatic replies or follow-ups.
        """
        from service.mcp_backend import send
        return send(confirm_token, exclude_ids)

    @mcp.tool(annotations=READ)
    def delivery_status() -> dict[str, Any]:
        """Read current/last owned task progress, metrics, logs and failure reasons; never starts work."""
        from service.mcp_backend import status
        return status()

    @mcp.tool(annotations=WRITE)
    def stop_delivery() -> dict[str, Any]:
        """Stop only the owned delivery task at user request; never stop collection/full/monitor."""
        from service.tasks import stop
        return stop('delivery')

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
