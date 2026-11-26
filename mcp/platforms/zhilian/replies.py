"""Read existing sessions only; latest loaded messages, not a full-history export."""
import time
from . import pages


class ZhilianReplySource:
    platform = 'zhilian'

    def fetch(self, space, *, since=None, cursor=None):
        page = space.new_page()
        try:
            page.goto(pages.CHAT_URL, wait_ms=10000)
            pages.guard(page.evaluate(pages.STATE_JS))
            page.wait_for(pages.LIST)
            pages.guard(page.evaluate(pages.STATE_JS))
            listing = page.evaluate(pages.LIST_JS)
            pages.guard(listing)
            if not isinstance(listing.get('rows'), list) or not listing['rows']:
                raise pages.PlatformStateError('聊天列表结构为空或未加载')
            result, seen = [], set()
            for i, row in enumerate(listing['rows']):
                if not row.get('jobNumber') or not row.get('sessionId'):
                    continue
                summary = pages.conversation_summary(row) if cursor is not None else None
                if cursor is not None and not cursor.select(summary):
                    continue
                # The list can reorder while messages arrive. Resolve by stable
                # session/job identity rather than the observed row index.
                if cursor is not None:
                    page.click_projected(pages.select_session(row))
                else:
                    page.click(pages.session_selector(i))
                page.wait_for(pages.MESSAGE_READY)
                pages.guard(page.evaluate(pages.STATE_JS))
                for attempt in range(30):
                    data = page.evaluate(pages.MESSAGES_JS)
                    pages.guard(data)
                    messages = data.get('messages', [])
                    latest_ready = (cursor is None or summary.last_time is None or any(
                        (pages.epoch(r.get('msg', {}).get('time')) or 0) >= summary.last_time for r in messages))
                    if messages and latest_ready and all(
                            r.get('session', {}).get('sessionId') == row['sessionId']
                            and r.get('session', {}).get('jobNumber') == row['jobNumber'] for r in messages):
                        break
                    if attempt == 29:
                        raise pages.PlatformStateError('会话消息未完成加载')
                    time.sleep(0.1)
                for event in pages.events(data, since):
                    if event.platform_job_id != row.get('jobNumber'):
                        raise pages.PlatformStateError('会话切换未完成或岗位不匹配')
                    if event.message_id not in seen:
                        seen.add(event.message_id)
                        result.append(event)
                if cursor is not None:
                    cursor.acknowledge(summary)
            return result
        finally:
            page.close()


ReplySource = ZhilianReplySource
