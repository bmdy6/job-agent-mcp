"""Generic, redacted settings with previews and locked, reversible YAML edits."""
from contextlib import closing
from copy import deepcopy
from datetime import datetime
import os
import sqlite3
import stat
import tempfile
import time
from zoneinfo import ZoneInfo
from urllib.parse import urlsplit

from job_agent.config import Settings, job_agent_home, load_config
from job_agent.config.calendar import delivery_policy, effective_limit
from job_agent.config.yaml_patch import document, patch_value

SCHEMA = Settings.model_json_schema()


def protected(name):
    return (name == 'key_source' or name.startswith('vault_')
            or name.endswith(('_env', '_path', '_token', '_secret', '_key')))


def safe_url(value):
    if not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value)
        return (parsed.scheme in {'http', 'https', 'ws', 'wss'} and bool(parsed.hostname)
                and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment)
    except (TypeError, ValueError):
        return False


def variants(schema):
    if '$ref' in schema:
        schema = SCHEMA['$defs'][schema['$ref'].split('/')[-1]]
    choices = schema.get('anyOf') or schema.get('oneOf')
    return [s for choice in choices for s in variants(choice)] if choices else [schema]


def field_schema(parts):
    candidates = [SCHEMA]
    for part in parts:
        following = []
        for candidate in candidates:
            for schema in variants(candidate):
                if part in schema.get('properties', {}):
                    following.append(schema['properties'][part])
                elif isinstance(schema.get('additionalProperties'), dict):
                    names = schema.get('propertyNames', {})
                    if 'enum' not in names or part in names['enum']:
                        following.append(schema['additionalProperties'])
        if not following:
            raise ValueError('Unknown settings field')
        candidates = following
    return [s for candidate in candidates for s in variants(candidate)]


def path_parts(path):
    if not isinstance(path, str) or not path or any(not p for p in path.split('.')):
        raise ValueError('Settings paths must be nonempty dot paths')
    parts = path.split('.')
    field_schema(parts)
    return parts


def lookup(data, parts, default=None):
    for part in parts:
        if not isinstance(data, dict) or part not in data:
            return default
        data = data[part]
    return data


def assign(data, parts, value):
    for part in parts[:-1]:
        if not isinstance(data.get(part), dict):
            data[part] = {}
        data = data[part]
    data[parts[-1]] = value


def redacted(value):
    if isinstance(value, dict):
        result = {}
        for key, child in value.items():
            if protected(key) or (key.endswith('_url') and not safe_url(child)):
                configured = bool(os.environ.get(child)) if key.endswith('_env') and isinstance(child, str) else bool(child)
                result[key] = {'configured': configured}
            else:
                result[key] = redacted(child)
        return result
    if isinstance(value, list):
        return [redacted(v) for v in value]
    return value


def today(config):
    from job_agent.engine.rate import daily_usage, operation_times
    now = time.time()
    zone = ZoneInfo(config.engine.timezone)
    day = datetime.fromtimestamp(now, zone).date()
    history = {p: [] for p in config.engine.platforms}
    operations = {p: [] for p in config.engine.platforms}
    usage_status = 'available'
    database = job_agent_home()/'jobs.sqlite3'
    if database.exists():
        try:
            with closing(sqlite3.connect(database.as_uri()+'?mode=ro', uri=True, timeout=1)) as db:
                for platform, stamp in db.execute('''SELECT j.platform, a.attempted_at FROM delivery_attempts a
                        JOIN jobs j ON j.id=a.job_id'''):
                    if platform in history:
                        history[platform].append(stamp)
                # Older databases may predate shared pacing. Do not migrate them.
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='operation_rate'").fetchone():
                    for platform, stamp in db.execute("SELECT platform, at FROM operation_rate WHERE scope='delivery'"):
                        if platform in operations:
                            operations[platform].append(stamp)
        except sqlite3.Error:
            usage_status = 'unavailable'
    platforms = {}
    for platform, options in config.engine.platforms.items():
        limit = effective_limit(config.engine, platform, now)
        policy = delivery_policy(config.engine, platform, now)
        attempts = reserved = used = None
        if usage_status == 'available':
            attempts = daily_usage(history[platform], now, config.engine.timezone)
            reserved = daily_usage(operation_times(operations[platform], history[platform]), now, policy.timezone)
            # Both the engine calendar cap and durable rate reservations can stop a
            # send. The larger used budget determines the remaining allowance.
            used = max(attempts, reserved)
        platforms[platform] = dict(enabled=options.enabled, limit=limit, used=used,
            attempts_used=attempts, rate_used=reserved,
            remaining=None if used is None else max(0, limit-used),
            allowed_hours=list(policy.allowed_hours), hours_timezone=policy.timezone)
    return dict(date=day.isoformat(), timezone=config.engine.timezone, usage_status=usage_status, platforms=platforms)


def safe_config():
    try:
        return load_config()
    except Exception:
        raise ValueError('Settings could not be loaded; check local YAML and model constraints') from None


