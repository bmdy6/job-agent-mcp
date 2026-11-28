"""Notify the user from replies saved in the owned store; no platform access."""


def poll_once(config=None, *, source="replies", store=None):
    if source != 'replies':
        raise ValueError('Unknown watch source; only replies is supported')
    if config is None:
        from job_agent.monitor.config import load_settings
        config, _ = load_settings()
    from job_agent.monitor.notifications import poll_replies
    if not config.notify.enabled or not any(c.enabled and c.type == 'telegram' for c in config.notify.channels.values()):
        return {'sent': 0, 'failed': 0, 'skipped': 0, 'suppressed': 0}
    if store is not None:
        return poll_replies(store, config)
    from job_agent.store import Store
    with Store() as database:
        return poll_replies(database, config)
