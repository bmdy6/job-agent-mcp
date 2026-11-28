"""Platform-owned newline declarations; pure text preparation shared by all consumers."""
from hashlib import sha256
from importlib import import_module
import re
import unicodedata


def prepare_greeting(platform: str, text: str, separator: str = '；', *, paragraph_separator=None) -> str:
    if platform not in {'boss', 'liepin', 'zhilian', '51job', 'nowcoder', 'maimai'}:
        raise ValueError('Unknown greeting platform')
    if separator not in {'；', ' '}:
        raise ValueError('Greeting newline separator must be semicolon or space')
    name = 'job51' if platform == '51job' else platform
    mode = import_module(f'job_agent.platforms.{name}.pages').GREETING_NEWLINE
    text = text.replace('\r\n', '\n').replace('\r', '\n').replace('\u2028', '\n').replace('\u2029', '\n')
    if paragraph_separator is not None:
        if paragraph_separator not in {'\n\n', ''}:
            raise ValueError('Invalid greeting paragraph separator')
        paragraphs = re.split(r'\n[ \t]*\n', text.strip())
        if len(paragraphs) not in {3, 4}:
            return text if mode != 'reject' else text.replace('\n', separator)
        if paragraph_separator == '':
            # Sentence punctuation joins paragraphs; never introduce semicolons.
            return ''.join(p if p.endswith(('。', '？', '！', '?', '!')) else p+'。' for p in paragraphs)
        if platform in {'boss', 'zhilian'}:
            return '\n\n'.join(paragraphs)
        # Other unverified message editors retain their existing safe flattening.
    return text if mode != 'reject' else text.replace('\n', separator)


def greeting_version(text: str) -> str:
    """Also works on historical deliveries.greeting; no migration or guessed labels."""
    return 'sha256:' + sha256(text.encode('utf-8')).hexdigest()


def same_text(actual, expected):
    """Compare a sent message with the intended greeting.

    Chat pages render line breaks as <br> or block elements, so textContent
    drops them; whitespace is ignored on both sides, everything else must match.
    """
    if not isinstance(actual, str) or not isinstance(expected, str) or not expected.strip():
        return False
    squash = lambda text: ''.join(text.split())
    return squash(actual) == squash(expected)


AI_CLICHE_TERMS = (
    '看了贵公司',
    '非常符合',
    '十分契合',
    '希望有机会',
    '期待您的回复',
    '我相信我能',
    '贵公司',
    '贵司',
    '非常匹配',
    '高度匹配',
    '深受吸引',
    '赋能',
    '助力',
    '核心落地经验',
    '攻克',
    '深耕',
    '致力于',
    '期待加入',
    '看到贵公司正在招聘',
    '您好，看到',
    '您', '体现了', '具备', '能力', '主导开发', '问答服务', '系统设计',
    '想了解目前的难点', '想听听需求', '想聊聊方案',
    '想了解具体怎么做', '想聊聊具体需求', '想听听具体的需求',
)


def _normalized(text):
    return ''.join(unicodedata.normalize('NFKC', text).split()).casefold()


def _numeric_achievements(text):
    # Count values, including BOTH sides of a before/after result.
    normalized = unicodedata.normalize('NFKC', text)
    return re.findall(r'\d+(?:\.\d+)?(?:[%万亿])?|[零〇一二三四五六七八九十百千万亿两]+(?=[%个篇条项人元倍秒年月天级多]|用户|经验)', normalized)


# Finite vocabulary; applications can extend it in greetings.generate.jargon_terms.
JARGON_TERMS = ('RAG', 'MCP', 'Agent', 'LLM', 'BM25', 'RRF', 'Rerank', 'Embedding',
                'LangChain', 'LangGraph', 'pgvector', '混合检索', '端到端延迟', '多智能体',
                '多Agent路由', '向量检索', '向量数据库', '研发自动化流水线', 'DAG', 'Token',
                'Function Calling', 'Mem0', 'JEV', 'GEPA', 'LoRA', 'vLLM')

