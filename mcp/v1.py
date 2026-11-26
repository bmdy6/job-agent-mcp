"""Context tools backed by owned SQLite and private local files."""

from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import quote

from mcp.types import ToolAnnotations
from pydantic import Field

from service import read_tools as own
from config import load_config, job_agent_home
import user_data as data
from applications import application_funnel, company_context, exclusion_matches, job_link, REPLIED

JobID = Annotated[str, Field(pattern=r'^[A-Za-z0-9:_-]{1,160}$')]
Event = Literal['greeted', 'replied', 'resume_requested', 'interview_scheduled',
                'interviewed', 'interview_done', 'manual_applied', 'offer', 'rejected', 'withdrawn']
Group = Literal['resume_version', 'platform', 'variant']
READ = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)


def import_resume(path: str) -> dict:
    source = Path(path).expanduser()
    suffix = source.suffix.casefold()
    if suffix not in {'.md', '.pdf', '.docx'}:
        raise ValueError('Supported resume formats: .md, .pdf, .docx')
    if not source.is_file():
        raise ValueError('Resume file does not exist')
    if suffix == '.md':
        text = source.read_text(encoding='utf-8')
    elif suffix == '.pdf':
        from pypdf import PdfReader
        text = '\n'.join(page.extract_text() or '' for page in PdfReader(source).pages)
    else:
        from docx import Document
        from docx.table import Table
        from docx.text.paragraph import Paragraph
        document = Document(source)
        parts = []
        for block in document.iter_inner_content():
            if isinstance(block, Paragraph):
                parts.append(block.text)
            elif isinstance(block, Table):
                parts.extend('\t'.join(cell.text for cell in row.cells) for row in block.rows)
        text = '\n'.join(parts)
    return {'text': text, 'format': suffix[1:], 'warnings': [] if text.strip() else ['No extractable text; scanned PDFs require OCR outside this MCP.']}


