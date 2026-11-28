"""Private owned worker control. Stop flags never signal unrelated processes."""
from job_agent.observability import observed, report_error, run_scope
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from job_agent.config import job_agent_home
from job_agent.store import Store


def paths(kind):
    if kind not in {'delivery','monitor','scoring'}:
        raise ValueError('Unknown worker kind')
    home = job_agent_home() / 'own-tasks'
    home.mkdir(parents=True,exist_ok=True,mode=0o700)
    return kind, home / (kind+'.lock')


@contextmanager
def locked(kind):
    state, lock = paths(kind)
    fd=os.open(lock,os.O_CREAT|os.O_RDWR,0o600)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX)
        yield state
    finally:
        os.close(fd)


def read(path):
    if isinstance(path, str) and path in {'delivery','monitor','scoring'}:
        with Store() as store:
            return store.worker_state(path)
    return json.loads(path.read_text()) if path.exists() else None


def write(path, value):
    if isinstance(path, str) and path in {'delivery','monitor','scoring'}:
        with Store() as store:
            store.save_worker_state(path, value)
        return
    temp=path.with_suffix('.tmp')
    fd=os.open(temp,os.O_CREAT|os.O_TRUNC|os.O_WRONLY,0o600)
    with os.fdopen(fd,'w') as stream: json.dump(value,stream)
    os.replace(temp,path)


def status(kind):
    with locked(kind) as path:
        state=read(path)
        if state and state['status'] in {'running','stopping'}:
            try: os.kill(state['pid'],0)
            except ProcessLookupError:
                if kind=='scoring' and state.get('run_id'):
                    from job_agent.service.scoring import abandon_run
                    abandon_run(state['run_id'])
                state['status']='interrupted'
                write(path,state)
        return state


def start(kind, **request):
    status(kind)
    with locked(kind) as path:
        state=read(path)
        if state and state['status'] in {'running','stopping'}:
            raise ValueError('Owned task already running')
        ident=uuid.uuid4().hex
        with Store() as store:
            store.worker_request(ident, request)
        env={**os.environ,'PYTHONPATH':str(Path(__file__).resolve().parents[2])}
        log=os.open(job_agent_home()/'own-tasks'/(kind+'.log'),os.O_CREAT|os.O_APPEND|os.O_WRONLY,0o600)
        try:
            process=subprocess.Popen([sys.executable,'-m','job_agent.service.tasks',kind,ident],
                stdin=subprocess.DEVNULL,stdout=log,stderr=log,env=env,start_new_session=True)
        finally: os.close(log)
        state={'id':ident,'pid':process.pid,'status':'running'}
        if kind=='scoring': state['run_id']=request['run_id']
        write(path,state)
        return state


def stop(kind):
    with locked(kind) as path:
        state=read(path)
        if not state or state['status'] not in {'running','stopping'}:
            return {'stopped':False,'reason':'No active owned task'}
        with Store() as store:
            store.request_stop(state['id'])
        state['status']='stopping'
        write(path,state)
        return {'stopped':True,'task':state,'instruction':'Cooperative stop after current operation; verify status'}


@observed('worker', id_argument='ident')
def worker(kind, ident):
    from job_agent.logsetup import setup_logging
    setup_logging(kind,home=job_agent_home())
    from job_agent.config import load_config
    path,_=paths(kind)
    with locked(kind):
        state=read(path)
        if not state or state['id']!=ident: return
    def should_stop():
        with Store() as store:
            return store.stop_requested(ident)
    with Store() as store:
        request = store.worker_request(ident)
    try:
        if kind=='scoring':
            from job_agent.service.scoring import execute_run
            with run_scope('scoring',run_id=request['run_id'],fresh=True):
                result=execute_run(request['run_id'],should_stop=should_stop)
        elif should_stop(): result='Stopped before execution'
        elif kind=='delivery':
            from job_agent.store import SQLiteDeliveryStore
            from job_agent.service.deliver import confirm
            if time.time()>=request['expires']: raise ValueError('Confirmation expired before worker start')
            with SQLiteDeliveryStore(dry_run=False) as store:
                result=confirm(store,load_config(),request['token'],exclude_ids=request['exclude_ids'],
                               should_stop=should_stop,notify=True)
        else:
            from job_agent.monitor.runtime import run
            result=run(should_stop=should_stop)
        state.update(status='stopped' if should_stop() else 'completed',result=result)
    except Exception as error:
        if kind=='scoring':
            from job_agent.service.scoring import abandon_run
            if request and request.get('run_id'):
                try:
                    abandon_run(request['run_id'])
                except Exception:
                    pass  # Keep the original failure; durable claims still prevent replays.
        state.update(status='failed',**report_error(error,stage=kind))
    finally:
        with locked(kind): write(path,state)
        with Store() as store:
            store.delete_worker_request(ident)

if __name__=='__main__': worker(*sys.argv[1:])
