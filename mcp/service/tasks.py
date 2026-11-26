"""Private owned worker control. Stop flags never signal unrelated processes."""
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from config import job_agent_home


def paths(kind):
    if kind not in {'delivery','monitor','scoring'}:
        raise ValueError('Unknown worker kind')
    home = job_agent_home() / 'own-tasks'
    home.mkdir(parents=True,exist_ok=True,mode=0o700)
    return home / (kind+'.json'), home / (kind+'.lock')


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
    return json.loads(path.read_text()) if path.exists() else None


def write(path, value):
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
                    from .scoring import abandon_run
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
        stop=path.parent / (ident+'.stop')
        req=path.parent / (ident+'.json')
        write(req,request)
        env={**os.environ,'PYTHONPATH':str(Path(__file__).resolve().parents[1])}
        log=os.open(path.parent/(kind+'.log'),os.O_CREAT|os.O_APPEND|os.O_WRONLY,0o600)
        try:
            process=subprocess.Popen([sys.executable,'-m','service.tasks',kind,ident],
                stdin=subprocess.DEVNULL,stdout=log,stderr=log,env=env,start_new_session=True)
        finally: os.close(log)
        state={'id':ident,'pid':process.pid,'status':'running','stop_path':str(stop)}
        if kind=='scoring': state['run_id']=request['run_id']
        write(path,state)
        return state


def stop(kind):
    with locked(kind) as path:
        state=read(path)
        if not state or state['status'] not in {'running','stopping'}:
            return {'stopped':False,'reason':'No active owned task'}
        Path(state['stop_path']).touch(mode=0o600)
        state['status']='stopping'
        write(path,state)
        return {'stopped':True,'task':state,'instruction':'Cooperative stop after current operation; verify status'}


def worker(kind, ident):
    from config import load_config
    path,_=paths(kind)
    with locked(kind):
        state=read(path)
        if not state or state['id']!=ident: return
    should_stop=lambda: Path(state['stop_path']).exists()
    request_path=path.parent/(ident+'.json')
    try:
        request=read(request_path)
        if kind!='scoring' and load_config().engine.backend!='own': raise ValueError('Backend changed')
        if kind=='scoring':
            from .scoring import execute_run
            result=execute_run(request['run_id'],should_stop=should_stop)
        elif should_stop(): result='Stopped before execution'
        elif kind=='delivery':
            from store import SQLiteDeliveryStore
            from .deliver import confirm
            if time.time()>=request['expires']: raise ValueError('Confirmation expired before worker start')
            with SQLiteDeliveryStore(dry_run=False) as store:
                result=confirm(store,load_config(),request['token'],exclude_ids=request['exclude_ids'],
                               should_stop=should_stop,notify=True)
        else:
            from monitor.runtime import run
            result=run(should_stop=should_stop)
        state.update(status='stopped' if should_stop() else 'completed',result=result)
    except Exception as error:
        if kind=='scoring':
            from .scoring import abandon_run
            request=read(request_path)
            if request and request.get('run_id'):
                try:
                    abandon_run(request['run_id'])
                except Exception:
                    pass  # Keep the original failure; durable claims still prevent replays.
        state.update(status='failed',error_type=type(error).__name__)
    finally:
        with locked(kind): write(path,state)
        request_path.unlink(missing_ok=True)

if __name__=='__main__': worker(*sys.argv[1:])
