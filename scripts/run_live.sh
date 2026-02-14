#!/usr/bin/env bash
set -euo pipefail
cd /home/zenith/Desktop/upbit_bot
[ -f /home/zenith/.upbit_bot.env ] && . /home/zenith/.upbit_bot.env
PY=python3
if [ -x .venv/bin/python ]; then
  if .venv/bin/python - <<'PYCHK' >/dev/null 2>&1
import yaml
PYCHK
  then
    PY=.venv/bin/python
  fi
fi
# 이미 실행 중이면(락파일의 PID가 살아있으면) 조용히 종료
LOCK=.upbit_bot.lock
if [ -f "$LOCK" ]; then
  PID=$(cat "$LOCK" 2>/dev/null || true)
  if [ -n "$PID" ] && ps -p "$PID" -o args= 2>/dev/null | grep -q "src.main --mode live"; then
    echo "upbit_bot already running (pid=$PID)" >&2
    exit 0
  fi
fi

exec "$PY" -m src.main --mode live
