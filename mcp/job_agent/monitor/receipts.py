"""Shared notification identity and receipt parsing; preserves legacy watch keys."""
import hashlib
import json

def digest(*parts):
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