def get_settings(section=None):
    config = safe_config()
    settings = redacted(config.model_dump(mode='json'))
    if section is not None:
        parts = path_parts(section)
        sentinel = object()
        settings = lookup(settings, parts, sentinel)
        if settings is sentinel:
            raise ValueError('Settings section is not configured')
    return dict(settings=settings, today=today(config))


def flattened(changes):
    if not isinstance(changes, dict) or not changes:
        raise ValueError('changes must be a nonempty object of dot paths')
    result = {}
    def visit(path, value):
        parts = path_parts(path)
        if any(protected(p) for p in parts):
            raise ValueError('Credential and path fields cannot be modified')
        if len(parts)==2 and parts[0]=='prompts' and value:
            from pathlib import Path
            target=Path(str(value).replace('$JOB_AGENT_HOME',str(job_agent_home()))).expanduser().resolve()
            if not target.is_relative_to((job_agent_home()/'prompts').resolve()) or target.suffix!='.md':
                raise ValueError('Settings prompt overrides must be Markdown files under JOB_AGENT_HOME/prompts')
        if parts[-1].endswith('_url') and not safe_url(value):
            raise ValueError('Connection URLs must be HTTP/WS URLs without credentials, query or fragment')
        schemas = field_schema(parts)
        if isinstance(value, dict) and value:
            for key, child in value.items():
                if not isinstance(key, str) or '.' in key:
                    raise ValueError('Nested settings keys must be single path components')
                visit(path+'.'+key, child)
            return
        # Model/union replacements could erase hidden credentials; only leaf values
        # and plain maps (weekday rules/city codes) can be replaced wholesale.
        if any('properties' in schema for schema in schemas):
            raise ValueError('Update individual model fields with dot paths')
        if any(schema.get('type') == 'object' for schema in schemas):
            def has_model(schema):
                return any('properties' in s or (isinstance(s.get('additionalProperties'), dict)
                           and has_model(s['additionalProperties'])) for s in variants(schema))
            if any(has_model(schema) for schema in schemas):
                raise ValueError('Update individual dictionary entries with dot paths')
        if path in result:
            raise ValueError('Duplicate settings paths')
        result[path] = deepcopy(value)
    for path, value in changes.items():
        visit(path, value)
    paths = sorted(result)
    if any(b.startswith(a+'.') for i, a in enumerate(paths) for b in paths[i+1:]):
        raise ValueError('Overlapping settings paths')
    return result


def read_source(path):
    if path.is_symlink():
        raise ValueError('Settings writes do not follow symlinks')
    return path.read_bytes() if path.exists() else None


