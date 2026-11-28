"""Process logging: a size-rotated file per command, and only warnings on stderr.

launchd redirects stderr to a file it never rotates, so INFO lines go to
$JOB_AGENT_HOME/logs/<command>.log through a RotatingFileHandler instead.
"""
import logging
import sys
from logging.handlers import RotatingFileHandler


def setup_logging(command, *, home=None, max_bytes=5_000_000, backups=5):
    from job_agent.observability import install_log_context
    install_log_context()
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for handler in list(root.handlers):
        root.removeHandler(handler)
    fmt = logging.Formatter('%(asctime)s %(levelname)s %(name)s run_id=%(run_id)s platform=%(platform)s stage=%(stage)s: %(message)s')
    stderr = logging.StreamHandler(sys.stderr)
    stderr.setFormatter(fmt)
    if home is None:
        stderr.setLevel(logging.INFO)
        root.addHandler(stderr)
        return None
    folder = home / 'logs'
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f'{command or "job-agent"}.log'
    handler = RotatingFileHandler(path, maxBytes=max_bytes, backupCount=backups, encoding='utf-8')
    handler.setFormatter(fmt)
    root.addHandler(handler)
    stderr.setLevel(logging.WARNING)
    root.addHandler(stderr)
    return path
