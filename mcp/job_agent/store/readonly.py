"""Strict, consistent read-only snapshots for public projections."""
from contextlib import contextmanager
import sqlite3
import json
import uuid
from contextlib import closing
from job_agent.store.database import Store, default_path, SCHEMA_VERSION


class ReadStore(Store):
    def __init__(self):
        self.path = default_path().resolve()
        self.connection = sqlite3.connect(self.path.as_uri()+'?mode=ro', uri=True)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute('BEGIN')  # one consistent snapshot per tool
        try:
            if self.schema_version != SCHEMA_VERSION:
                raise ValueError('Own store schema requires initialization/migration by the engine')
        except BaseException:
            self.connection.close()
            raise


@contextmanager
def snapshot():
    store = ReadStore()
    try:
        yield store
    finally:
        store.close()



def append_application_event(record):
    # rw refuses to create a missing store. No migration or upstream write.
    path = default_path().resolve()
    with closing(sqlite3.connect(path.as_uri()+'?mode=rw',uri=True)) as db, db:
        db.execute('PRAGMA foreign_keys=ON')
        if not db.execute('SELECT 1 FROM jobs WHERE id=? AND deleted_at IS NULL',(record['job_id'],)).fetchone():
            raise ValueError('Own job not found')
        db.execute('INSERT INTO source_history(source_key,job_id,action,detail,occurred_at) VALUES(?,?,?,?,?)',
                   ('job-agent:event:'+uuid.uuid4().hex,record['job_id'],'job_agent_application_event',
                    json.dumps(record,ensure_ascii=False),record['time']))
    return record
