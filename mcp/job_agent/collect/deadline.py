"""Hard per-platform deadline for the synchronous CLI worker (POSIX main thread)."""
from contextlib import contextmanager
from contextvars import ContextVar
import signal
import threading
import time
from job_agent.browser.base import BrowserError

_deadline=ContextVar('collection_deadline',default=None)


class CollectionTimeout(BrowserError):
    code='collection_timeout'


def remaining():
    end=_deadline.get()
    if end is None:return None
    left=end-time.monotonic()
    if left<=0:raise CollectionTimeout('Platform collection timed out')
    return left


@contextmanager
def platform_deadline(seconds):
    # MCP collection already launches cli.collect in a separate main-thread process.
    # Refuse an unbounded worker rather than silently starting uncancellable threads.
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError('Collection requires a main-thread worker for its hard deadline')
    old_handler=signal.getsignal(signal.SIGALRM)
    old_timer=signal.getitimer(signal.ITIMER_REAL)
    if old_timer[0]:raise RuntimeError('Collection cannot replace an active alarm')
    token=_deadline.set(time.monotonic()+seconds)
    def expire(*_):raise CollectionTimeout('Platform collection timed out')
    signal.signal(signal.SIGALRM,expire)
    signal.setitimer(signal.ITIMER_REAL,seconds)
    try:
        yield
        remaining()
    finally:
        signal.setitimer(signal.ITIMER_REAL,0)
        signal.signal(signal.SIGALRM,old_handler)
        _deadline.reset(token)
