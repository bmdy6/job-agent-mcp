"""Emit synthetic public projections; invoked against v6 and v7 package roots."""
import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from job_agent.config import job_agent_home
from job_agent.browser.base import FakeBrowser
from job_agent.platforms.session import SessionResult
from job_agent.service import read_tools, dashboard
from job_agent.service.monitor import status as monitor_status


class FixedClock(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime(2026,11,28,12,tzinfo=timezone.utc).astimezone(tz)


def emit():
    browser=FakeBrowser();browser.executable='/nonexistent/synthetic-ego'
    with patch('job_agent.service.read_tools.datetime',FixedClock), \
         patch('job_agent.browser.ego.EgoBrowser',return_value=browser), \
         patch('job_agent.platforms.session.check_session',return_value=SessionResult('boss',True,'',True,'professional')), \
         patch('job_agent.service.scoring.setup_checks',return_value={'scoring_credentials':{'ok':False},'resume':{'ok':False}}), \
         patch('job_agent.service.deployment.status',return_value={'executing_commit':'synthetic'}):
        state={**read_tools.status(),'monitor':monitor_status(),'deployment':{'executing_commit':'synthetic'}}
        # New #103 config field is additive; assert its default independently,
        # while this v6/v7 regression compares the shared public fields.
        age=state.get('job_agent',{}).get('engine',{}).pop('max_job_age_hours',48.0)
        assert age==48.0
        funnel=read_tools.funnel_stats()
        funnel.pop('as_of',None)  # observation clock; no business field is normalized
        checks=read_tools.setup_checks()
        with read_tools.snapshot() as store:
            data=dashboard.collect(store,read_tools.load_config(),job_agent_home(),now=datetime(2026,11,28,12,tzinfo=timezone.utc))
        from job_agent.dashboard_html import render
        return {'status':state,'funnel_stats':funnel,'setup_check':checks,'dashboard':data,'dashboard_html':render(data)}


if __name__=='__main__':
    print(json.dumps(emit(),ensure_ascii=False,default=str,sort_keys=True))
