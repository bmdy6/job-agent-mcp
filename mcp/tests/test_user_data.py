import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import user_data as data


class UserDataTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ, {'JOB_AGENT_HOME': self.temp.name}, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def test_experience_roundtrip_unique_and_empty(self):
        self.assertEqual(data.experience_list(), [])
        first = data.experience_add('项目 A', '# 原文\nUnicode 你好\n')
        second = data.experience_add('项目 A', '另一段')
        self.assertNotEqual(first['id'], second['id'])
        self.assertTrue(first['id'].startswith('项目-a-'))
        self.assertEqual(data.experience_get(first['id'])['md'], '# 原文\nUnicode 你好\n')
        self.assertEqual(len(data.experience_list()), 2)
        self.assertEqual(Path(self.temp.name, 'experience', first['id']+'.md').stat().st_mode & 0o777, 0o600)
        self.assertIsNone(data.experience_get('missing'))
        for title, md in [('', 'x'), ('a\nb', 'x'), ('x', '  ')]:
            with self.assertRaises(ValueError):
                data.experience_add(title, md)
        with self.assertRaises(ValueError):
            data.experience_get('../config')

    def test_long_unicode_titles_generate_portable_unique_ids(self):
        entry = data.experience_add('长标题' * 80, '经历原文')
        self.assertEqual(data.experience_get(entry['id'])['md'], '经历原文')
        self.assertLessEqual(len((entry['id']+'.md').encode('utf-8')), 255)

    def test_normalization_company_cache_and_validation(self):
        for name in ['ＡＣＭＥ（杭州）科技股份有限公司', ' acme 科技有限公司 ', 'ACME股份']:
            self.assertEqual(data.company_key(name), 'acme')
        self.assertIsNone(data.company_get('ACME'))
        result = data.company_save('ＡＣＭＥ（杭州）科技股份有限公司', '合成内容', 'https://example.invalid/company')
        self.assertEqual(data.company_get('acme')['md'], '合成内容')
        self.assertTrue(result['updated_at'])
        data.company_save('ACME有限公司', '新内容', 'https://example.invalid/new')
        self.assertEqual(len(list(Path(self.temp.name, 'companies').iterdir())), 1)
        self.assertEqual(data.company_get('acme')['source_url'], 'https://example.invalid/new')
        for name, md, url in [('', 'x', 'https://example.invalid'), ('有限公司', 'x', 'https://example.invalid'),
                               ('acme', '', 'https://example.invalid'), ('acme', 'x', 'file:///etc/passwd')]:
            with self.assertRaises(ValueError):
                data.company_save(name, md, url)

    def test_append_and_corrupt_events(self):
        self.assertEqual(data.read_events(), [])
        data.append_event({'job_id': 'example:1', 'event': 'greeted'})
        data.append_event({'job_id': 'example:1', 'event': 'replied'})
        self.assertEqual(len(data.read_events()), 2)
        path = Path(self.temp.name, 'applications.jsonl')
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        path.write_text(path.read_text()+'bad json\n')
        with self.assertRaisesRegex(ValueError, 'line 3'):
            data.read_events()
        path.write_text('[]\n')
        with self.assertRaises(ValueError):
            data.read_events()

    def test_paths_cannot_escape_home(self):
        with tempfile.TemporaryDirectory() as other:
            Path(self.temp.name, 'experience').symlink_to(other, target_is_directory=True)
            with self.assertRaises(ValueError):
                data.experience_add('safe', 'text')
            Path(self.temp.name, 'applications.jsonl').symlink_to(Path(other, 'events'))
            with self.assertRaises(ValueError):
                data.append_event({'event': 'greeted'})
        # Punctuation in company names cannot become path separators.
        data.company_save('../a/b', 'x', 'https://example.invalid')
        self.assertEqual(data.company_get('../a/b')['md'], 'x')
