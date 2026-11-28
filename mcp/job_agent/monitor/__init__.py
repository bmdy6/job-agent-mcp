"""Reply monitoring; never sends messages to recruitment platforms."""
from job_agent.monitor.config import MonitorSettings
from job_agent.monitor.poller import Monitor

__all__ = ['Monitor', 'MonitorSettings']
