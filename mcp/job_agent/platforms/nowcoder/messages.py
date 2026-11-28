"""Recruitment API contracts from official notice bundle; populated fixtures synthetic."""
import math
from job_agent.platforms.base import ConversationSummary, ReplyEvent
from . import pages, flow


def roster_js(page=1):
    return r'''async () => { /* nowcoder.roster */
 const r=await fetch('/np-api/u/msg/get-conversations',{method:'POST',
 headers:{'Content-Type':'application/x-www-form-urlencoded'},body:'page=PAGE&pageSize=15'});
 const d=await r.json();return {status:r.status,...d};
}'''.replace('PAGE',str(page))


def roster(data,page=1):
    d=flow.read_api(data)
    if (not isinstance(d,dict) or not isinstance(d.get('datas'),list)
        or type(d.get('totalPage')) is not int or not 0<=d['totalPage']<=20
        or type(d.get('totalCount')) is not int or d['totalCount']<0
        or d.get('currentPage',page)!=page):raise pages.Unverified('牛客会话分页结构不可读')
    if len(d['datas'])>15 or (d['datas'] and (d['totalPage']<1 or d['totalCount']<len(d['datas']))):
        raise pages.Unverified('牛客会话分页数量不一致')
    if not d['datas'] and (d['totalCount']!=0 or d['totalPage']!=0):
        raise pages.Unverified('牛客会话分页不完整')
    return d


def summary(row):
    if not isinstance(row,dict):raise pages.Unverified('牛客会话结构不可读')
    # Recommendation pseudo-conversations have no recruitment message identity.
    if row.get('apiJobAssistantMsgJumpUrl'):return None
    latest=row.get('latestMsg')
    if (not flow.identifier(row.get('id')) or not flow.identifier(row.get('jobId'))
        or not isinstance(latest,dict) or not flow.identifier(latest.get('id'))
        or type(row.get('unreadCount')) is not int or row['unreadCount']<0):
        raise pages.Unverified('牛客会话稳定 ID、岗位或未读数缺失')
    stamp=latest.get('createTime')
    if type(stamp) not in (int,float) or not math.isfinite(stamp) or stamp<=0:
        raise pages.Unverified('牛客会话时间缺失')
    return ConversationSummary(str(row['id']),str(latest['id']),row['unreadCount'],stamp/1000)


def messages_js(conversation_id):
    if not flow.identifier(conversation_id):raise ValueError('Invalid conversation ID')
    return r'''async () => { /* nowcoder.messages */
 const r=await fetch('/np-api/u/msg/common/get-message-list',{method:'POST',
 headers:{'Content-Type':'application/x-www-form-urlencoded'},
 body:'conversationId=CID&pageSize=20&preId=0'});
 const d=await r.json();
 const uid=document.querySelector('.js-nowcoder-header')?.__vue__?.userInfo?.userId;
 return {status:r.status,...d,owner_id:uid,conversation_id:'CID'};
}'''.replace('CID',str(conversation_id))


def events(data,row,since=None):
    d=flow.read_api(data)
    s=summary(row)
    if (not s or str(data.get('conversation_id'))!=s.conversation_id
        or not flow.identifier(data.get('owner_id')) or not flow.identifier(row.get('hrId'))
        or not isinstance(d,dict) or not isinstance(d.get('messageList'),list)):
        raise pages.Unverified('牛客消息会话或方向无法关联')
    messages=d['messageList']
    if not any(str(m.get('id'))==s.last_message_id for m in messages if isinstance(m,dict)):
        raise pages.Unverified('牛客最新消息尚未读到，保留游标')
    out=[];seen=set()
    for m in messages:
        if not isinstance(m,dict) or not flow.identifier(m.get('id')):raise pages.Unverified('牛客消息 ID 缺失')
        mid=str(m['id'])
        if mid in seen:raise pages.Unverified('牛客消息 ID 重复')
        seen.add(mid)
        if m.get('conversationId') is not None and str(m['conversationId'])!=s.conversation_id:
            raise pages.Unverified('牛客消息属于其他会话')
        sender=m.get('sender');stamp=m.get('createTime');body=m.get('content')
        if (not flow.identifier(sender) or str(sender) not in {str(row['hrId']),str(data['owner_id'])}
            or type(stamp) not in (int,float) or not math.isfinite(stamp) or stamp<=0
            or not isinstance(body,str)):
            raise pages.Unverified('牛客消息方向、时间或正文类型待核验')
        if since is not None and stamp/1000<=since:continue
        out.append(ReplyEvent('nowcoder',s.conversation_id+':'+mid,str(row['jobId']),
            '',row.get('jobName',''),'outgoing' if str(sender)==str(data['owner_id']) else 'incoming',
            body,stamp/1000,'https://www.nowcoder.com/users/notice/jobAssistant?id='+s.conversation_id))
    return out
