"""Collection persistence. All collection SQL stays in the store layer.

Additive component tables deliberately leave shared jobs/delivery schema untouched.
"""
from contextlib import contextmanager
from datetime import datetime
from zoneinfo import ZoneInfo
import fcntl
import json
import os
import time


class CollectionRepository:
    def __init__(self, store, *, timezone='Asia/Shanghai', clock=time.time):
        self.store, self.clock, self.zone = store, clock, ZoneInfo(timezone)
        self.db = store.connection
        with store.transaction():
            self.db.execute('''CREATE TABLE IF NOT EXISTS collection_cursors (
                platform TEXT, keyword TEXT, city TEXT, page INTEGER NOT NULL,
                done_day TEXT, PRIMARY KEY(platform,keyword,city))''')
            self.db.execute('''CREATE TABLE IF NOT EXISTS collection_usage (
                platform TEXT, day TEXT, pages INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(platform,day))''')
            self.db.execute('''CREATE TABLE IF NOT EXISTS collection_platforms (
                platform TEXT PRIMARY KEY, last_visit REAL, error TEXT NOT NULL DEFAULT '')''')
            self.db.execute('CREATE TABLE IF NOT EXISTS collection_days (platform TEXT PRIMARY KEY, day TEXT NOT NULL)')
            self.db.execute('''CREATE TABLE IF NOT EXISTS collection_filters (
                job_id INTEGER PRIMARY KEY REFERENCES jobs(id), reasons TEXT NOT NULL)''')

    def day(self):
        return datetime.fromtimestamp(self.clock(), self.zone).date().isoformat()

    @contextmanager
    def exclusive(self):
        fd = os.open(str(self.store.path)+'.collect.lock', os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
            else:
                yield True
        finally:
            os.close(fd)

    def next_page(self, platform, keyword, city):
        row = self.db.execute('SELECT page,done_day FROM collection_cursors WHERE platform=? AND keyword=? AND city=?', (platform,keyword,city)).fetchone()
        if not row:
            return 1
        if row['done_day']:
            return None
        return row['page'] + 1

    def begin_cycle(self, platform, keywords, cities, *, refresh_daily=False):
        if refresh_daily:
            with self.store.transaction():
                row=self.db.execute('SELECT day FROM collection_days WHERE platform=?',(platform,)).fetchone()
                if not row or row[0]!=self.day():
                    self.db.execute('DELETE FROM collection_cursors WHERE platform=?',(platform,))
                    self.db.execute('INSERT OR REPLACE INTO collection_days VALUES(?,?)',(platform,self.day()))
            return
        # Preserve unfinished combinations across days; only reset a whole cycle.
        rows = [self.db.execute('SELECT done_day FROM collection_cursors WHERE platform=? AND keyword=? AND city=?',
                                (platform,k,c)).fetchone() for k in keywords for c in cities]
        if rows and all(r and r[0] and r[0] != self.day() for r in rows):
            self.db.execute('DELETE FROM collection_cursors WHERE platform=?', (platform,))

    def complete(self, platform, query, *, done=False):
        self.db.execute('''INSERT INTO collection_cursors VALUES(?,?,?,?,?)
            ON CONFLICT(platform,keyword,city) DO UPDATE SET page=excluded.page,done_day=excluded.done_day''',
            (platform, query.keyword, query.city, query.page, self.day() if done else None))

    def reserve_page(self, platform, limit, count=1):
        if type(count) is not int or count < 1:
            raise ValueError('page cost must be a positive integer')
        # Debit before I/O. Crashes/errors consume quota but cannot exceed it.
        with self.store.transaction():
            self.db.execute('INSERT OR IGNORE INTO collection_usage VALUES(?,?,0)', (platform,self.day()))
            return self.db.execute('UPDATE collection_usage SET pages=pages+? WHERE platform=? AND day=? AND pages+?<=?',
                                   (count,platform,self.day(),count,limit)).rowcount == 1

    def last_visit(self, platform):
        row = self.db.execute('SELECT last_visit FROM collection_platforms WHERE platform=?', (platform,)).fetchone()
        return row[0] if row else None

    def visited(self, platform):
        self.db.execute('''INSERT INTO collection_platforms(platform,last_visit) VALUES(?,?)
            ON CONFLICT(platform) DO UPDATE SET last_visit=excluded.last_visit''', (platform,self.clock()))

    def error(self, platform, code):
        self.db.execute('''INSERT INTO collection_platforms(platform,error) VALUES(?,?)
            ON CONFLICT(platform) DO UPDATE SET error=excluded.error''', (platform,code))

    def contains(self, listing):
        return self.db.execute('SELECT 1 FROM jobs WHERE platform=? AND platform_job_id=?',
                               (listing.platform,listing.platform_job_id)).fetchone() is not None

    def ingest(self, listing, reasons):
        with self.store.transaction():
            job_id = self.store.upsert_job(platform=listing.platform, platform_job_id=listing.platform_job_id,
                url=listing.url, title=listing.title, company=listing.company, city=listing.city, salary=listing.salary,
                hr_activity=listing.hr_activity, hr_activity_raw=listing.hr_activity_raw,
                experience=listing.experience or listing.extra.get('experience',''),
                education=listing.education or listing.extra.get('education',''),
                **{k:getattr(listing,k) for k in ('hr_name','hr_title','hr_id') if getattr(listing,k,'')})
            current = self.store.get_job(job_id)
            # Existing MCP tools consume legacy_status. Only map the hard-filter
            # decision; preserve imported scores/approval when a row passes.
            own_filter = (current['legacy_status'] == 'filtered'
                          and current['legacy_score_reason'].startswith('预筛不通过:采集:'))
            if reasons and (own_filter or current['legacy_status'] in {None, 'pending', 'ready', 'approved', 'scored'}):
                self.store.upsert_job(platform=listing.platform, platform_job_id=listing.platform_job_id,
                    legacy_status='filtered', legacy_score_reason='预筛不通过:采集:' + ','.join(reasons))
            elif not reasons and own_filter:
                self.store.upsert_job(platform=listing.platform, platform_job_id=listing.platform_job_id,
                                      legacy_status=None, legacy_score_reason='')
            self.db.execute('INSERT INTO collection_filters VALUES(?,?) ON CONFLICT(job_id) DO UPDATE SET reasons=excluded.reasons',
                            (job_id,json.dumps(reasons)))
        return self.store.get_job(job_id)

    def set_jd(self, listing, jd):
        self.store.upsert_job(platform=listing.platform, platform_job_id=listing.platform_job_id, jd=str(jd),
            **{k:v for k,v in getattr(jd,'hr',{}).items() if v})

    def filters(self, job_id):
        row = self.db.execute('SELECT reasons FROM collection_filters WHERE job_id=?', (job_id,)).fetchone()
        return json.loads(row[0]) if row else []

    def status(self):
        return {'day': self.day(), 'pages_today': dict(self.db.execute('SELECT platform,pages FROM collection_usage WHERE day=?', (self.day(),))),
                'cursors': [dict(row) for row in self.db.execute('SELECT * FROM collection_cursors ORDER BY platform,keyword,city')],
                'errors': dict(self.db.execute("SELECT platform,error FROM collection_platforms WHERE error<>''")),
                'filtered': self.db.execute("SELECT COUNT(*) FROM collection_filters WHERE reasons<>'[]'").fetchone()[0]}
