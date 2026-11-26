"""Opt-in Telegram transport for driver lifecycle events; no page contents."""
import notify


def space_alert(settings, name, event):
    if settings is None or not settings.enabled:
        return
    labels = {
        'recreated': '空间已丢失，已按名称重建。旧页面操作不会重放；结果不明的投递仍须核对。',
        'unavailable': '空间控制权不可用，已停止该空间保活。请检查后明确重启，不会自动接管。',
        'keepalive_failed': '空间保活失败。未重放页面动作，请检查 Ego Lite 连接。',
    }
    failed = False
    for channel in settings.channels.values():
        if channel.enabled and channel.type == 'telegram':
            outcome = notify.send_one(channel, 'job-agent 浏览器告警', f'{name}：{labels[event]}')
            failed |= not outcome['success']
    if failed:
        raise RuntimeError('Telegram browser alert failed')
