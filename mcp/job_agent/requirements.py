"""Deterministic JD section extraction, with no model or skill taxonomy."""

import re
from html import unescape
from html.parser import HTMLParser

REQUIREMENTS = r'任职要求|岗位要求|职位要求|任职资格|任职条件|岗位资格|应聘要求|招聘要求|Requirements|Qualifications'
OTHER_SECTIONS = (r'岗位职责|工作职责|职位职责|职位描述|工作内容|岗位描述|职责描述|福利待遇|薪资待遇|薪酬福利|'
                  r'薪酬待遇|公司简介|公司介绍|关于我们|工作地点|联系方式|加分项|优先条件|'
                  r'Responsibilities|Benefits|About us|Nice to have')
# A heading begins a line/section, or follows punctuation in an inline JD.
# Require a colon or line end, avoiding ordinary prose mentioning 任职要求.
HEADING = re.compile(
    rf'(?:^|(?<=[\n。；;]))[ \t#*【\[]*(?:[一二三四五六七八九十\d]+[、.．）)\s]+)?'
    rf'(?P<name>{REQUIREMENTS}|{OTHER_SECTIONS})[ \t*】\]]*(?:[:：]|(?=\n|$))',
    re.IGNORECASE | re.MULTILINE,
)
BULLET = re.compile(r'^\s*(?:[-*•●▪]+\s*|[（(]?[\d一二三四五六七八九十]+[、.．）)]\s*)')


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in {'br', 'p', 'div', 'li', 'h1', 'h2', 'h3', 'h4'}:
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in {'p', 'div', 'li', 'h1', 'h2', 'h3', 'h4'}:
            self.parts.append('\n')

    def handle_data(self, data):
        self.parts.append(data)


def sentences(text: str) -> list[str]:
    result = []
    for line in text.splitlines():
        line = BULLET.sub('', line.strip())
        # Keep decimals, version numbers, URLs and abbreviations intact. An
        # English full stop is a boundary only before whitespace or line end.
        for part in re.split(r'[。！？!?；;]+|\.(?=\s|$)', line):
            part = part.strip()
            if part:
                result.append(part)
    return result


def extract_requirements(jd: str) -> tuple[str, list[str]]:
    text = jd.replace('\r\n', '\n').replace('\r', '\n')
    if re.search(r'</?(?:p|br|div|li|h[1-6])\b', text, re.IGNORECASE):
        parser = PlainText()
        parser.feed(text)
        text = ''.join(parser.parts)
    text = unescape(text)
    headings = list(HEADING.finditer(text))
    blocks = []
    for i, heading in enumerate(headings):
        if re.fullmatch(REQUIREMENTS, heading['name'], re.IGNORECASE):
            end = headings[i + 1].start() if i + 1 < len(headings) else len(text)
            blocks.extend(sentences(text[heading.end():end]))
    return ('requirements', blocks) if blocks else ('full', sentences(text))