def register_tools(mcp):
    api = own.read_api
    events = own.read_events
    all_jobs = own.all_jobs

    def profile_data():
        config = load_config()
        return {'preferences': own.preferences(), 'mode': config.mode,
                'practice': config.practice.model_dump(), 'stretch_salary_min': config.stretch_salary_min}

    @mcp.tool(annotations=READ)
    def profile() -> dict[str, Any]:
        """Read owned search preferences plus job-agent mode, practice cap and stretch line (monthly K)."""
        return profile_data()

    @mcp.tool(annotations=READ)
    def experience_list() -> dict[str, Any]:
        """List local experience IDs and titles, without model matching."""
        return {'items': data.experience_list()}

    @mcp.tool(annotations=READ)
    def experience_get(id: str) -> dict[str, Any]:
        """Read one Markdown experience by ID; item=null means absent."""
        return {'item': data.experience_get(id)}

    @mcp.tool(annotations=WRITE)
    def experience_add(title: str, md: str) -> dict[str, Any]:
        """Add a new experience under JOB_AGENT_HOME, never overwriting an existing file."""
        return data.experience_add(title, md)

    @mcp.tool(annotations=READ)
    def company_get(company: str) -> dict[str, Any]:
        """Read cached company research by normalized name; item=null means absent."""
        return {'item': data.company_get(company)}

    @mcp.tool(annotations=WRITE)
    def company_save(company: str, md: str, source_url: str) -> dict[str, Any]:
        """Cache company Markdown, source link and updated_at locally. Never fetch the link."""
        return data.company_save(company, md, source_url)

    @mcp.tool(annotations=WRITE)
    def log_event(job_id: JobID, event: Event, resume_version: str | None = None,
                  variant: str | None = None, chat_url: str | None = None, note: str | None = None) -> dict[str, Any]:
        """Append a local application event, copying company/platform/url from the owned job pool. Does not send."""
        job = api('/api/jobs/'+quote(job_id, safe=''))
        record = {'job_id': job_id, 'event': event, 'company': job.get('company'),
                  'platform': job.get('source_platform') or 'boss', 'url': job.get('url'),
                  'city': job.get('city'), 'score': job.get('score'), 'time': data.now()}
        for key, value in [('resume_version', resume_version), ('variant', variant), ('chat_url', chat_url), ('note', note)]:
            if value is not None:
                record[key] = value
        return own.append_event(record)

    @mcp.tool(annotations=READ)
    def company_history(company: str) -> dict[str, Any]:
        """Return normalized-company application events and saved jobs, with links."""
        return company_context(company, events(), all_jobs())

    @mcp.tool(annotations=READ)
    def funnel(group_by: Group | None = None) -> dict[str, Any]:
        """Unique-job funnel, merging local events with sent/replied/resume_sent saved job states.

        Rates use greeted jobs as denominator (null when zero). Missing cohorts are unknown.
        Later stages imply earlier stages. No model calls or upstream writes.
        """
        return application_funnel(events(), all_jobs(), group_by)

    @mcp.tool(annotations=READ)
    def setup_check() -> dict[str, Any]:
        """Check owned config, database, resume, experiences and configured Ego sessions."""
        checks = {}
        try:
            load_config()
            checks['config'] = {'ok': (job_agent_home()/'config.yaml').is_file()}
        except (ValueError, OSError) as exc:
            checks['config'] = {'ok': False, 'reason': type(exc).__name__}
        else:
            checks.update(own.setup_checks())
        try:
            checks['experience'] = {'ok': bool(data.experience_list())}
        except (ValueError, OSError) as exc:
            checks['experience'] = {'ok': False, 'reason': type(exc).__name__}
        missing = [key for key, check in checks.items() if not check['ok']]
        from service.deployment import status as deployment_status
        return {'ok': not missing, 'missing': missing, 'checks': checks, 'deployment': deployment_status()}

    @mcp.tool(annotations=READ)
    def resume_import(path: str) -> dict[str, Any]:
        """Extract local PDF/docx/md text without storing it. Scanned PDFs need external OCR."""
        return import_resume(path)

    @mcp.tool(annotations=READ)
    def briefing(job_id: JobID | None = None) -> dict[str, Any]:
        """Context entry point. Overview: profile, mode, funnel, linked status queues.

        With job_id: full JD, all local experiences for the Agent to match, cached
        company/history and literal exclusion evidence. Does not evaluate or send.
        New replies are a status snapshot, not an unread-message signal.
        """
        from service.monitor import status as monitor_status
        monitor = monitor_status()
        context = profile_data()
        jobs, event_rows = own.population()
        if job_id is not None:
            job = api('/api/jobs/'+quote(job_id, safe=''))
            link = job_link(job, event_rows)
            return {'profile': context, 'mode': context['mode'], 'monitor': monitor,
                    'job': {**job, **link},
                    'experiences': [data.experience_get(i['id']) for i in data.experience_list()],
                    'experience_matching': 'All materials; relevance is for the calling Agent to determine.',
                    'company': data.company_get(job['company']) if job.get('company') else None,
                    'company_history': company_context(job['company'], event_rows, jobs) if job.get('company') else None,
                    'exclusions': exclusion_matches(job, context['preferences'])}
        categories = {'pending_confirmation': {'ready', 'scored', 'needs_review'},
                      'sent_no_reply': {'sent', 'follow_up_sent'}, 'new_replies': REPLIED}
        todos = {}
        for key, statuses in categories.items():
            items = [job_link(j, event_rows) for j in jobs if j.get('status') in statuses]
            todos[key] = {'count': len(items), 'items': items}
        from service.signals import greeting_stats, interview_schedule
        signals = {'greeting_stats': greeting_stats(), 'interview_schedule': interview_schedule()}
        todos['interview_invitations'] = signals['interview_schedule']
        from analytics import brief_reminders
        todos['company_briefs'] = brief_reminders(jobs, event_rows)
        return {'profile': context, 'mode': context['mode'], 'monitor': monitor, 'funnel': application_funnel(event_rows, jobs),
                'todos': todos, **signals, 'reply_semantics': 'Snapshot of replied/resume_sent/needs_resume jobs; not unread messages.'}

    @mcp.tool(annotations=READ)
    def greeting_stats() -> dict[str, Any]:
        """Unique first-successful-delivery cohorts, reply/invitation rates and Wilson 95% intervals.

        Reuses #59 content hashes; absent historic text is unknown. Small samples
        never imply a winning version. No model, network, writes or sending.
        """
        from service.signals import greeting_stats as read_stats
        return read_stats()

    @mcp.tool(annotations=READ)
    def interview_schedule() -> dict[str, Any]:
        """Read pending HR interview invitations with original time/form and links.

        Invitations require the user's decision. Never accepts or creates calendar events.
        """
        from service.signals import interview_schedule as read_schedule
        return read_schedule()

    from issue6 import register_tools as register_issue6
    register_issue6(mcp)
