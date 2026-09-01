#!/usr/bin/env bash
# Runs on the EC2 box. CI copies this file over alongside docker-compose.yml
# and invokes it with GHCR_TOKEN, ACTOR and IMAGE set.
#
#   GHCR_TOKEN  short-lived GITHUB_TOKEN, valid only for the length of the run
#   ACTOR       GitHub user the token belongs to
#   IMAGE       fully qualified image pinned to the deployed commit
set -euo pipefail

: "${GHCR_TOKEN:?GHCR_TOKEN is required}"
: "${ACTOR:?ACTOR is required}"
: "${IMAGE:?IMAGE is required}"

cd "$(dirname "$0")"

if [ ! -f .env ]; then
    echo "No .env here. Create it with GEMINI_API_KEY before deploying." >&2
    exit 1
fi

echo "$GHCR_TOKEN" | docker login ghcr.io -u "$ACTOR" --password-stdin

# IMAGE is interpolated into docker-compose.yml from this environment, pinning
# the deployment to one commit instead of whatever :latest happens to be.
export IMAGE
docker compose pull
docker compose up -d --remove-orphans

echo "$IMAGE" > DEPLOYED
docker logout ghcr.io

# Keeps the disk from filling with superseded torch layers. Only untagged
# images go; the other services on this box keep theirs.
docker image prune -f
