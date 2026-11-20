#!/bin/sh
# Install usage-gate from this repo (the logic lives in install.py; works on macOS, Linux, Windows).
exec python3 "$(dirname "$0")/install.py" "$@"
