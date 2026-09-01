#!/usr/bin/env bash
# Runs on the EC2 box after a deploy. The container answers on loopback only,
# so this has to be checked from the host rather than from CI.
set -euo pipefail

PORT="${APP_PORT:-8010}"
DEADLINE=$((SECONDS + 300))

while [ "$SECONDS" -lt "$DEADLINE" ]; do
    if curl -fsS "http://127.0.0.1:${PORT}/api/status" >/dev/null 2>&1; then
        echo "answered after ${SECONDS}s"
        exit 0
    fi
    sleep 10
done

echo "no answer on ${PORT} after 300s; last 50 log lines:" >&2
docker logs --tail 50 media-monitoring >&2 || true
exit 1
