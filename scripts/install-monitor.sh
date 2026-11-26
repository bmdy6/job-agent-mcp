#!/bin/zsh
set -eu
# The Python installer never reads or prints vault values.
exec python3 "${0:A:h}/install-monitor.py" "$@"
