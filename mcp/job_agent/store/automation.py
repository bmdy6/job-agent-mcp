"""Additive rate state and the same durable pause table used by daily (#39/#44)."""
import json
from job_agent.store.database import now


class AutomationRepository:
    def __init__(self,store):
        self.store,self.db=store,store.connection
        existing={r[0] for r in self.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('platform_pauses','operation_rate','operation_rate_state')")}
        if len(existing)<3:
            with store.transaction():
                self.db.execute('CREATE TABLE IF NOT EXISTS platform_pauses(platform TEXT PRIMARY KEY, reason TEXT NOT NULL, paused_at TEXT NOT NULL)')
                self.db.execute('CREATE TABLE IF NOT EXISTS operation_rate(platform TEXT, scope TEXT, at REAL NOT NULL)')
                self.db.execute('CREATE INDEX IF NOT EXISTS operation_rate_lookup ON operation_rate(platform,scope,at)')
                self.db.execute('CREATE TABLE IF NOT EXISTS operation_rate_state(platform TEXT, scope TEXT, state TEXT NOT NULL, PRIMARY KEY(platform,scope))')
    def pauses(self):
        return dict(self.db.execute('SELECT platform,reason FROM platform_pauses'))
    def pause(self,platform,reason='risk_control'):
        self.db.execute('INSERT OR REPLACE INTO platform_pauses VALUES(?,?,?)',(platform,reason,now()))
    def pause_token(self,platform):
        row=self.db.execute('SELECT reason,paused_at FROM platform_pauses WHERE platform=?',(platform,)).fetchone()
        return tuple(row) if row else None
    def resume(self,platform,*,expected=None):
        if expected is None:
            return platform not in self.pauses()
        return self.db.execute('DELETE FROM platform_pauses WHERE platform=? AND reason=? AND paused_at=?',(platform,*expected)).rowcount==1
    def state(self,platform,scope):
        row=self.db.execute('SELECT state FROM operation_rate_state WHERE platform=? AND scope=?',(platform,scope)).fetchone()
        return json.loads(row[0]) if row else {}
    def set_state(self,platform,scope,state):
        self.db.execute('INSERT OR REPLACE INTO operation_rate_state VALUES(?,?,?)',(platform,scope,json.dumps(state)))
    def operations(self,platform,scope):
        return [r[0] for r in self.db.execute('SELECT at FROM operation_rate WHERE platform=? AND scope=?',(platform,scope))]
    def record(self,platform,scope,at,state):
        self.db.execute('INSERT INTO operation_rate VALUES(?,?,?)',(platform,scope,at))
        self.set_state(platform,'pacing',state)
        # Only today's/recent history is needed; keep a bounded rolling retention.
        self.db.execute('DELETE FROM operation_rate WHERE at<?',(at-172800,))