# Packaged defaults; runtime callers may configure each vocabulary.
import json
from pathlib import Path
_VOCAB = json.loads((Path(__file__).resolve().parents[1]/'prompts/vocabulary.json').read_text(encoding='utf-8'))
HOOK_PREFIXES = tuple(_VOCAB['hook_prefixes'])
HOOK_SUFFIXES = tuple(_VOCAB['hook_suffixes'])
CLOSING_LINES = tuple(_VOCAB['closing_lines'])


def split_hook(paragraph):
    """Split the employer-facing opening clause at the first comma or full stop."""
    match = re.search(r'[，,。]', paragraph)
    if not match:
        return paragraph, ''
    return paragraph[:match.start()], paragraph[match.end():]


def entry_pattern(entry, hook=''):
    """Mask the JD span in the first sentence; no model-supplied pattern labels."""
    first = split_hook(entry)[0]
    return _normalized(first.replace(hook, '[JD]') if hook else first)


def entry_patterns(entry, hook=''):
    patterns = {entry_pattern(entry, hook)}
    # Also catch a repeated conversational bridge after otherwise varied openings.
    first, rest = split_hook(entry)
    if rest:
        bridge = re.split(r'[，,。！？!?]', rest)[0]
        if re.fullmatch(r'.{0,8}我(?:也|之前)?(?:做过|熟|参与过)', bridge):
            patterns.add('bridge:' + _normalized(bridge))
    return patterns


def entry_similarity(left, right):
    from difflib import SequenceMatcher
    left, right = _normalized(left), _normalized(right)
    # SequenceMatcher can depend on argument order. Every pair uses a symmetric
    # conservative score so swapping generation order cannot bypass the gate.
    return max(SequenceMatcher(None, left, right, autojunk=False).ratio(),
               SequenceMatcher(None, right, left, autojunk=False).ratio())


def _company_grounded(text, context, hook, facts, hook_prefixes=HOOK_PREFIXES, hook_suffixes=HOOK_SUFFIXES):
    # Only the employer-facing opening is a closed scaffold. Candidate experience
    # after it may be rewritten, with provenance checked by validate_result.
    paragraphs = re.split(r'\n[ \t]*\n', text.strip())
    if len(paragraphs) not in {3, 4} or not isinstance(hook, str) or not hook or hook not in context:
        return False
    first, tail = split_hook(paragraphs[1])
    rest = [tail] if tail else []
    if first.count(hook) != 1:
        return False
    before, after = first.split(hook)
    trim = lambda s: s.strip(' ，,。！？!?：“”「」')
    if trim(before) not in hook_prefixes or trim(after) not in hook_suffixes:
        return False
    # Do not permit another employer/product claim beside the valid source span.
    remaining = '\n'.join((paragraphs[0], *rest, *paragraphs[2:]))
    return not re.search(r'你们|贵司|贵公司|(?:产品|业务)(?:叫|名为|是)|(?:试过|用过).*产品', remaining)


