import json
from http.client import BadStatusLine
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server
import v1
from applications import application_funnel
from config import load_config


class EdgesTest(unittest.TestCase):
    def test_empty_and_implied_funnel_and_stable_cohorts(self):
        empty = application_funnel([], [])
        self.assertIsNone(empty['total']['reply_rate'])
        self.assertIsNone(empty['total']['interview_rate'])
        events = [{'job_id': 'a', 'event': 'offer', 'variant': 'first'},
                  {'job_id': 'a', 'event': 'greeted', 'variant': 'later'},
                  {'job_id': 'a', 'event': 'offer'}, {'job_id': 'b', 'event': 'rejected'}]
        result = application_funnel(events, [], 'variant')
        self.assertEqual(result['total'], {'greeted': 1, 'replied': 1, 'reply_rate': 1,
                                          'interview_scheduled': 1, 'interview_rate': 1,
                                          'interviewed': 1, 'offers': 1})
        self.assertEqual([g['key'] for g in result['groups']], ['first', 'unknown'])
        jobs = [{'id': 'a', 'status': 'resume_sent', 'source_platform': 'liepin'},
                {'id': 'b', 'status': 'sent', 'source_platform': 'zhilian'},
                {'id': 'c', 'status': 'ready'}]
        result = application_funnel(events, jobs, 'platform')
        self.assertEqual(result['total']['greeted'], 2)
        self.assertEqual(result['total']['offers'], 1)
        self.assertEqual([g['key'] for g in result['groups']], ['liepin', 'zhilian'])

    def test_pdf_text_and_scanned_pdf_and_docx_paragraph_table_order(self):
        from pypdf import PdfWriter
        from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
        from docx import Document
        with tempfile.TemporaryDirectory() as home:
            path = Path(home, 'synthetic.pdf')
            writer = PdfWriter()
            page = writer.add_blank_page(612, 792)
            font = DictionaryObject({NameObject('/Type'): NameObject('/Font'),
                                     NameObject('/Subtype'): NameObject('/Type1'),
                                     NameObject('/BaseFont'): NameObject('/Helvetica')})
            page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
            stream = DecodedStreamObject()
            stream.set_data(b'BT /F1 12 Tf 72 720 Td (Synthetic resume evidence) Tj ET')
            page[NameObject('/Contents')] = writer._add_object(stream)
            writer.write(path)
            self.assertIn('Synthetic resume evidence', v1.import_resume(str(path))['text'])
            writer = PdfWriter()
            writer.add_blank_page(612, 792)
            writer.write(path)
            self.assertIn('OCR', v1.import_resume(str(path))['warnings'][0])
            document = Document()
            document.add_paragraph('前文')
            table = document.add_table(rows=1, cols=2)
            table.cell(0, 0).text = '表格经历'
            table.cell(0, 1).text = '证据'
            document.add_paragraph('后文')
            path = Path(home, 'synthetic.docx')
            document.save(path)
            self.assertEqual(v1.import_resume(str(path))['text'], '前文\n表格经历\t证据\n后文')
            self.assertEqual(sorted(p.name for p in Path(home).iterdir()), ['synthetic.docx', 'synthetic.pdf'])

    def test_config_modes_caps_and_remote_cdp_rejected(self):
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {'JOB_AGENT_HOME': home}, clear=True):
            path = Path(home, 'config.yaml')
            for content in ['mode: unknown', 'practice: {max_interviews_per_week: -1}',
                            'practice: {max_interviews_per_week: true}', 'stretch_salary_min: -1',
                            'stretch_salary_min: .inf', 'browser: {cdp_url: http://example.invalid:9222}',
                            'browser: {cdp_url: http://user:pass@localhost:9222}']:
                path.write_text(content)
                with self.subTest(content=content), self.assertRaises(ValueError):
                    load_config()
            path.write_text('mode: practice\npractice: {max_interviews_per_week: 0}\nstretch_salary_min: 25')
            self.assertEqual(load_config().practice.max_interviews_per_week, 0)

    def test_cdp_invalid_http_is_reported_as_missing(self):
        with patch.object(v1, 'HTTPConnection') as connection:
            connection.return_value.getresponse.side_effect = BadStatusLine('invalid HTTP')
            self.assertFalse(v1.check_cdp('http://127.0.0.1:9222')['ok'])
            connection.return_value.close.assert_called_once()

    def test_cdp_get_only_and_never_follow_redirects(self):
        requests = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_GET(self):
                requests.append(self.path)
                body = json.dumps({'Browser': 'Synthetic'}).encode()
                self.send_response(200 if len(requests) == 1 else 302)
                self.send_header('Location', 'http://example.invalid/never-follow')
                self.end_headers()
                self.wfile.write(body)
        service = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        worker = threading.Thread(target=service.serve_forever, daemon=True)
        worker.start()
        try:
            self.assertTrue(v1.check_cdp(f'ws://127.0.0.1:{service.server_port}/devtools/browser/test')['ok'])
            self.assertFalse(v1.check_cdp(f'http://127.0.0.1:{service.server_port}')['ok'])
            self.assertEqual(requests, ['/json/version', '/json/version'])
        finally:
            service.shutdown()
            service.server_close()
            worker.join()


class ErrorTest(unittest.IsolatedAsyncioTestCase):
    async def test_pending_limit_matches_upstream_contract(self):
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {'JOB_AGENT_HOME': home}, clear=True):
            with patch.object(server, 'api') as api:
                for limit in [0, 11]:
                    with self.assertRaises(Exception):
                        await server.mcp.call_tool('pending_evaluations', {'limit': limit})
                api.assert_not_called()

    async def test_pagination_nonadvancing_or_truncated_fails_instead_of_partial_totals(self):
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {'JOB_AGENT_HOME': home}, clear=True):
            for pages in [[{'total': 2, 'items': [{'id': 'a', 'status': 'sent'}]}]*2,
                          [{'total': 2, 'items': [{'id': 'a', 'status': 'sent'}]}, {'total': 2, 'items': []}]]:
                with patch.object(server, 'api', side_effect=pages), self.assertRaises(Exception):
                    await server.mcp.call_tool('funnel', {})

    async def test_upstream_unavailable_setup_returns_all_checks_and_reads_fail(self):
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {'JOB_AGENT_HOME': home}, clear=True):
            with patch.object(server, 'api', side_effect=RuntimeError('offline')), patch.object(v1, 'check_cdp', return_value={'ok': False}):
                for tool in ['briefing', 'funnel', 'company_history', 'pending_evaluations', 'profile']:
                    with self.subTest(tool=tool), self.assertRaises(Exception):
                        await server.mcp.call_tool(tool, {'company': 'acme'} if tool == 'company_history' else {})
                result = (await server.mcp.call_tool('setup_check', {}))[1]
                self.assertEqual(set(result['missing']), {'config', 'bosshunter', 'resume', 'experience', 'browser_cdp'})

    async def test_relative_resume_uses_bosshunter_base_not_process_cwd(self):
        def api(path, body=None):
            if path == '/api/config':
                return {'profile': {'resume_path': './resume.md'}}
            if path == '/api/agent/onboarding':
                return {'missing': []}
            return {'preferences': {}}
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {'JOB_AGENT_HOME': home}, clear=True):
            with patch.object(server, 'api', side_effect=api), patch.object(v1, 'check_cdp', return_value={'ok': True}):
                result = (await server.mcp.call_tool('setup_check', {}))[1]
                self.assertTrue(result['checks']['resume']['ok'])
                self.assertEqual(result['checks']['resume']['checked_by'], 'bosshunter_onboarding')
