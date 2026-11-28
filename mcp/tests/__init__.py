"""Safety bootstrap for every package-qualified test, including a single module."""
import os
from pathlib import Path
import tempfile

os.environ['JOB_AGENT_TESTING'] = '1'
_home = os.environ.get('JOB_AGENT_HOME')
if not _home or Path(_home).expanduser().resolve() == (Path.home()/'.job-agent').resolve():
    # Keep the object alive for the entire test run; children inherit the safe path.
    _isolated_home = tempfile.TemporaryDirectory(prefix='job-agent-tests-')
    os.environ['JOB_AGENT_HOME'] = _isolated_home.name

# Legacy tests share fixtures via top-level sibling imports.
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
