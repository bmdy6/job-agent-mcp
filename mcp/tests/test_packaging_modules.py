"""Namespace and public MCP contracts are installation boundaries."""
import inspect
import json
import unittest
from pathlib import Path
import job_agent.server as server

ROOT = Path(__file__).resolve().parents[1]

class PackagingTest(unittest.IsolatedAsyncioTestCase):
    def test_only_owned_namespace_at_project_root(self):
        # tests is a source-only safety bootstrap, excluded by pyproject.toml.
        # Only the legacy MCP entry script (not packaged; see server.py docstring).
        self.assertEqual([p.name for p in ROOT.glob("*.py")], ["server.py"])
        packages = {p.name for p in ROOT.iterdir() if (p / "__init__.py").is_file()}
        self.assertEqual(packages, {"job_agent", "tests"})

    async def test_mcp_tool_contract_unchanged(self):
        expected = json.loads((ROOT / "tests/fixtures/mcp-tools.json").read_text())
        actual = [t.model_dump(mode="json") for t in await server.mcp.list_tools()]
        # Python versions preserve different indentation in compiled docstrings.
        # Compare the content after dedent; input/output schemas remain exact.
        for tools in (actual, expected):
            for tool in tools:
                tool['description'] = inspect.cleandoc(tool['description'])
        self.assertEqual(actual, expected)
