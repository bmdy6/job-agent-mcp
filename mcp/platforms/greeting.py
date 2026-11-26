"""Platform-owned newline declarations; pure text preparation shared by all consumers."""
from hashlib import sha256
from importlib import import_module


def prepare_greeting(platform: str, text: str, separator: str = '；') -> str:
    if platform not in {'boss', 'liepin', 'zhilian', '51job', 'nowcoder', 'maimai'}:
        raise ValueError('Unknown greeting platform')
    if separator not in {'；', ' '}:
        raise ValueError('Greeting newline separator must be semicolon or space')
    name = 'job51' if platform == '51job' else platform
    mode = import_module(f'platforms.{name}.pages').GREETING_NEWLINE
    text = text.replace('\r\n', '\n').replace('\r', '\n').replace('\u2028', '\n').replace('\u2029', '\n')
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
