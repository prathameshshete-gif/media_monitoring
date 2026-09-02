#!/usr/bin/env bash
# Nightly news harvest. Installed as a cron job on the EC2 host.
#
# Runs at 21:00 UTC = 02:30 IST, so "yesterday" in IST is a finished day.
# Two days are harvested, not one: articles published late are sometimes added
# to a sitemap after midnight, and re-harvesting a stored day is nearly free
# because articles already saved are skipped.
set -uo pipefail

cd "$(dirname "$0")"
LOG=./harvest.log

echo "=== $(date -Is) starting harvest ===" >> "$LOG"
docker compose exec -T media-monitoring \
    python harvest.py --days-back 2 >> "$LOG" 2>&1
rc=$?
echo "=== $(date -Is) finished, exit $rc ===" >> "$LOG"

# Keep the log from growing without limit.
tail -n 2000 "$LOG" > "$LOG.trim" && mv "$LOG.trim" "$LOG"

exit $rc
