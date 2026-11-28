"""Read-only Liepin replies, with content-free diagnostics at each boundary."""
import logging
import re
from job_agent.platforms.liepin import pages

log = logging.getLogger(__name__)


def step(stage, action):
    log.info('platform=liepin stage=%s reason_code=started', stage)
    try:
        value = action()
    except Exception as error:
        error.stage = stage
        # These are internal enums, never exception text or page data.
        error.reason_code = getattr(error, 'reason_code', getattr(error, 'code', 'unexpected_error'))
        if error.reason_code == 'browser_error':
            # Driver text is never logged; extract only its fixed internal step enum.
            match = re.fullmatch(r'Ego [a-z_]+\.(resolve|target_geometry|target_recheck|mouse\.move|mouse\.down|mouse\.up|post_click_observation) failed: browser_error', str(error))
            if match:
                error.reason_code = 'browser_' + match[1].replace('.', '_')
        log.warning('platform=liepin stage=%s reason_code=%s error_type=%s',
                    stage, error.reason_code, type(error).__name__)
        raise
    log.info('platform=liepin stage=%s reason_code=ok', stage)
    return value


class LiepinReplySource:
    platform = 'liepin'

    def fetch(self, space, *, since=None, cursor=None):
        page = step('open_page', space.new_page)
        failed = False
        try:
            step('navigate', lambda: page.goto(pages.RECORD_URL, wait_ms=10000))
            step('guard_page', lambda: pages.guard(page.evaluate(pages.GUARD_JS)))
            step('wait_im_entry', lambda: page.wait_for(pages.ENTRY))
            step('open_im', lambda: page.click(pages.ENTRY))
            step('guard_im', lambda: pages.guard(page.evaluate(pages.GUARD_JS)))
            step('wait_contacts', lambda: page.wait_for(pages.CONTACTS))
            def read_contacts():
                contacts = page.evaluate(pages.CONTACTS_JS)
                if not isinstance(contacts, list) or any(not isinstance(c, dict) for c in contacts):
                    error = pages.Unverified('猎聘联系人列表不可读')
                    error.reason_code = 'contacts_unreadable'
                    raise error
                return contacts
            contacts = step('read_contacts', read_contacts)
            events = []
            for contact in contacts:
                summary = step('read_summary', lambda: pages.conversation_summary(contact)) if cursor is not None else None
                if cursor is not None and not cursor.select(summary):
                    continue
                projection = step('locate_conversation', lambda: pages.select_contact(contact.get('id')))
                if cursor is not None:
                    step('click_conversation', lambda: page.click_projected(projection))
                else:
                    step('click_conversation', lambda: page.click(f'{pages.CONTACTS} >> nth={int(contact["index"])}'))
                step('guard_conversation', lambda: pages.guard(page.evaluate(pages.GUARD_JS)))
                step('wait_editor', lambda: page.wait_for(pages.EDITOR))
                events.extend(step('read_messages', lambda: pages.parse_messages(
                    page.evaluate(pages.messages_for(contact.get('id'))), since=since)))
                if cursor is not None:
                    cursor.acknowledge(summary)
            return events
        except BaseException:
            failed = True
            raise
        finally:
            try:
                step('close_page', page.close)
            except Exception:
                if not failed:
                    raise

ReplySource = LiepinReplySource
