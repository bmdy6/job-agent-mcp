"""Roster first; read only changed conversations, never mount jobAssistant."""
from . import pages, messages

ROSTER_JS=messages.roster_js()

class NowcoderReplySource:
    platform='nowcoder'

    def fetch(self,space,*,since=None,cursor=None):
        page=space.new_page()
        try:
            page.goto('https://www.nowcoder.com/jobs/school/jobs',wait_ms=15000)
            state=page.evaluate(pages.GUARD_JS)
            pages.guard(state)
            if state.get('logged_in') is not True:raise pages.LoginRequired('牛客登录未确认')
            first=messages.roster(page.evaluate(ROSTER_JS))
            rows=list(first['datas'])
            for number in range(2,first['totalPage']+1):
                data=messages.roster(page.evaluate(messages.roster_js(number)),number)
                if (data['totalCount'],data['totalPage'])!=(first['totalCount'],first['totalPage']):
                    raise pages.Unverified('牛客会话分页变化；保留游标')
                rows.extend(data['datas'])
            if len(rows)!=first['totalCount']:raise pages.Unverified('牛客会话列表不完整')
            summaries=[messages.summary(row) for row in rows]
            ids=[s.conversation_id for s in summaries if s]
            if len(ids)!=len(set(ids)):raise pages.Unverified('牛客会话重复')
            result=[]
            for row,summary in zip(rows,summaries):
                if summary is None:continue
                # A cursor is required: never open every session on an untracked poll.
                if cursor is None:raise pages.Unverified('牛客非空会话监听需要持久化游标')
                if not cursor.select(summary):continue
                result.extend(messages.events(page.evaluate(messages.messages_js(summary.conversation_id)),row,since))
                cursor.acknowledge(summary)
            return result
        finally:page.close()

ReplySource=NowcoderReplySource
