"""Read-only signal tools. Never migrate, call models or contact platforms."""
from job_agent.config import load_config
from job_agent.store import signals as sql
from job_agent.service.read_tools import snapshot


def greeting_stats():
    with snapshot() as store:
        return {'backend':'own',**sql.greeting_statistics(store,min_samples=load_config().analytics.greeting_min_samples)}


def interview_schedule():
    with snapshot() as store:
        items=sql.invitations(store)
    return {'backend':'own','count':len(items),'items':items,
            'semantics':'面试邀约原文，均待用户确认；相对时间未转成日期，不自动接受或创建日历事件。'}
