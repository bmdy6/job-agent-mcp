"""Read-only original-site receipt readers shared by the adapter and audit tool.

Following a known application action never requires clicking Apply or Continue
again. A positive application and an exact outgoing greeting are independent.
"""
import time
from browser.base import PageTimeout
from . import pages


def read_application(space, job, *, clock=time.time):
    page=space.new_page()
    try:
        page.goto(pages.APPLICATIONS_URL,wait_ms=15000)
        pages.guard(page.evaluate(pages.STATE_JS))
        page.wait_for(pages.APPLICATION_ROWS)
        pages.guard(page.evaluate(pages.STATE_JS))
        return pages.fresh_application_record(page.evaluate(pages.APPLICATIONS_JS),
            job,job['attempted_at'],clock())
    finally:
        page.close()


def read_greeting(space, job, *, clock=time.time):
    page=space.new_page()
    try:
        page.goto(pages.CHAT_URL,wait_ms=10000)
        pages.guard(page.evaluate(pages.STATE_JS))
        page.wait_for(pages.LIST)
        pages.guard(page.evaluate(pages.STATE_JS))
        listing=page.evaluate(pages.LIST_JS)
        pages.guard(listing)
        rows=[r for r in listing.get('rows',[]) if r.get('jobNumber')==job['platform_job_id']]
        if len(rows)!=1 or not rows[0].get('sessionId'):
            return False
        # Opening an existing matching conversation is a read-only operation;
        # no contact, apply, fill, press or send control is used.
        if page.evaluate(pages.select_existing_session(rows[0])) is not True:
            return False
        try:
            page.wait_for(pages.MESSAGE_READY,timeout_ms=3000)
        except PageTimeout:
            pages.guard(page.evaluate(pages.STATE_JS))
            return False
        pages.guard(page.evaluate(pages.STATE_JS))
        return pages.fresh_greeting_receipt(page.evaluate(pages.MESSAGES_JS),job,
            rows[0]['sessionId'],clock())
    finally:
        page.close()
