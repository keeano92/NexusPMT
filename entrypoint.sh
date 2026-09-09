#!/usr/bin/env sh
set -eu

BASE="${WORLDMAP_BASE_URL:-http://sk-ai-worldmap:8080}"
PATH_H="${WORLDMAP_HEALTH_PATH:-/api/sidecar-health}"
URL="${BASE}${PATH_H}"

if [ "${SKIP_WORLDMAP_WAIT:-false}" = "true" ]; then
  echo "[entrypoint] SKIP_WORLDMAP_WAIT=true — starting without WorldMap gate"
else
  echo "[entrypoint] waiting for WorldMap liveness: $URL"
  i=0
  while true; do
    if command -v curl >/dev/null 2>&1; then
      if curl -sf "$URL" >/dev/null 2>&1; then
        break
      fi
    elif command -v wget >/dev/null 2>&1; then
      if wget -qO- "$URL" >/dev/null 2>&1; then
        break
      fi
    else
      echo "[entrypoint] curl/wget missing" >&2
      exit 1
    fi
    i=$((i + 1))
    echo "[entrypoint] not ready (attempt $i); sleeping 2s…"
    sleep 2
  done
  echo "[entrypoint] WorldMap returned 200 — starting NexusPMT"
fi
exec uvicorn backend.app.main:app --host "${NEXUSPMT_HOST:-0.0.0.0}" --port "${NEXUSPMT_PORT:-8080}"
