"""Global runtime switch, independent of config and platform risk holds.

Followup workers must call allowed('followup') before planning or sending.
The shared delivery engine also checks this switch between operations.
"""
from job_agent.store.state import snapshot

MODES = {'running', 'paused', 'only_reply'}
CONTROL_KIND = 'automation_control'
CONTROL_ID = 'automation:control'


def mode(store=None):
    if store is None:
        with snapshot() as reader:
            return mode(reader) if reader else 'running'
    record = store.latest_run(CONTROL_KIND) or {}
    # Corrupt persisted control fails closed, rather than silently resuming.
    value = record.get('mode', 'running')
    return value if value in MODES else 'paused'


def allowed(task, store=None):
    value = mode(store)
    return value == 'running' or (value == 'only_reply' and task == 'monitor')


def hold(task, store=None):
    value = mode(store)
    if value == 'running' or (value == 'only_reply' and task == 'monitor'):
        return None
    return {'status': 'deferred', 'reason_code': 'automation_' + value}


def set_mode(store, value):
    if value not in MODES:
        raise ValueError('无效的运行模式')
    store.save_run(CONTROL_KIND, {'mode': value}, run_id=CONTROL_ID)
