"""Bounded diagnostic values only: never serialize exception text or page data."""
import re
from datetime import datetime, timezone

_TOKEN = re.compile(r'[a-z][a-z0-9_]{0,63}(?::(?:latestMsgId|unReadCnt|id))?\Z')


def token(value, fallback):
    return value if isinstance(value, str) and _TOKEN.fullmatch(value) else fallback


def failure(error, stage='fetch'):
    return {'stage': token(getattr(error, 'stage', None), stage),
            'reason_code': token(getattr(error, 'reason_code', getattr(error, 'code', None)), 'unexpected_error')}


def advice(kind, reason):
    if kind == 'risk':
        return '已暂停，请本人在官方端确认账号状态；不要自动重试'
    if kind == 'login':
        return '请本人登录后重启监听'
    if kind == 'space':
        return '请检查 Ego 空间所有权后重启监听'
    if reason.startswith(('summary_', 'contacts_')) or reason in {'receipt_unknown', 'page_structure'}:
        return '页面结构或数据未确认，等待修复'
    return '请检查 Ego 连接和该阶段日志；持续失败时等待修复'


def alert_body(platform, state, recovered=False):
    last = state.get('last_success_at')
    stamp = datetime.fromtimestamp(last, timezone.utc).isoformat() if last else '尚无成功记录'
    return (f"平台：{platform}\n{'已恢复' if recovered else '监听失败'}\n"
            f"阶段：{state['stage']}\n原因码：{state['reason_code']}\n"
            f"连续失败次数：{state['count']}\n上次成功时间：{stamp}\n"
            f"处理建议：{'已恢复正常轮询' if recovered else advice(state['kind'], state['reason_code'])}")
