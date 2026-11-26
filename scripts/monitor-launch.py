#!/usr/bin/env python3
"""Load only configured Telegram secrets into the child environment, never output them."""
import json
import os
from pathlib import Path
import sys
import yaml


def main():
    runtime = Path(__file__).resolve().parents[1]
    home = Path(os.environ.get('JOB_AGENT_HOME', '~/.job-agent')).expanduser()
    config = yaml.safe_load((home / 'config.yaml').read_text()) or {}
    env = dict(os.environ)
    vault = json.loads((Path.home() / '.config/ai-vault/vault.json').read_text())
    def find(node, key):
        values = []
        if isinstance(node, dict):
            if key in node and isinstance(node[key], dict) and 'value' in node[key]:
                values.append(node[key]['value'])
            for value in node.values():
                values.extend(find(value, key))
        return values
    for channel in config.get('notify', {}).get('channels', {}).values():
        if channel.get('type') == 'telegram' and channel.get('enabled'):
            key = channel.get('bot_token_env', 'JOB_AGENT_TG_BOT_TOKEN')
            values = find(vault, key)
            if len(values) != 1 or not isinstance(values[0], str) or not values[0].strip():
                raise ValueError('Configured Telegram credential missing or ambiguous in vault')
            env[key] = values[0]
    executable = str(runtime / '.venv/bin/job-agent')
    os.execve(executable, [executable, 'monitor'], env)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('monitor launcher failed error_type=' + type(error).__name__, file=sys.stderr)
        sys.exit(1)
