import asyncio
import json
import os
import sys
import threading
import tempfile
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class StdioTest(unittest.IsolatedAsyncioTestCase):
    async def test_tools_preserve_filters_and_scope(self):
        requests = []
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                url = urlsplit(self.path)
                requests.append(("GET", url.path, parse_qs(url.query)))
                self.reply({"items": [{"id": "example:1", "title": "AI 应用",
                                        "hr_name": "private", "jd": "long text"}],
                            "total": 1, "limit": 15, "offset": 0})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append(("POST", self.path, body))
                if self.path == "/api/workbench/preflight":
                    self.reply({"ok": True})
                elif self.path == "/api/workbench/task":
                    self.reply({"id": "example-task", "status": "running"})
                elif self.path == "/api/scoring/preview":
                    self.reply({"eligible_jobs": 1, "job_ids": ["example:1"]})
                else:
                    self.reply({"run": {"id": "example-run", "status": "running"},
                                "task": {"id": "score-task", "status": "running"}})

            def reply(self, data):
                body = json.dumps(data).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        api = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=api.serve_forever, daemon=True)
        worker.start()
        server = StdioServerParameters(
            command=sys.executable,
            args=[str(Path(__file__).resolve().parents[1] / "server.py")],
            env={**os.environ, "BOSSHUNTER_PORT": str(api.server_port),
                 "JOB_AGENT_HOME": home.name, "JOB_AGENT_CALLER": "unit-stdio"},
        )
        try:
            async with stdio_client(server) as (reader, writer):
                async with ClientSession(reader, writer) as client:
                    await client.initialize()
                    names = {tool.name for tool in (await client.list_tools()).tools}
                    self.assertEqual(names, {"status", "resume_delivery_status", "review_resume_application_record", "search_jobs", "get_job", "check_collection",
                                             "start_collection", "preview_scoring", "start_scoring",
                                             "scoring_runs", "jd_requirements", "profile", "experience_list",
                                             "experience_get", "experience_add", "company_get", "company_save",
                                             "log_event", "company_history", "funnel", "pending_evaluations",
                                             "submit_evaluations", "set_greeting", "setup_check", "resume_import", "briefing", "funnel_stats", "greeting_targets", "preview_greetings",
                                             "send_greetings", "delivery_status", "stop_delivery",
                                             "start_reply_monitor", "stop_reply_monitor", "reply_monitor_status", "notify_test", "greeting_stats", "interview_schedule"})
                    self.assertNotIn("send", names)
                    for tool in ("greeting_stats", "interview_schedule"):
                        signal = await client.call_tool(tool, {})
                        self.assertFalse(signal.isError)
                        self.assertFalse(signal.structuredContent["supported"])

                    result = await client.call_tool("search_jobs", {
                        "query": "Agent", "cities": ["苏州", "上海"], "salary_min": 10,
                        "salary_max": 14, "platforms": ["boss"], "statuses": ["pending"],
                    })
                    self.assertFalse(result.isError)
                    self.assertNotIn("hr_name", result.structuredContent["items"][0])
                    self.assertNotIn("jd", result.structuredContent["items"][0])
                    rejected = await client.call_tool("start_scoring", {"job_ids": ["example:1"]})
                    self.assertTrue(rejected.isError)
                    rejected = await client.call_tool("start_collection", {
                        "platforms": ["boss"], "confirm": "true",
                    })
                    self.assertTrue(rejected.isError)
                    self.assertEqual(len(requests), 1)
                    preview = await client.call_tool("preview_scoring", {"job_ids": ["example:1"]})
                    self.assertFalse(preview.isError, preview.content)
                    self.assertEqual(preview.structuredContent["eligible_jobs"], 1)
                    run = await client.call_tool("start_scoring", {
                        "job_ids": ["example:1"], "confirm": True,
                    })
                    self.assertFalse(run.isError, run.content)
                    task = await client.call_tool("start_collection", {
                        "platforms": ["boss"], "confirm": True,
                    })
                    self.assertFalse(task.isError, task.content)
        finally:
            await asyncio.to_thread(api.shutdown)
            api.server_close()
            worker.join()
        self.assertEqual(requests[0], ("GET", "/api/jobs/search", {
            "limit": ["15"], "offset": ["0"], "sort_by": ["created_at"], "q": ["Agent"],
            "city": ["苏州", "上海"], "source_platform": ["boss"], "salary_min": ["10.0"],
            "salary_max": ["14.0"], "status": ["pending"],
        }))
        selection = {"scope": "selected", "job_ids": ["example:1"]}
        self.assertEqual(requests[1], ("POST", "/api/scoring/preview", selection))
        self.assertEqual(requests[2], ("POST", "/api/scoring/start", selection))
        request = {"mode": "collect", "options": {"platform_order": ["boss"], "auto_score": False}}
        self.assertEqual(requests[3:], [
            ("POST", "/api/workbench/preflight", request),
            ("POST", "/api/workbench/task", request),
        ])

        entries = [json.loads(line) for line in Path(home.name, "activity.jsonl").read_text().splitlines()]
        self.assertEqual(len(entries), 8)
        self.assertEqual([row["result"]["status"] for row in entries], ["ok", "ok", "ok", "error", "error", "ok", "ok", "ok"])
        self.assertTrue(all(row["caller"] == "unit-stdio" for row in entries))
