"""Read-only recruitment roster probe. Populated message schemas await live evidence.

Never navigate jobAssistant: its own mounted hook selects the first conversation,
which would violate #57's first-run baseline rule. Use the verified roster read API.
"""
from . import pages

ROSTER_JS = r'''async () => { /* nowcoder.roster */
 const r=await fetch('/np-api/u/msg/get-conversations',{method:'POST',
 headers:{'Content-Type':'application/x-www-form-urlencoded'},body:'page=1&pageSize=15'});
 const d=await r.json();return {status:r.status,code:d.code,data:d.data,risk:/验证码|安全验证|操作频繁|账号异常|访问异常/.test(d.msg||'')};
}'''

class NowcoderReplySource:
    platform='nowcoder'

    def fetch(self,space,*,since=None,cursor=None):
        page=space.new_page()
        try:
            page.goto('https://www.nowcoder.com/jobs/school/jobs',wait_ms=15000)
            state=page.evaluate(pages.GUARD_JS)
            pages.guard(state)
            if state.get('logged_in') is not True:raise pages.LoginRequired('牛客登录未确认')
            data=page.evaluate(ROSTER_JS)
            if not isinstance(data,dict):raise pages.Unverified('牛客会话摘要不可读')
            if data.get('code')==1125 or data.get('risk'):raise pages.RiskControl('牛客接口要求验证码')
            payload=data.get('data')
            if data.get('status')!=200 or data.get('code')!=0 or not isinstance(payload,dict):
                raise pages.Unverified('牛客招聘会话接口失败')
            # Only a complete, positive empty roster is currently live verified.
            if payload.get('datas')==[] and payload.get('totalCount')==0 and payload.get('totalPage')==0:
                return []
            raise pages.Unverified('牛客已有招聘会话：摘要和消息关联待实测，未打开会话')
        finally:page.close()

ReplySource=NowcoderReplySource
