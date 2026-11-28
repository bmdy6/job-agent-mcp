"""Existing BOSS conversation composer; uses the adapter's ordinary receipt path."""
from time import time
from job_agent.platforms.boss import pages


def ready(adapter, *, filled=False):
    adapter._chat_identity()
    state = adapter.page.evaluate(pages.CHAT_READY_JS)
    data = adapter.page.evaluate(pages.MESSAGES_JS)
    pages.guard(data)
    rows = data.get('rows')
    if (not isinstance(state,dict) or state.get('root') is not True or state.get('editor') is not True
            or state.get('loading') is not False or (not filled and state.get('input_empty') is not True)
            or data.get('current_job_id') != adapter.job.job_id
            or data.get('current_conversation_id') != adapter.conversation_id
            or not isinstance(rows,list) or not rows or state.get('visible_messages') != len(rows)
            or any(not isinstance(r,dict) or not r.get('id') or r.get('job_id')!=adapter.job.job_id
                or r.get('conversation_id')!=adapter.conversation_id or r.get('direction') not in {'outgoing','system'} for r in rows)
            or not any(r.get('direction')=='outgoing' and r.get('receipt') in {'送达','已读'} for r in rows)):
        raise pages.NotStarted('已有会话未就绪或已收到回复；不跟进')
    return data


def send(adapter, text):
    if adapter.attempted:
        raise pages.PlatformStateError('同一会话禁止重复提交')
    adapter._guard()
    adapter.target = adapter.page.evaluate(pages.JOB_IDENTITY_JS)
    if not isinstance(adapter.target,dict) or not all(adapter.target.get(k) for k in ('company','title')):
        raise pages.NotStarted('岗位身份不可核验；不跟进')
    # Navigate only to the existing chat list; never press first-contact controls.
    adapter.page.goto(pages.CHAT_URL,wait_ms=10000)
    adapter._guard()
    if not pages.is_chat_url(adapter.page.url()):
        raise pages.NotStarted('聊天地址不可核验；不跟进')
    adapter.page.wait_for(pages.LIST)
    listing = adapter.page.evaluate(pages.LIST_JS)
    pages.guard(listing)
    rows = [r for r in listing.get('rows',[]) if r.get('encryptJobId')==adapter.job.job_id]
    if len(rows)!=1 or not rows[0].get('uniqueId') or not rows[0].get('lastMsgId'):
        raise pages.NotStarted('没有唯一已有会话；不跟进')
    row = rows[0]
    if adapter.page.evaluate(pages.select_session(row)) is not True:
        raise pages.NotStarted('已有会话无法选择；不跟进')
    adapter.page.wait_for(pages.session_ready(row),timeout_ms=3000)
    adapter.page.wait_for(pages.INPUT)
    adapter.before = pages.message_ids(ready(adapter))
    if not 20 <= len(text) <= 50 or pages.utf16_length(text)>adapter._setting('greeting_max_length',1000):
        raise pages.NotStarted('跟进文案长度不符；不跟进')
    adapter.page.fill(pages.INPUT,text)
    if adapter.page.evaluate(pages.INPUT_JS)!=text:
        raise pages.NotStarted('跟进全文读回不匹配；不发送')
    latest = ready(adapter,filled=True)
    if pages.message_ids(latest) != adapter.before:
        raise pages.NotStarted('发送前会话出现新消息；不跟进')
    adapter.greeting,adapter.attempted,adapter.attempted_at = text,True,time()
    adapter.message_submitted = True
    adapter.page.press(pages.SEND)
    adapter.page.wait_for(pages.MESSAGE_READY)
