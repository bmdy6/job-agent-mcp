#!/usr/bin/env python3
"""Review an artifact against v7. Claude/operator owns the explicit --apply step."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'mcp'))
from job_agent.store.reconciliation import apply_results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('artifact', type=Path)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--apply', action='store_true', help='Append deliveries/events/run in one transaction; never requeue')
    args = parser.parse_args()
    print(json.dumps(apply_results(args.database, json.loads(args.artifact.read_text()), apply=args.apply)))


if __name__ == '__main__':
    main()
