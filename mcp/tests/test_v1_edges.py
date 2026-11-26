import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
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
