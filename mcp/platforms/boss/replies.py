"""Read existing sessions only; latest loaded messages, not a full-history export."""
import logging
from browser.base import PageTimeout
from . import pages

log = logging.getLogger(__name__)


class BossReplySource:
    platform = 'boss'

    def fetch(self, space, *, since=None, cursor=None):
        page = space.new_page()
        stage, selector, index = 'navigate', 'chat_url', None
        empty, missing = 0, 0
        try:
            page.goto(pages.CHAT_URL, wait_ms=10000)
            stage, selector = 'page_state', 'boss.state'
            pages.guard(page.evaluate(pages.STATE_JS))
            stage, selector = 'list_ready', pages.LIST
            page.wait_for(pages.LIST)
            pages.guard(page.evaluate(pages.STATE_JS))
            stage, selector = 'list_projection', pages.LIST
            listing = page.evaluate(pages.LIST_JS)
            pages.guard(listing)
            if not isinstance(listing.get('rows'), list) or not listing['rows']:
                raise pages.PlatformStateError('聊天列表结构为空或未加载')
            result, seen = [], set()
            for index, row in enumerate(listing['rows']):
                if not row.get('encryptJobId'):
                    continue
                summary = pages.conversation_summary(row) if cursor is not None else None
                if cursor is not None and not cursor.select(summary):
                    continue
                # BOSS includes empty, already-established conversations with
                # lastMsgId=0. There is no message to wait for or fabricate.
                if row.get('lastMsgId') in ('0', 0):
                    empty += 1
                    continue
                stage, selector = 'session_identity', '[role=listitem] .friend-content'
                projection = pages.select_session(row)
                ready = pages.session_ready(row)
                stage = 'session_select'
                page.click_projected(projection)
                stage, selector = 'message_ready', '.message-item[data-mid]'
                try:
                    page.wait_for(ready, timeout_ms=3000)
                except PageTimeout:
                    pages.guard(page.evaluate(pages.STATE_JS))
                    missing += 1
                    log.warning('boss monitor skipped stage=%s selector=%s row=%s reason=latest_message_unavailable',
                                stage, selector, index)
                    continue
                stage, selector = 'message_projection', '.message-item[data-mid]'
                pages.guard(page.evaluate(pages.STATE_JS))
                data = page.evaluate(pages.MESSAGES_JS)
                pages.guard(data)
                # Validate even when events() would filter every message out.
                if (data.get('current_job_id') != row['encryptJobId']
                        or data.get('current_conversation_id') != row['uniqueId']):
                    raise pages.PlatformStateError('会话切换未完成或岗位不匹配')
                for event in pages.events(data, since):
                    if (event.platform_job_id != row['encryptJobId']
                            or not event.message_id.startswith(str(row['uniqueId']) + ':')):
                        raise pages.PlatformStateError('会话切换未完成或岗位不匹配')
                    if event.message_id not in seen:
                        seen.add(event.message_id)
                        result.append(event)
                if cursor is not None:
                    cursor.acknowledge(summary)
            log.info('boss monitor stage=complete sessions=%s selected=%s baseline=%s empty=%s missing=%s fetched=%s',
                     len(listing['rows']), cursor.selected if cursor is not None else None,
                     cursor.baseline if cursor is not None else None, empty, missing, len(result))
            return result
        except Exception as error:
            # Do not log exception text, message IDs, job IDs, names or bodies.
            log.warning('boss monitor failed stage=%s selector=%s row=%s code=%s error_type=%s',
                        stage, selector, index, getattr(error, 'code', 'unknown'), type(error).__name__)
            raise
        finally:
            page.close()


ReplySource = BossReplySource