def atomic_write(path, content, mode=0o600):
    fd, name = tempfile.mkstemp(prefix='.settings-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as handle:
            os.fchmod(handle.fileno(), mode)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def prepared(changes):
    config = safe_config()
    before = config.model_dump(mode='json')
    candidate = deepcopy(before)
    for path, value in changes.items():
        assign(candidate, path.split('.'), value)
    try:
        after = Settings.model_validate(candidate).model_dump(mode='json')
    except Exception:
        # Pydantic errors may contain credential references or arbitrary user text.
        raise ValueError('Settings validation failed; check field types, ranges and cross-field constraints') from None
    home = job_agent_home()
    sources = {name: read_source(home/name) for name in ('config.yaml', 'daily.yaml')}
    texts = {name: (raw or b'').decode('utf-8') for name, raw in sources.items()}
    import yaml
    try:
        for text in texts.values():
            document(text)
    except Exception:
        raise ValueError('Settings edits require valid mappings without duplicate keys, anchors, aliases or merges') from None
    main = yaml.safe_load(texts['config.yaml']) or {}
    extension = yaml.safe_load(texts['daily.yaml']) or {}
    diffs = []
    for path in changes:
        parts = path.split('.')
        old, new = lookup(before, parts), lookup(after, parts)
        if old == new:
            continue
        # load_config merges sections shallowly: a top-level section child in main
        # owns its entire subtree, even when the exact leaf is still implicit.
        child = parts[:2]
        sentinel = object()
        owner = ('daily.yaml' if len(parts)>1 and lookup(main, child, sentinel) is sentinel
                 and lookup(extension, child, sentinel) is not sentinel else 'config.yaml')
        texts[owner] = patch_value(texts[owner], parts, new)
        diffs.append(dict(path=path, before=redacted({parts[-1]: old})[parts[-1]], after=new, file=owner))
    return sources, texts, diffs


def settings_history():
    from job_agent.service.settings_history import history
    return history(job_agent_home())


def _commit_sources(home, targets, diffs, *, operation, actor, reason, source,
                    rollback_of=None, target_modes=None):
    from job_agent.service import settings_history as history
    # Parse every new YAML text before touching disk: other processes (monitor,
    # heartbeat, daily) may read the file between a write and its rollback.
    for name, raw in targets.items():
        if raw is not None and name.endswith(('.yaml', '.yml')):
            try:
                document(raw)
            except Exception:
                raise ValueError('Settings write failed; rendered YAML invalid, nothing written') from None
    names = set(targets) | {'config.yaml'}
    originals = {name: read_source(home/name) for name in names}
    modes = {name: stat.S_IMODE((home/name).stat().st_mode) if originals[name] is not None else 0o600
             for name in names}
    row = history.begin(home, originals, diffs, modes=modes, operation=operation,
                        actor=actor, reason=reason, source=source, rollback_of=rollback_of)
    try:
        for name, raw in sorted(targets.items()):
            if raw is None:
                (home/name).unlink(missing_ok=True)
            else:
                atomic_write(home/name, raw, (target_modes or modes)[name])
        actual = load_config().model_dump(mode='json')
        if operation == 'set' and any(lookup(actual, d['path'].split('.')) != d['after'] for d in diffs):
            raise ValueError('Settings readback mismatch')
        history.append_event(home, dict(id=row['id'], status='applied'))
    except BaseException as error:
        try:
            for name in sorted(targets):
                if originals[name] is None:
                    (home/name).unlink(missing_ok=True)
                else:
                    atomic_write(home/name, originals[name], modes[name])
        except BaseException:
            raise ValueError('Settings recovery failed; inspect private history snapshots') from None
        try:
            history.append_event(home, dict(id=row['id'], status='failed'))
        except OSError:
            pass  # The durable pending record does not claim success.
        if not isinstance(error, Exception):
            raise
        raise ValueError('Settings write failed; original files rolled back') from None
    result = dict(applied=True, changes=diffs, history_id=row['id'],
                  backups={name: str(history.snapshot_path(home, row['id'], name)) for name in names})
    try:
        history.prune(home)
    except (OSError, ValueError):
        # The validated write and audit are committed; a retention failure must
        # not pretend the write failed or roll back an already-applied journal.
        result['retention_warning'] = 'Settings applied; snapshot pruning needs private inspection'
    return result


def update_settings(changes, confirm=False, *, actor=None, reason=None, source='service'):
    from job_agent.service.settings_history import metadata
    if type(confirm) is not bool:
        raise ValueError('confirm must be a boolean')
    metadata(actor, reason, source, 'set')
    changes = flattened(changes)
    if not confirm:
        _, _, diffs = prepared(changes)
        return dict(applied=False, changes=diffs, backups={})
    from job_agent.service.daily import run_lock
    home = job_agent_home()
    with run_lock(home):
        _, texts, diffs = prepared(changes)
        if not diffs:
            return dict(applied=False, changes=[], backups={})
        targets = {name: texts[name].encode('utf-8') for name in {d['file'] for d in diffs}}
        return _commit_sources(home, targets, diffs, operation='set', actor=actor, reason=reason, source=source)


def _source_diff(name, old, new):
    import yaml
    # Rollback compares explicit stored values, including fields added since the
    # selected version. It restores full files, including original comments.
    def parsed(raw):
        text = (raw or b'').decode('utf-8')
        document(text)
        return yaml.safe_load(text) or {}
    def visit(before, after, path):
        if before is None and isinstance(after, dict):
            before = {}
        if after is None and isinstance(before, dict):
            after = {}
        if isinstance(before, dict) and isinstance(after, dict):
            for key in sorted(before.keys() | after.keys()):
                yield from visit(before.get(key), after.get(key), path+[key])
        elif before != after:
            def safe(value):
                return {'configured': bool(value)} if any(protected(p) for p in path) else redacted({path[-1]: value})[path[-1]]
            yield dict(path='.'.join(path), before=safe(before), after=safe(after), file=name)
    return list(visit(parsed(old), parsed(new), []))


def rollback_settings(history_id, confirm=False, *, actor=None, reason=None, source='service'):
    from job_agent.service import settings_history as history
    if type(confirm) is not bool:
        raise ValueError('confirm must be a boolean')
    history.metadata(actor, reason, source, 'rollback')
    home = job_agent_home()
    def prepare():
        targets, modes = history.restore_sources(home, history_id)
        originals = {name: read_source(home/name) for name in targets}
        diffs = [d for name in sorted(targets) for d in _source_diff(name, originals[name], targets[name])]
        # Exact bytes/absence may differ even when explicit field values do not.
        changed = any(originals[name] != targets[name] for name in targets)
        if changed and not diffs:
            diffs = [dict(path='yaml_format', before='current', after='snapshot', file=next(iter(targets)))]
        return targets, modes, diffs, changed
    if not confirm:
        _, _, diffs, _ = prepare()
        return dict(applied=False, changes=history.audit_changes(diffs), backups={}, rollback_of=history_id)
    from job_agent.service.daily import run_lock
    with run_lock(home):
        targets, modes, diffs, changed = prepare()
        if not changed:
            return dict(applied=False, changes=[], backups={})
        return _commit_sources(home, targets, history.audit_changes(diffs), operation='rollback', actor=actor,
                               reason=reason, source=source, rollback_of=history_id, target_modes=modes)
