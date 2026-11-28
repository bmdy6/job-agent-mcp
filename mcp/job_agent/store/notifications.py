"""Durable notification intents and receipts, usable inside delivery transactions."""
from job_agent.store.state import encode


class Notifications:
    def enqueue_notification(self, key, channel, title, text, url=None, *, kind='user', delivery_id=None, status='pending'):
        from job_agent.store.database import now
        self.connection.execute('''INSERT INTO notifications(kind,dedupe_key,channel,title,body,url,status,delivery_id,created_at)
            VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(dedupe_key) DO NOTHING''',
            (kind,key,channel,title,text,url,status,delivery_id,now()))
        return self.connection.execute('SELECT id FROM notifications WHERE dedupe_key=?', (key,)).fetchone()[0]

    def alert_receipts(self):
        import json
        return {(r['dedupe_key'][6:], r['channel']): json.loads(r['receipt'])
                for r in self.connection.execute("SELECT * FROM notifications WHERE kind='alert'")}

    def save_alert_receipt(self, event, channel, metadata):
        ident = self.enqueue_notification('alert:'+event, channel, '', '', kind='alert', status='sent')
        self.connection.execute("UPDATE notifications SET status='sent',receipt=? WHERE id=?", (encode(metadata), ident))

    def reset_alert_receipts(self, platform):
        for (event, channel), metadata in self.alert_receipts().items():
            if metadata.get('platform') == platform:
                # Retain the row, but invalidate only this platform's cooldown.
                self.save_alert_receipt(event, channel, {**metadata, 'alert_at': None})

    def notifications(self, *, channel=None, pending=False):
        where, args = ['1=1'], []
        if channel is not None:
            where.append('channel=?'); args.append(channel)
        if pending:
            where.append("status IN ('pending','failed') AND (next_retry_at IS NULL OR next_retry_at<=?)")
            import time
            args.append(time.time())
        return [dict(r) for r in self.connection.execute('SELECT * FROM notifications WHERE '+' AND '.join(where)+' ORDER BY id', args)]

    def notification_result(self, ident, success, error='', *, next_retry_at=None):
        from job_agent.store.database import now
        self.connection.execute('''UPDATE notifications SET status=?,attempts=attempts+1,
            error=?,next_retry_at=?,sent_at=? WHERE id=? AND status<>'sent' ''',
            ('sent' if success else 'failed', error, next_retry_at, now() if success else None, ident))

    def refresh_notification(self, key, title, body):
        self.connection.execute("UPDATE notifications SET title=?,body=? WHERE dedupe_key=? AND status<>'sent'", (title,body,key))

    def delivery_notification_ready(self, delivery_id):
        row = self.connection.execute('SELECT batch_id FROM deliveries WHERE id=?', (delivery_id,)).fetchone()
        return bool(row and row[0] and not self.connection.execute("SELECT 1 FROM delivery_queue WHERE batch_id=? AND status='pending'", (row[0],)).fetchone())

    def notification_sent(self, key):
        row = self.connection.execute('SELECT status FROM notifications WHERE dedupe_key=?', (key,)).fetchone()
        return bool(row and row[0]=='sent')


from contextlib import contextmanager
import fcntl
import os
import time


@contextmanager
def notification_sender(deadline, store=None):
    """Serialize network sends without holding SQLite's write lock across I/O."""
    from job_agent.store.database import Store, default_path
    path = store.path if store is not None else default_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path.parent/'notify-outbox.lock', os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW, 0o600)
    database = None
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX|fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError('outbox busy') from None
                time.sleep(.05)
        database = store or Store()
        yield database
    finally:
        if database is not None and store is None:
            database.close()
        os.close(fd)
