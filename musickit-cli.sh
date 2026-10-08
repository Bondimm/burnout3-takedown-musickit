#!/bin/bash
# Command line (macOS/Linux): ./musickit-cli.sh list --iso "Burnout 3 - Takedown.iso"   (see README.md)
HERE="$(cd "$(dirname "$0")" && pwd)" || exit 1
export PYTHONPATH="$HERE${PYTHONPATH:+:$PYTHONPATH}"
exec "$HERE/.venv/bin/python" -m musickit "$@"
