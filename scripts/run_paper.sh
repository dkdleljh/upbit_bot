#!/usr/bin/env bash
set -euo pipefail
cd /home/zenith/Desktop/upbit_bot
PY=python3
if [ -x .venv/bin/python ]; then
  if .venv/bin/python - <<'PYCHK' >/dev/null 2>&1
import yaml
PYCHK
  then
    PY=.venv/bin/python
  fi
fi
exec "$PY" -m src.main --mode paper
