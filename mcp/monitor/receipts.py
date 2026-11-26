"""Shared notification identity and receipt parsing; preserves legacy watch keys."""
import hashlib
import json

def digest(*parts):
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def read_receipts(stream):
    receipts, job_receipts = set(), set()
    stream.seek(0)
    for line in stream:
        try:
            item = json.loads(line)
            event, job, channel = item['event'], item['job'], item['channel']
            if not all(isinstance(value, str) for value in (event, job, channel)):
                raise ValueError()
        except (ValueError, KeyError, TypeError):
            raise ValueError('Invalid notified.jsonl; repair from backup before watching') from None
        receipts.add((event, channel))
        job_receipts.add((job, channel))
    return receipts, job_receipts
