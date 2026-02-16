#!/usr/bin/env bash
set -euo pipefail

cd /home/zenith/Desktop/upbit_bot

PY=python3
if [ -x .venv/bin/python ]; then
  PY=.venv/bin/python
fi

echo "[smoke] running pytest"
"$PY" -m pytest -q

echo "[smoke] running healthcheck"
set +e
OUT=$("$PY" -m src.main --health 2>&1)
RC=$?
set -e
echo "$OUT"

if ! echo "$OUT" | grep -q "\"ok\""; then
  echo "[smoke] healthcheck output did not include JSON status"
  exit 1
fi

echo "[smoke] healthcheck exit code: $RC"
exit 0
