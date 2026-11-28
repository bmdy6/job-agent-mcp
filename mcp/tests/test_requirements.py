import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from job_agent.requirements import extract_requirements


class RequirementsTest(unittest.TestCase):
    def test_heading_and_next_section(self):
        section, sentences = extract_requirements(
            '岗位职责：\n维护系统。\n任职要求：\n1、熟悉 Python 3.10；善于协作。\n2. 理解 HTTP。\n福利待遇：\n五险。')
        self.assertEqual(section, 'requirements')
        self.assertEqual(sentences, ['熟悉 Python 3.10', '善于协作', '理解 HTTP'])

    def test_inline_markdown_and_multiple_sections(self):
        section, sentences = extract_requirements(
            '工作职责：维护系统；岗位要求：熟悉 SQL；理解事务。福利待遇：午餐。\n'
            '### 职位要求\n- 能阅读文档！\n### 关于我们\n团队介绍。')
        self.assertEqual((section, sentences), ('requirements', ['熟悉 SQL', '理解事务', '能阅读文档']))

    def test_english_and_newline_sentences(self):
        self.assertEqual(extract_requirements('Responsibilities:\nBuild.\nRequirements:\n- Know C++.\n- Communicate well.\nBenefits:\nLunch.'),
                         ('requirements', ['Know C++', 'Communicate well']))

    def test_fallback_preserves_full_jd(self):
        self.assertEqual(extract_requirements('建设平台。\n欢迎加入！'), ('full', ['建设平台', '欢迎加入']))
        self.assertEqual(extract_requirements('任职要求：\n福利待遇：午餐。')[0], 'full')

    def test_empty_and_embedded_phrase(self):
        self.assertEqual(extract_requirements(''), ('full', []))
        self.assertEqual(extract_requirements('需要沟通任职要求相关事项。')[0], 'full')

    def test_html_and_numbered_heading(self):
        self.assertEqual(extract_requirements('<p>一、任职要求：</p><p>1）熟悉 Go。</p><p>二、福利待遇：</p><p>午餐。</p>'),
                         ('requirements', ['熟悉 Go']))
