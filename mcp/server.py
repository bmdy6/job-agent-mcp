"""Compatibility entry point for MCP registrations that still run `mcp/server.py`.

Codex (~/.codex/config.toml), Claude Code on the Mac and remote Claude registrations
point here; new registrations should use `python -m job_agent.server`.
"""
import runpy

if __name__ == "__main__":
    runpy.run_module("job_agent.server", run_name="__main__", alter_sys=True)
