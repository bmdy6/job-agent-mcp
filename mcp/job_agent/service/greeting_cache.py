"""Private generation ledger, separate from the shared application database.

Claims commit before network calls. An interrupted claim is never replayed.
Only the cache stores text; call rows contain bounded metrics and error codes.
"""
from datetime import datetime
import json
import os
import sqlite3
import uuid
from zoneinfo import ZoneInfo

from job_agent.config import job_agent_home
from job_agent.service.scoring import digest


class GreetingCache:
    def __enter__(self):
        home = job_agent_home()
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = home / 'greeting-generation.sqlite3'
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        self.db = sqlite3.connect(path, timeout=5)
        self.db.execute('CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, text TEXT, trace TEXT)')
        self.db.execute('''CREATE TABLE IF NOT EXISTS calls (
            id TEXT PRIMARY KEY, key TEXT NOT NULL, run_id TEXT NOT NULL, day TEXT NOT NULL,
            model TEXT NOT NULL, status TEXT NOT NULL, prompt_tokens INTEGER, completion_tokens INTEGER,
            total_tokens INTEGER, duration_ms INTEGER, error TEXT)''')
        columns={r[1] for r in self.db.execute('PRAGMA table_info(calls)')}
        if 'usage_json' not in columns:
            self.db.execute("ALTER TABLE calls ADD COLUMN usage_json TEXT NOT NULL DEFAULT '{}'")
        self.db.commit()
        return self

    def __exit__(self, *args):
        self.db.close()

    @staticmethod
    def key(job):
        return digest(['greeting-conversation-v2', job['platform'], str(job['platform_job_id']),
                       digest([job.get('jd') or '', job.get('company_intro') or ''])])

    def claim(self, key):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            row = self.db.execute('SELECT text,trace FROM cache WHERE key=?', (key,)).fetchone()
            if row is None:
                self.db.execute('INSERT INTO cache(key) VALUES(?)', (key,))
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
        return None if row is None else ((row[0], json.loads(row[1])) if row[0] is not None else ('', {'source':'fallback','reason':'generation_in_progress_or_interrupted'}))

    def calls_today(self, timezone):
        day=datetime.now(ZoneInfo(timezone)).date().isoformat()
        return self.db.execute("SELECT COUNT(*) FROM calls WHERE day=? AND status!='budget_blocked'",(day,)).fetchone()[0]

    def reserve_call(self, key, run_id, model, budget, timezone):
        day = datetime.now(ZoneInfo(timezone)).date().isoformat()
        self.db.execute('BEGIN IMMEDIATE')
        try:
            count = self.db.execute('SELECT COUNT(*) FROM calls WHERE day=?', (day,)).fetchone()[0]
            ident = uuid.uuid4().hex if count < budget else None
            if ident:
                self.db.execute('INSERT INTO calls(id,key,run_id,day,model,status) VALUES(?,?,?,?,?,?)',
                                (ident,key,run_id,day,model,'in_progress'))
            self.db.commit()
            return ident
        except BaseException:
            self.db.rollback()
            raise

    def finish_call(self, ident, status, duration_ms, usage=None, error=''):
        usage = usage if isinstance(usage, dict) else {}
        tokens = [usage.get(k) if type(usage.get(k)) is int and usage[k] >= 0 else None
                  for k in ('prompt_tokens','completion_tokens','total_tokens')]
        self.db.execute('UPDATE calls SET status=?,duration_ms=?,prompt_tokens=?,completion_tokens=?,total_tokens=?,error=?,usage_json=? WHERE id=?',
                        (status,duration_ms,*tokens,error,json.dumps(usage),ident))
        self.db.commit()

    def save(self, key, text, trace):
        self.db.execute('UPDATE cache SET text=?,trace=? WHERE key=?',
                        (text,json.dumps(trace,ensure_ascii=False),key))
        self.db.commit()

    def recent_entries(self, direction, job_key, *, run_id=None):
        entries = []
        for text, raw in self.db.execute('SELECT text,trace FROM cache WHERE text IS NOT NULL ORDER BY rowid'):
            trace = json.loads(raw)
            in_batch = (trace.get('run_id') == run_id or run_id in trace.get('batch_run_ids', [])) if run_id else trace.get('direction') == direction
            if trace.get('source') == 'generated' and in_batch and trace.get('job_key') != job_key:
                entries.append({'text': text.split('\n\n')[1], 'hook': trace.get('company_hook', ''),
                                'closing': text.split('\n\n')[-1]})
        return entries

    def join_batch(self, key, text, trace, run_id):
        runs = list(dict.fromkeys([*trace.get('batch_run_ids', []), run_id]))[-100:]
        self.save(key, text, {**trace, 'batch_run_ids': runs})
