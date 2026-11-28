"""User-only notification adapters. Secrets and upstream errors never leave transport."""
import json
import re
import logging
import os
import sqlite3
import time
import hashlib
import uuid
from contextvars import ContextVar
from http.client import HTTPSConnection, HTTPConnection, HTTPException
from urllib.parse import quote, urlsplit
from typing import Any

from job_agent.config import NotifySettings, TelegramSettings, load_config
from mcp.types import ToolAnnotations

log = logging.getLogger(__name__)
_SEND_DEADLINE = ContextVar('notify_deadline', default=None)
SEND_BUDGET_SECONDS = 50


def request_timeout():
    deadline = _SEND_DEADLINE.get()
    remaining = deadline - time.monotonic() if deadline is not None else 10
    if remaining <= 0:
        raise SendError('transport')
    return min(10, remaining)


def socket_timeout(connection, response_socket=None):
    timeout = request_timeout()
    sock = connection.sock if connection.sock is not None else response_socket
    if sock is not None:
        sock.settimeout(timeout)


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
        connection = cls(parsed.hostname, parsed.port, timeout=request_timeout())
        connection.request('POST', parsed.path + ('?'+parsed.query if parsed.query else ''),
                           json.dumps(body, ensure_ascii=False).encode(),
                           {'Content-Type': 'application/json', **(headers or {})})
        response_socket = connection.sock
        socket_timeout(connection)
        response = connection.getresponse()
        if response.status >= 300:
            raise SendError(response.status)
        chunks, size = [], 0
        while size <= 1024*1024:
            if response.isclosed() is True:
                break
            # read1 performs one bounded socket read; a trickling peer cannot reset
            # the overall send deadline indefinitely.
            socket_timeout(connection, response_socket)
            chunk = response.read1(min(65536, 1024*1024+1-size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        raw = b''.join(chunks)
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
        from job_agent.config import job_agent_home
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
    from job_agent.observability import current_run_id
    ident = current_run_id()
    match = re.search(r'\n\n运行编号：([0-9a-f]{8})$',text)
    if match:
        ident = match.group(1)
        text = text[:match.start()]
    suffix = ('\n\n'+url if url else '') + ('\n\n运行编号：'+ident[:8] if ident else '')
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


def _attempts(adapter, title, text, url, retries, deadline):
    for attempt in range(min(max(retries, 0), 2)+1):
        if time.monotonic() >= deadline:
            return {'success': False, 'error_code': 'transport'}
        try:
            adapter.send(title, text, url)
            return {'success': True}
        except SendError as error:
            code = error.code
        except Exception:
            code = 'transport'
        if code != 'transport' or attempt >= min(max(retries, 0), 2):
            break
        delay = 2**attempt
        if time.monotonic() + delay >= deadline:
            break
        time.sleep(delay)
    log.warning('notification failed error_code=%s', code)
    return {'success': False, 'error_code': code}


def channel_identity(config):
    # A changed chat, webhook URL or bot must never inherit another target's queue.
    fields = config.model_dump(exclude={'enabled'})
    names = [config.bot_token_env] if isinstance(config, TelegramSettings) else (
        [config.sendkey_env] if config.format == 'serverchan' else [config.url_env])
    destination = [secret(name) for name in names]
    return hashlib.sha256(json.dumps([fields, destination], sort_keys=True).encode()).hexdigest()


def send_one(config, title, text, url=None, retries=2, *, durable=True, queue_only=False, keep_failed=False):
    from job_agent.observability import current_run_id
    ident = current_run_id()
    if ident and not re.search(r'\n\n运行编号：[0-9a-f]{8}$',text):
        text += '\n\n运行编号：'+ident[:8]
    adapter = TelegramAdapter(config) if isinstance(config, TelegramSettings) else WebhookAdapter(config)
    deadline = time.monotonic() + SEND_BUDGET_SECONDS
    token = _SEND_DEADLINE.set(deadline)
    try:
        if not durable:
            return _attempts(adapter, title, text, url, retries, deadline)
        try:
            identity = channel_identity(config)
        except SendError as error:
            return {'success': False, 'error_code': error.code}
        from job_agent.store.notifications import notification_sender
        try:
            with notification_sender(deadline) as store:
                pending = [e for e in store.notifications(channel=identity, pending=True) if e['kind']=='user']
                current = next((e for e in pending if (e['title'],e['body'],e['url']) == (title,text,url)), None)
                if current is None:
                    ident = store.enqueue_notification('user:'+uuid.uuid4().hex, identity, title, text, url)
                    current = next(e for e in store.notifications(channel=identity, pending=True) if e['id']==ident)
                    pending.append(current)
                if queue_only:
                    return {'success': False, 'error_code': 'deferred', 'queued': True}
                for entry in pending[:10]:
                    result = _attempts(adapter, entry['title'], entry['body'], entry['url'], retries, deadline)
                    permanent = not result['success'] and result['error_code'] != 'transport' and not keep_failed
                    store.notification_result(entry['id'], result['success'], result.get('error_code',''),
                                              next_retry_at=1e30 if permanent else None)
                    if entry['id'] == current['id']:
                        return {**result, **({'queued': True} if result.get('error_code') == 'transport' or keep_failed and not result['success'] else {})}
                    if result.get('error_code') == 'transport':
                        break
                return {'success': False, 'error_code': 'transport', 'queued': True}
        except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
            log.warning('notification outbox unavailable')
            return {'success': False, 'error_code': 'outbox_storage'}
    finally:
        _SEND_DEADLINE.reset(token)


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
                    '这是一条发给用户本人的渠道测试消息。', retries=0, durable=False)


def register_tools(mcp):
    @mcp.tool(name='notify_test', annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                                         idempotentHint=False, openWorldHint=True))
    def test_notification(channel: str) -> dict[str, Any]:
        """Send ONE user-only test notification to a configured channel, even if disabled.

        Only call at explicit user request. No retries, no getUpdates, no HR messages.
        Returns sanitized success/error_code, never credentials or provider response text.
        """
        return notify_test(channel)
