"""Per-poll staged cursor. A read is acknowledged only with successful ingestion."""
import time


class ConversationCursor:
    def __init__(self, store, platform, backfill_hours):
        self.platform = platform
        cutoff = store.monitor_baseline(platform)
        self.baseline = cutoff is None
        self.since = time.time() - backfill_hours * 3600 if cutoff is None else cutoff
        self.previous = store.monitor_conversations(platform)
        self.pending = {}
        self.listed = self.selected = 0

    def select(self, summary):
        self.listed += 1
        previous = self.previous.get(summary.conversation_id)
        changed = previous is None or previous['last_message_id'] != summary.last_message_id
        # The first poll only seeds read sessions; unread ones are still opened so a
        # reply that arrives during a deploy is not swallowed (message ids dedupe).
        wanted = summary.unread_count > 0 or (changed and not self.baseline)
        if wanted and summary.last_message_id != '0':
            self.selected += 1
            return True
        self.acknowledge(summary)
        return False

    def acknowledge(self, summary):
        self.pending[summary.conversation_id] = summary

    def commit(self, store):
        store.save_monitor_conversations(self.platform, self.since, self.pending.values())

    def stats(self):
        return dict(sessions=self.listed, selected=self.selected, baseline=self.baseline)
