#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON_BIN="${PYTHON:-python3}"

if [ ! -x .venv/bin/python ]; then
  "$PYTHON_BIN" -m venv .venv
fi

.venv/bin/python -m pip install --disable-pip-version-check --quiet -r requirements.txt

PYTHONPATH=src .venv/bin/python -m compileall src tests
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests
