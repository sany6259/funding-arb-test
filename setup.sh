#!/usr/bin/env bash
# Prepare a local development environment without modifying system Python.
set -euo pipefail
cd "$(dirname "$0")"
command -v python3 >/dev/null || { echo "Python 3.12+ is required." >&2; exit 1; }
python3 -c 'import sys; sys.exit(sys.version_info < (3, 12))' || { echo "Python 3.12+ is required." >&2; exit 1; }
if [[ ! -x .venv/bin/python ]]; then
    python3 -m venv .venv || { echo "Could not create .venv; install the python3-venv package." >&2; exit 1; }
fi
.venv/bin/python -m pip install -r server/requirements.txt
if [[ ! -e .env ]]; then
    cp -n .env.example .env
    echo "Created .env from .env.example."
fi
mkdir -p data runs/.state
printf '%s\n' 'Setup complete. Run bash start.sh, or bash start.sh --api-only.' 'For tests: .venv/bin/python -m pip install -r requirements-dev.txt'
