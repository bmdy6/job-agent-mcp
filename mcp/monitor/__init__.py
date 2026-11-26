"""Reply monitoring; never sends messages to recruitment platforms."""
from .config import MonitorSettings
from .poller import Monitor

__all__ = ['Monitor', 'MonitorSettings']
