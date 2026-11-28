"""The production-path guard treats a loaded unittest module as a test process.

Production entry points must therefore never import unittest, or every service
would refuse its own JOB_AGENT_HOME.
"""
import os
import subprocess
import sys
import unittest

ENTRY_POINTS = ('job_agent.cli', 'job_agent.server', 'job_agent.service.daily',
                'job_agent.monitor.runtime', 'job_agent.service.scoring',
                'job_agent.service.heartbeat', 'job_agent.browser.ego')


class ProductionImportsTests(unittest.TestCase):
    def test_entry_points_do_not_import_unittest(self):
        env = {k: v for k, v in os.environ.items() if k not in {'JOB_AGENT_TESTING'}}
        code = ('import sys\n' + ''.join(f'import {m}\n' for m in ENTRY_POINTS)
                + "print('unittest' in sys.modules)")
        out = subprocess.run([sys.executable, '-c', code], env=env, capture_output=True,
                             text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr[-2000:])
        self.assertEqual(out.stdout.strip().splitlines()[-1], 'False')
