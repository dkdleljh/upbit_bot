#!/usr/bin/env bash
set -euo pipefail

cd /home/zenith/Desktop/upbit_bot

PY=python3
if [ -x .venv/bin/python ]; then
  PY=.venv/bin/python
fi

echo "[smoke] running pytest"
"$PY" -m pytest -q

if [ "${SMOKE_SKIP_HEALTHCHECK:-0}" = "1" ]; then
  echo "[smoke] skipping healthcheck (SMOKE_SKIP_HEALTHCHECK=1)"
  exit 0
fi

echo "[smoke] running healthcheck"
set +e
OUT=$("$PY" -m src.main --health 2>&1)
RC=$?
set -e
echo "$OUT"

if [ "$RC" -ne 0 ]; then
  echo "[smoke] healthcheck command failed with exit code: $RC"
  exit "$RC"
fi

if ! HEALTH_OK=$(OUT="$OUT" "$PY" -c 'import json, os, sys
raw = os.environ.get("OUT", "")
lines = [line.strip() for line in raw.splitlines() if line.strip()]
if not lines:
    sys.exit(2)
try:
    payload = json.loads(lines[-1])
except Exception:
    sys.exit(3)
ok = payload.get("ok")
if not isinstance(ok, bool):
    sys.exit(4)
print("true" if ok else "false")
'); then
  echo "[smoke] healthcheck output did not contain valid JSON with boolean ok"
  exit 1
fi

if [ "$HEALTH_OK" != "true" ]; then
  echo "[smoke] healthcheck reported ok=false"
  exit 1
fi

echo "[smoke] healthcheck status: ok=true"
exit 0
