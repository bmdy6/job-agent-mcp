#!/usr/bin/env python3
"""Optional launchd template. Does nothing until explicitly run/installed.

Only the exact BossHunter space name is eligible. No delivery, messages,
claim/takeover, finish, or browser-action retries. Requires PyYAML.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import uuid

import yaml

PREFIX = '__BOSSHUNTER_KEEPALIVE__'
DEFAULT_CONFIG = '~/Library/Application Support/BossHunter-示例用户/config.yaml'

SCRIPT = r'''
try {
  const name = "BossHunter 投递";
  const matches = (await listTaskSpaces()).filter(s => s.name === name);
  if (matches.length > 1) throw Error("ambiguous_space");
  const metadata = matches[0];
  if (metadata && metadata.ownership !== "agent") throw Error("space_not_agent_owned");
  const task = await taskSpace(metadata ? metadata.id : name);
  if (task.ownership !== "agent") throw Error("space_not_agent_owned");
  let page;
  if (!metadata) {
    page = task.page("p1");
    await page.goto("https://www.zhipin.com/web/geek/chat");
  } else {
    const tabs = await task.tabs();
    const tab = tabs.find(t => t.label && t.openedBy === "agent" && t.url === "about:blank")
      ?? tabs.find(t => t.label && t.openedBy === "agent");
    if (!tab) throw Error("no_agent_page");
    page = task.page(tab.label);
  }
  if ((await page.evaluate("1")) !== 1) throw Error("pulse_failed");
  console.log("__BOSSHUNTER_KEEPALIVE__" + JSON.stringify({ok:true,space_id:task.spaceId,created:!metadata}));
} catch (error) {
  // Provider errors can contain page data. Emit a bounded generic failure.
  console.log("__BOSSHUNTER_KEEPALIVE__" + JSON.stringify({ok:false,code:"space_unavailable_or_pulse_failed"}));
}
'''


def validate_config(original):
    text = original.decode('utf-8')
    try:
        data = yaml.safe_load(text)
        root = yaml.compose(text)
    except yaml.YAMLError:
        raise ValueError('Invalid configuration YAML') from None
    if not isinstance(data, dict):
        raise ValueError('Configuration must be a mapping')
    # BossHunter reads delivery.ego_task_space_id, not a top-level key.
    # YAML marks locate just that scalar; no reserialization of private config.
    sections = [value for key, value in root.value if key.value == 'delivery']
    if len(sections) != 1 or not isinstance(data.get('delivery'), dict):
        raise ValueError('Configuration needs one delivery mapping')
    section = sections[0]
    if section.flow_style or text[section.start_mark.index:section.start_mark.index + 1] in {'&', '*'}:
        raise ValueError('delivery must be a plain block mapping')
    fields = [value for key, value in section.value if key.value == 'ego_task_space_id']
    value = data['delivery'].get('ego_task_space_id')
    if len(fields) > 1 or value is not None and (type(value) is not int or value < 0):
        raise ValueError('delivery.ego_task_space_id must be a single integer or null')
    if fields:
        node = fields[0]
        raw = text[node.start_mark.index:node.end_mark.index]
        if not re.fullmatch(r'(?:[0-9]+|null|Null|NULL|~)?', raw):
            raise ValueError('Complex space ID syntax is not supported')
        span = node.start_mark.index, node.end_mark.index
    else:
        if 'ego_task_space_id' in data['delivery']:
            raise ValueError('Merged space ID syntax is not supported')
        # Insert before the first child key, using its actual indentation.
        first = section.value[0][0].start_mark
        span = first.index - first.column, first.index - first.column
    return text, data, span


def pulse(executable):
    proc = subprocess.run([str(Path(executable).expanduser()), 'nodejs'], input=SCRIPT,
                          text=True, capture_output=True, timeout=40)
    if proc.returncode:
        raise RuntimeError('Ego CLI failed; no automatic retry')
    try:
        lines = (proc.stdout + '\n' + proc.stderr).splitlines()
        result = json.loads(next(s[len(PREFIX):] for s in reversed(lines) if s.startswith(PREFIX)))
        if (result.get('ok') is not True or type(result.get('space_id')) is not int
                or result['space_id'] <= 0 or type(result.get('created')) is not bool):
            raise ValueError()
    except (StopIteration, ValueError, TypeError, AttributeError):
        raise RuntimeError('Space unavailable or pulse failed; no config change') from None
    return result


def update_config(path, original, space_id):
    if type(space_id) is not int or space_id <= 0:
        raise ValueError('Invalid space ID')
    text, data, span = validate_config(original)
    if path.is_symlink() or path.read_bytes() != original:
        raise ValueError('Configuration changed concurrently or is a symlink; stopped')
    if data['delivery'].get('ego_task_space_id') == space_id:
        return None
    start, end = span
    if 'ego_task_space_id' in data['delivery']:
        updated = text[:start] + str(space_id) + text[end:]
    else:
        newline = '\r\n' if '\r\n' in text else '\n'
        indent = len(text[start:]) - len(text[start:].lstrip(' '))
        updated = text[:start] + ' ' * indent + f'ego_task_space_id: {space_id}{newline}' + text[start:]
    _, check, _ = validate_config(updated.encode('utf-8'))
    expected = {**data, 'delivery': {**data['delivery'], 'ego_task_space_id': space_id}}
    if check != expected:
        raise ValueError('Configuration update would change other fields')
    suffix = uuid.uuid4().hex
    backup = path.with_name(path.name + '.bak-' + suffix)
    fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(original)
        stream.flush()
        os.fsync(stream.fileno())
    temporary = None
    try:
        fd, temporary = tempfile.mkstemp(prefix=path.name + '.tmp-', dir=path.parent)
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), stat.S_IMODE(path.stat().st_mode))
            stream.write(updated.encode('utf-8'))
            stream.flush()
            os.fsync(stream.fileno())
        if path.is_symlink() or path.read_bytes() != original:
            raise ValueError('Configuration changed concurrently; stopped')
        os.replace(temporary, path)
        if path.read_bytes() != updated.encode('utf-8'):
            raise ValueError('Configuration readback mismatch')
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)
    return backup


def run(config_path, executable):
    path = Path(config_path).expanduser()
    if path.is_symlink():
        raise ValueError('Configuration symlinks are not supported')
    # Lock file is separate: atomic replacement changes the config file inode.
    lock_fd = os.open(path.with_name(path.name + '.keepalive.lock'), os.O_CREAT | os.O_WRONLY, 0o600)
    with os.fdopen(lock_fd, 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        original = path.read_bytes()
        validate_config(original)
        result = pulse(executable)
        # Also repair the pointer on an existing space after a prior failed write.
        backup = update_config(path, original, result['space_id'])
        return {**result, 'config_updated': backup is not None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default=DEFAULT_CONFIG)
    parser.add_argument('--ego-browser', default='~/.local/bin/ego-browser')
    args = parser.parse_args()
    try:
        print(json.dumps(run(args.config, args.ego_browser), ensure_ascii=False))
        return 0
    except Exception as error:
        # No configuration contents, secrets, page text or provider output.
        print(json.dumps({'ok': False, 'error_type': type(error).__name__}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
