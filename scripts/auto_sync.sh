#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

# Safety: never commit env/secrets
if git status --porcelain | rg -q "^\?\? \.env"; then
  echo "[auto_sync] ERROR: .env is untracked; refusing to proceed" >&2
  exit 2
fi

# Pull latest
if git remote get-url origin >/dev/null 2>&1; then
  git pull --rebase --autostash || true
fi

# Lightweight secret scan (best-effort). Scan only files Git would actually
# sync, and avoid matching this script's own regex.
if command -v rg >/dev/null 2>&1; then
  scan_files="$(mktemp)"
  trap 'rm -f "$scan_files"' EXIT
  git ls-files -co --exclude-standard | rg -v '^(scripts/auto_sync\.sh|.*\.env\.example)$' >"$scan_files" || true
  # Avoid matching placeholder env var names like UPBIT_ACCESS_KEY; scan for generic secrets only.
  if [ -s "$scan_files" ] && xargs -r -d '\n' rg -n "(-----BEGIN |AKIA[0-9A-Z]{16}|gho_[A-Za-z0-9]{20,})" -S --hidden <"$scan_files" >/dev/null 2>&1; then
    echo "[auto_sync] ERROR: potential secret detected by regex scan" >&2
    exit 3
  fi
fi

# If there are changes (e.g., docs/autogen), commit and push
if [ -n "$(git status --porcelain)" ]; then
  git add -A
  git commit -m "chore(auto): sync $(date +%F)" || true
  git push || true
  echo "OK: $ROOT_DIR pushed" >&2
else
  echo "[auto_sync] clean" >&2
fi
