"""Audit at the MCP dispatch boundary, including schema rejections.

Values and error messages are deliberately excluded: either may contain a JD,
resume, upstream response or other private text.
"""

import asyncio
import os
from datetime import datetime, timezone

from mcp.server.fastmcp import FastMCP

from job_agent.store import Store


def parameter_summary(arguments: dict) -> dict:
    # Do not persist arbitrary argument names from unknown/untrusted calls.
    known = {'query', 'cities', 'platforms', 'statuses', 'salary_min', 'salary_max',
             'min_score', 'sort_by', 'limit', 'offset', 'job_id', 'job_ids', 'confirm', 'since'}
    summary = {}
    for name, value in arguments.items():
        if name not in known:
            continue
        if isinstance(value, (bool, int, float)) or value is None:
            summary[name] = value
        elif isinstance(value, (list, dict)):
            summary[name] = {'count': len(value)}
        elif isinstance(value, str):
            summary[name] = {'characters': len(value)}
        else:
            summary[name] = {'type': type(value).__name__}
    return {'count': len(arguments), 'fields': summary}


def result_summary(result) -> dict:
    # FastMCP returns (content_blocks, structured_content) for typed tools.
    data = result[1] if isinstance(result, tuple) and len(result) == 2 else result
    summary = {'status': 'ok'}
    if isinstance(data, dict):
        for name in ('total', 'jobs', 'requirements_jobs', 'full_jobs', 'section_success_rate'):
            value = data.get(name)
            if isinstance(value, (int, float)):
                summary[name] = value
        for name in ('items', 'runs'):
            if isinstance(data.get(name), list):
                summary[name + '_count'] = len(data[name])
    return summary


class AuditedMCP(FastMCP):
    async def call_tool(self, name: str, arguments: dict):
        # Open before dispatch: unavailable audit storage blocks side effects.
        store = Store()
        record = {
            'time': datetime.now(timezone.utc).isoformat(),
            'tool': name if name in {tool.name for tool in await self.list_tools()} else 'unknown',
            'parameters': parameter_summary(arguments),
            'caller': os.environ.get('JOB_AGENT_CALLER') or 'unknown',
        }
        try:
            result = await super().call_tool(name, arguments)
            record['result'] = result_summary(result)
            return result
        except BaseException as exc:
            record['result'] = {
                'status': 'cancelled' if isinstance(exc, asyncio.CancelledError) else 'error',
                'error_type': type(exc).__name__,
            }
            raise
        finally:
            try:
                store.append_event(record)
            finally:
                store.close()