def lint_greeting(text: str, *, min_length: int = 70, max_length: int = 120,
                  blacklist=None, company_context=None, company_hook='', candidate_facts=(),
                  require_paragraphs=True, jargon_terms=None, max_sentence_length=45, previous_entries=(),
                  similarity_threshold=0.78, max_sentence_pattern=3, profile=None,
                  hook_prefixes=HOOK_PREFIXES, hook_suffixes=HOOK_SUFFIXES) -> dict[str, object]:
    """Deterministic copy checks; lexical source guards cannot prove all semantics.

    Generation always supplies context and validates canonical 3–4 paragraph text
    BEFORE platform-specific flattening. Line breaks do not count as characters.
    """
    if not isinstance(text, str):
        raise ValueError('Greeting text must be a string')
    stripped = text.strip()
    length = len(''.join(stripped.splitlines()))
    issues: list[dict[str, object]] = []
    if not stripped:
        return {'ok': False, 'length': 0, 'issues': [{'code': 'empty', 'message': '招呼语为空'}]}
    if length > max_length:
        issues.append({'code': 'too_long', 'message': f'招呼语共 {length} 字，超过 {max_length} 字上限'})
    elif length < min_length:
        issues.append({'code': 'too_short', 'message': f'招呼语共 {length} 字，少于 {min_length} 字'})
    paragraphs = re.split(r'\n[ \t]*\n', stripped.replace('\r\n', '\n'))
    if require_paragraphs and (len(paragraphs) not in {3, 4} or any(len(p.splitlines()) != 1 for p in paragraphs)):
        issues.append({'code': 'paragraph_format', 'message': '需要3–4段，每段一行，段间空一行'})
    terms = AI_CLICHE_TERMS if blacklist is None else blacklist
    normalized = _normalized(stripped)
    matched = [term for term in terms if _normalized(term) in normalized]
    if matched:
        issues.append({'code': 'ai_cliche', 'terms': matched, 'message': '包含模板化或书面词'})
    if len(_numeric_achievements(stripped)) > 2:
        issues.append({'code': 'numeric_list', 'message': '最多两个数字，前后对比按两个计'})
    sentences = re.split(r'[。！？!?\n\r\u2028\u2029]', stripped)
    if any(len(s.strip()) > max_sentence_length for s in sentences):
        issues.append({'code': 'long_sentence', 'message': f'一句话不超过 {max_sentence_length} 字，请拆成短句'})
    if require_paragraphs and len(paragraphs) in {3, 4} and len([x for x in re.split(r'[。！？!?]', paragraphs[1]) if x.strip()]) > 3:
        issues.append({'code': 'choppy', 'message': '第二段最多3句，用逗号连成自然的话'})
    jargon = JARGON_TERMS if jargon_terms is None else jargon_terms
    # Longest phrases first avoids counting Agent again inside 多Agent路由.
    residual = normalized
    found = []
    for term in sorted(jargon, key=len, reverse=True):
        key = _normalized(term)
        if key in residual:
            found.append(term)
            residual = residual.replace(key, '')
    absent = [term for term in found if _normalized(term) not in _normalized(company_context or '')]
    if len(found) > 1 or (company_context is not None and absent):
        issues.append({'code': 'jargon_overload', 'terms': found, 'message': '最多一个JD原有术语，请改成大白话'})
    if require_paragraphs and len(paragraphs) in {3, 4}:
        if profile is None:
            from job_agent.config import load_config
            profile = load_config().profile
        identity = _normalized(paragraphs[0]).replace('，','').replace('。','').replace(',', '')
        if not all(_normalized(part).replace('，','').replace('。','').replace(',', '') in identity
                   for part in (profile.name, profile.role, profile.availability, profile.graduation) if part):
            issues.append({'code': 'identity', 'message': '身份段须含配置的姓名、工作方向和可用时间'})
        if not re.search(r'(?:做过|处理过|负责|参与过|改过|搭过|加过|[我让].*(?:做|写|测|关|换|采集)|帮.*写|给.*做)', paragraphs[1]):
            issues.append({'code': 'experience_missing', 'message': '第二段须说具体做过什么'})
        if len(paragraphs[-1]) > 18 or '简历' not in paragraphs[-1]:
            issues.append({'code': 'closing', 'message': '用一句短话收尾'})
    if len(paragraphs) in {3, 4} and previous_entries:
        entry = paragraphs[1]
        for previous in previous_entries:
            if entry_similarity(previous['text'], entry) > similarity_threshold:
                issues.append({'code': 'homogeneous', 'message': '同批第二段太相似，请重写具体经历'})
                break
        patterns = entry_patterns(entry, company_hook)
        if any(sum(pattern in entry_patterns(p['text'], p.get('hook', '')) for p in previous_entries)
               >= max_sentence_pattern for pattern in patterns):
            issues.append({'code': 'sentence_pattern', 'message': '同批相同开头句式最多出现三次'})
    if company_context is not None and not _company_grounded(stripped, company_context, company_hook, candidate_facts, hook_prefixes, hook_suffixes):
        issues.append({'code': 'ungrounded_company', 'message': '切入段必须来自 JD/简介原句，不得添加产品或业务名'})
    return {'ok': not issues, 'length': length, 'issues': issues}
