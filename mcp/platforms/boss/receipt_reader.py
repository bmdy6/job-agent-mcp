"""Original-site, read-only BOSS message receipt, shared by adapter and audit.

A first-contact form can succeed without navigating to chat. Open only an
existing conversation in a temporary owned tab; never click Contact or Send.
"""
import time
from . import pages


def read_greeting(space, job, *, clock=time.time):
    page=space.new_page()
    try:
        page.goto(pages.CHAT_URL,wait_ms=10000)
        pages.guard(page.evaluate(pages.STATE_JS))
        if not pages.is_chat_url(page.url()):
            return dict(status='not_verified',detail='unexpected_chat_page')
        page.wait_for(pages.LIST)
        pages.guard(page.evaluate(pages.STATE_JS))
        listing=page.evaluate(pages.LIST_JS)
        pages.guard(listing)
        rows=[r for r in listing.get('rows',[]) if r.get('encryptJobId')==job['platform_job_id']]
        if len(rows)!=1:
            return dict(status='not_verified',detail='no_unique_loaded_chat')
        row=rows[0]
        if not row.get('lastMsgId'):
            return dict(status='not_verified',detail='chat_without_stable_message')
        if not row.get('uniqueId'):
            return dict(status='not_verified',detail='chat_without_stable_identity')
        # Selection opens an existing conversation. It cannot submit a message.
        if page.evaluate(pages.select_session(row)) is not True:
            return dict(status='not_verified',detail='session_selection_unavailable')
        page.wait_for(pages.session_ready(row),timeout_ms=3000)
        pages.guard(page.evaluate(pages.STATE_JS))
        if not pages.is_chat_url(page.url()):
            return dict(status='not_verified',detail='unexpected_chat_page')
        data=page.evaluate(pages.MESSAGES_JS)
        confirmed=pages.fresh_greeting_receipt(data,job['platform_job_id'],
            row['uniqueId'],job['greeting'],job['attempted_at'],clock())
        return dict(status='greeting_confirmed' if confirmed else 'not_verified',
                    greeting_confirmed=confirmed,attachment_confirmed=None)
    finally:
        page.close()


def read_no_outgoing(space, job_id):
    """Positive evidence only: missing/unloaded conversations prove nothing."""
    page=space.new_page()
    try:
        page.goto(pages.CHAT_URL,wait_ms=10000)
        pages.guard(page.evaluate(pages.STATE_JS))
        if not pages.is_chat_url(page.url()):
            return False
        page.wait_for(pages.LIST)
        listing=page.evaluate(pages.LIST_JS)
        pages.guard(listing)
        rows=[r for r in listing.get('rows',[]) if r.get('encryptJobId')==job_id]
        if len(rows)!=1 or not rows[0].get('uniqueId'):
            return False
        row=rows[0]
        if page.evaluate(pages.select_session(row)) is not True:
            return False
        page.wait_for(pages.INPUT,timeout_ms=3000)
        for _ in range(2):
            pages.guard(page.evaluate(pages.STATE_JS))
            if not pages.is_chat_url(page.url()):
                return False
            identity=page.evaluate(pages.CHAT_IDENTITY_JS)
            if (not isinstance(identity,dict) or identity.get('job_id')!=job_id
                    or identity.get('conversation_id')!=row['uniqueId']):
                return False
            data=page.evaluate(pages.MESSAGES_JS)
            if data.get('current_conversation_id')!=row['uniqueId']:
                return False
            if not pages.first_message_ready(page.evaluate(pages.CHAT_READY_JS),data,job_id,row['uniqueId']):
                return False
            time.sleep(.25)
        return True
    finally:
        page.close()
