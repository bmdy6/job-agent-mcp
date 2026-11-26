"""Private, local persistence. No BossHunter or network dependencies."""

import hashlib
import json
import os
import re
import tempfile
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
from urllib.parse import urlsplit

from config import job_agent_home


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def text_required(value: str, name: str) -> str:
    if not value.strip():
        raise ValueError(f'{name} cannot be blank')
    return value


def local_path(*parts: str) -> Path:
    home = job_agent_home()
    path = home.joinpath(*parts)
    if not path.resolve().is_relative_to(home) or path.is_symlink():
        raise ValueError('User data path must stay inside JOB_AGENT_HOME without file symlinks')
    return path


def private_directory(path: Path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)


def experience_list() -> list[dict]:
    directory = local_path('experience')
    if not directory.exists():
        return []
    return [{key: item[key] for key in ('id', 'title')}
            for path in sorted(directory.glob('*.md'))
            if (item := experience_get(path.stem)) is not None]


def experience_get(id: str) -> dict | None:
    if not re.fullmatch(r'[\w-]{1,160}', id):
        raise ValueError('Invalid experience id')
    path = local_path('experience', id + '.md')
    if not path.exists():
        return None
    content = path.read_text(encoding='utf-8')
    if content.startswith('# ') and '\n\n' in content:
        heading, md = content.split('\n\n', 1)
        title = heading[2:]
    else:
        # Existing hand-written markdown remains readable without rewriting it.
        title = next((line.lstrip('# ').strip() for line in content.splitlines() if line.strip()), id)
        md = content
    return {'id': id, 'title': title, 'md': md}


def experience_add(title: str, md: str) -> dict:
    text_required(title, 'title')
    text_required(md, 'md')
    if '\n' in title or '\r' in title:
        raise ValueError('title must be one line')
    title = title.strip()
    slug = re.sub(r'[^\w-]+', '-', unicodedata.normalize('NFKC', title).casefold()).strip('-')[:100] or 'experience'
    slug = slug.encode('utf-8')[:180].decode('utf-8', errors='ignore').rstrip('-') or 'experience'
    directory = local_path('experience')
    private_directory(directory)
    while True:
        id = slug + '-' + uuid4().hex
        path = local_path('experience', id + '.md')
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        with os.fdopen(fd, 'w', encoding='utf-8') as output:
            output.write('# ' + title + '\n\n' + md)
        return {'id': id, 'title': title, 'md': md}


def company_key(company: str) -> str:
    key = unicodedata.normalize('NFKC', text_required(company, 'company')).casefold()
    key = re.sub(r'\s+', '', key)
    key = re.sub(r'\([^()]*\)', '', key)
    while True:
        stripped = re.sub(r'(?:有限责任公司|股份有限公司|股份公司|有限公司|公司|股份|科技)$', '', key)
        if stripped == key:
            break
        key = stripped
    return text_required(key, 'normalized company')


def company_path(company: str) -> Path:
    # A digest encodes the normalized key without truncation/path collisions.
    digest = hashlib.sha256(company_key(company).encode('utf-8')).hexdigest()
    return local_path('companies', digest + '.json')


def company_get(company: str) -> dict | None:
    path = company_path(company)
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else None


def company_save(company: str, md: str, source_url: str) -> dict:
    text_required(md, 'md')
    url = urlsplit(source_url)
    if url.scheme not in {'http', 'https'} or not url.hostname or url.username or url.password:
        raise ValueError('source_url must be an HTTP(S) URL')
    path = company_path(company)
    private_directory(path.parent)
    result = {'company': company, 'key': company_key(company), 'md': md,
              'source_url': source_url, 'updated_at': now()}
    fd, temporary = tempfile.mkstemp(dir=path.parent, suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as output:
            json.dump(result, output, ensure_ascii=False)
            output.write('\n')
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return result


def read_events() -> list[dict]:
    path = local_path('applications.jsonl')
    if not path.exists():
        return []
    events = []
    for number, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
            if not isinstance(event, dict) or not event.get('job_id') or not event.get('event'):
                raise ValueError('not an event object')
        except (ValueError, TypeError) as exc:
            raise ValueError(f'Invalid applications.jsonl at line {number}') from exc
        events.append(event)
    return events


def append_event(event: dict) -> dict:
    path = local_path('applications.jsonl')
    private_directory(path.parent)
    fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        payload = (json.dumps(event, ensure_ascii=False) + '\n').encode('utf-8')
        if os.write(fd, payload) != len(payload):
            raise OSError('Incomplete application event append')
    finally:
        os.close(fd)
    return event
