"""Status-only fallback authorized by #37; no chat/write endpoints."""
import time
from . import status


class Job51ReplySource:
    platform = '51job'

    def fetch(self, space, *, since=None, cursor=None):
        page = space.new_page()
        try:
            # Keep the existing single temporary page for monitor filters.
            snapshots = {tag: status.read_application_pages(page, tag=tag)
                         for tag in ('', *status.STATUS_TAGS)}
            events = status.application_events(snapshots, observed_at=time.time(), since=since)
            if cursor is None:
                return events
            changed = set()
            for summary in status.conversation_summaries(snapshots):
                if cursor.select(summary):
                    changed.add(summary.conversation_id)
                    cursor.acknowledge(summary)
            return [e for e in events if ':'.join(e.message_id.split(':')[1:3]) in changed]
        finally:
            page.close()


ReplySource = Job51ReplySource
