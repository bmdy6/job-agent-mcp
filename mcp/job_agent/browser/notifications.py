"""Opt-in Telegram transport for driver lifecycle events; no page contents."""
import job_agent.notify as notify


def space_alert(settings, name, event):
    if settings is None or not settings.enabled:
        return
    labels = {
        'ego_launch_failed': '环节：浏览器；步骤：拉起 Ego Lite；原因码：ego_launch_failed；最后成功时间：未知。open 启动失败，已停止本次操作。',
        'ego_ready_timeout': '环节：浏览器；步骤：等待 CDP 就绪；原因码：ego_ready_timeout；最后成功时间：未知。启动等待超时，已停止本次操作。',
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
