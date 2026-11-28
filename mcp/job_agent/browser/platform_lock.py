"""Cross-process platform ownership, shared by every automation workflow.

Nonblocking: a busy platform is deferred before any website access. flock is
released by the kernel on process exit; lock files must never be unlinked.
"""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
from job_agent.browser.base import BrowserError


class PlatformBusy(BrowserError):
    code = 'platform_busy'


@contextmanager
def platform_lock(platform, *, home=None, existing_only=False):
    if platform not in {'boss', 'zhilian', 'liepin', '51job', 'nowcoder', 'maimai'}:
        raise ValueError('Unknown platform')
    home = Path(home or os.environ.get('JOB_AGENT_HOME', '~/.job-agent')).expanduser().resolve()
    if not existing_only:
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Read-only audit joins the SAME inode without creating or editing production files.
    fd = os.open(home / (platform + '.automation.lock'), os.O_RDONLY if existing_only else os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise PlatformBusy('Platform automation already running') from None
        yield fd
    finally:
        os.close(fd)
