"""User-only notification adapters. Secrets and upstream errors never leave transport."""
import json
import logging
import os
import time
from http.client import HTTPSConnection, HTTPConnection, HTTPException
from urllib.parse import quote, urlsplit
from typing import Any

from config import NotifySettings, TelegramSettings, load_config
from mcp.types import ToolAnnotations

log = logging.getLogger(__name__)


class SendError(Exception):
    def __init__(self, code):
        # Never accept arbitrary upstream text as an error code.
        self.code = str(code) if isinstance(code, int) else code
        if self.code not in {'transport', 'invalid_response', 'missing_secret', 'invalid_config', 'http_error', 'api_error'} and not str(self.code).lstrip('-').isdigit():
            self.code = 'api_error'
        super().__init__(self.code)


def post(url: str, body: dict, headers: dict | None = None) -> dict:
    """JSON POST, no redirects. Loopback HTTP is supported for local model servers."""
    connection = None
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != 'https' and not (parsed.scheme == 'http' and parsed.hostname in {'127.0.0.1', 'localhost', '::1'})) or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise SendError('invalid_config')
        cls = HTTPSConnection if parsed.scheme == 'https' else HTTPConnection
        connection = cls(parsed.hostname, parsed.port, timeout=25)
        connection.request('POST', parsed.path + ('?'+parsed.query if parsed.query else ''),
                           json.dumps(body, ensure_ascii=False).encode(),
                           {'Content-Type': 'application/json', **(headers or {})})
        response = connection.getresponse()
        raw = response.read(1024*1024 + 1)
        if response.status >= 300:
            raise SendError(response.status)
        if len(raw) > 1024*1024:
            raise SendError('invalid_response')
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise SendError('invalid_response')
        return payload
    except SendError:
        raise
    except (OSError, HTTPException):
        raise SendError('transport') from None
    except (ValueError, TypeError):
        raise SendError('invalid_response') from None
    finally:
        if connection is not None:
            connection.close()


def secret(name: str) -> str:
    value = os.environ.get(name, '').strip()
    if not value:
        from config import job_agent_home
        path = job_agent_home() / 'daily.env.json'
        if path.exists():
            if path.stat().st_mode & 0o077:
                raise SendError('invalid_config')
            try:
                values = json.loads(path.read_text())
                value = values.get(name, '').strip()
            except (ValueError, TypeError, AttributeError, OSError):
                raise SendError('invalid_config') from None
    if not value:
        raise SendError('missing_secret')
    return value


def bounded(title: str, text: str, url: str | None, limit: int, encoding: str) -> str:
    """Bound text in provider units while preserving the final link block."""
    suffix = '\n\n'+url if url else ''
    prefix = title[:100]+'\n\n'
    unit = 2 if encoding == 'utf-16-le' else 1
    budget = limit*unit - len((prefix+suffix).encode(encoding))
    if budget < 0:
        raise SendError('invalid_config')
    content = text.encode(encoding)[:budget].decode(encoding, errors='ignore')
    return prefix + content + suffix


class TelegramAdapter:
    def __init__(self, config):
        self.config = config

    def send(self, title, text, url=None):
        if not self.config.chat_id:
            raise SendError('invalid_config')
        token = quote(secret(self.config.bot_token_env), safe=':')
        result = post(f'https://api.telegram.org/bot{token}/sendMessage', {
            'chat_id': self.config.chat_id,
            'text': bounded(title, text, url, 4096, 'utf-16-le'),
            'link_preview_options': {'is_disabled': True},
        })
        if result.get('ok') is not True:
            raise SendError(result.get('error_code', 'api_error'))


class WebhookAdapter:
    def __init__(self, config):
        self.config = config

    def send(self, title, text, url=None):
        fmt = self.config.format
        if fmt == 'serverchan':
            key = quote(secret(self.config.sendkey_env), safe='')
            endpoint = f'https://sctapi.ftqq.com/{key}.send'
            payload = {'title': title.replace('\n', ' ')[:100],
                       'desp': bounded('', text, url, 30000, 'utf-8').strip()}
            code_field = 'code'
        else:
            endpoint = secret(self.config.url_env)
            if fmt == 'wecom':
                payload = {'msgtype': 'markdown', 'markdown': {
                    'content': bounded(title, text, url, 4096, 'utf-8')}}
                code_field = 'errcode'
            else:
                payload = {'title': title[:100], 'text': text[:20000], 'url': url}
                code_field = None
        result = post(endpoint, payload)
        if code_field and (type(result.get(code_field)) is not int or result[code_field] != 0):
            raise SendError(result.get(code_field, 'api_error'))


def send_one(config, title, text, url=None, retries=2):
    adapter = TelegramAdapter(config) if isinstance(config, TelegramSettings) else WebhookAdapter(config)
    for attempt in range(retries+1):
        try:
            adapter.send(title, text, url)
            return {'success': True}
        except SendError as error:
            code = error.code
        except Exception:
            # Do not leak arbitrary exception text (often contains credential URLs).
            code = 'transport'
        if attempt < retries:
            time.sleep(1 + attempt)
    log.warning('notification failed error_code=%s', code)
    return {'success': False, 'error_code': code}


def send_channels(config: NotifySettings, title, text, url=None, retries=2):
    if not config.enabled:
        return {}
    return {name: send_one(channel, title, text, url, retries)
            for name, channel in config.channels.items() if channel.enabled}


def notify_test(channel: str) -> dict:
    """Explicit one-attempt test, even when disabled; never polls Telegram or HR."""
    channels = load_config().notify.channels
    if channel not in channels:
        raise ValueError('Unknown notification channel')
    return send_one(channels[channel], 'job-agent 通知测试',
                    '这是一条发给用户本人的渠道测试消息。', retries=0)


def register_tools(mcp):
    @mcp.tool(name='notify_test', annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                                         idempotentHint=False, openWorldHint=True))
    def test_notification(channel: str) -> dict[str, Any]:
        """Send ONE user-only test notification to a configured channel, even if disabled.

        Only call at explicit user request. No retries, no getUpdates, no HR messages.
        Returns sanitized success/error_code, never credentials or provider response text.
        """
        return notify_test(channel)
