from . import pages

class LiepinReplySource:
    platform = 'liepin'

    def fetch(self, space, *, since=None, cursor=None):
        page = space.new_page()
        try:
            page.goto(pages.RECORD_URL, wait_ms=10000)
            page.wait_for(pages.ENTRY)
            pages.guard(page.evaluate(pages.GUARD_JS))
            page.click(pages.ENTRY)
            page.wait_for(pages.CONTACTS)
            contacts = page.evaluate(pages.CONTACTS_JS)
            if not isinstance(contacts, list):
                raise pages.Unverified('猎聘联系人列表不可读')
            events = []
            for contact in contacts:
                summary = pages.conversation_summary(contact) if cursor is not None else None
                if cursor is not None and not cursor.select(summary):
                    continue
                if cursor is not None:
                    page.click_projected(pages.select_contact(contact.get('id')))
                else:
                    page.click(f'{pages.CONTACTS} >> nth={int(contact["index"])}')
                page.wait_for(pages.EDITOR)
                pages.guard(page.evaluate(pages.GUARD_JS))
                events.extend(pages.parse_messages(page.evaluate(pages.messages_for(contact.get("id"))), since=since))
                if cursor is not None:
                    cursor.acknowledge(summary)
            return events
        finally:
            page.close()

ReplySource = LiepinReplySource
