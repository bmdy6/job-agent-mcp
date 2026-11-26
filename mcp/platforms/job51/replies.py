"""Status-only fallback authorized by #37; no chat/write endpoints."""
import time
from . import status


class Job51ReplySource:
    platform = '51job'

    def fetch(self, space, *, since=None, cursor=None):
        page = space.new_page()
        try:
            snapshots = {}
            for tag in ('', *status.STATUS_TAGS):
                url, rows, seen_urls, last_page = status.APPLICATION_URL + tag, [], set(), 0
                while url:
                    if url in seen_urls:
                        raise status.Unverified('51job 申请分页重复')
                    seen_urls.add(url)
                    page.goto(url, wait_ms=10000)
                    data = page.evaluate(status.APPLICATION_JS)
                    current_rows, url = status.parse_application_page(data, tag)
                    if current_rows and data['page'] != last_page + 1:
                        raise status.Unverified('51job 申请分页不连续')
                    last_page = data['page']
                    rows.extend(current_rows)
                if len({r['id'] for r in rows}) != len(rows):
                    raise status.Unverified('51job 申请岗位重复，列表可能发生变化')
                snapshots[tag] = rows
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
