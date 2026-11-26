import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class PackagingTest(unittest.TestCase):
    def test_every_top_level_module_is_packaged(self):
        """Runtimes install the wheel, so a module missing from py-modules breaks production only."""
        listed = set(tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["setuptools"]["py-modules"])
        present = {p.stem for p in ROOT.glob("*.py")}
        self.assertEqual(sorted(present - listed), [])


if __name__ == "__main__":
    unittest.main()
