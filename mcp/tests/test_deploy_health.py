import importlib.util, sys, unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("deploy_health", ROOT/"scripts"/"deploy.py")
deploy = importlib.util.module_from_spec(spec); spec.loader.exec_module(deploy)


def state(**platforms):
    return {"state": "running", "result": {"failed": sum(v != "ok" and not v.endswith("_paused") for v in platforms.values()),
            "platforms": {k: {"status": v} for k, v in platforms.items()}}}


class DeployHealthTest(unittest.TestCase):
    def test_already_failing_platform_does_not_block_deploy(self):
        self.assertTrue(deploy.poll_healthy(state(boss="ok", liepin="receipt_unknown"), ["boss"]))

    def test_regression_of_a_healthy_platform_blocks_deploy(self):
        self.assertFalse(deploy.poll_healthy(state(boss="receipt_unknown", liepin="ok"), ["boss", "liepin"]))

    def test_paused_counts_as_healthy_and_no_baseline_requires_zero_failures(self):
        self.assertTrue(deploy.poll_healthy(state(boss="ok", job51="risk_control_paused"), ["boss", "job51"]))
        self.assertFalse(deploy.poll_healthy(state(boss="ok", liepin="receipt_unknown"), None))
        self.assertTrue(deploy.poll_healthy(state(boss="ok"), None))

    def test_busy_poll_is_never_healthy(self):
        busy = state(boss="ok"); busy["result"]["busy"] = True
        self.assertFalse(deploy.poll_healthy(busy, ["boss"]))


if __name__ == "__main__":
    unittest.main()
