#!/usr/bin/env bash
# Downloads the reranker weights (~2.3 GB) into the hf-cache volume so the
# first real run does not stall on the download. Run once after the first
# deploy; the volume survives every later one.
set -euo pipefail

cd "$(dirname "$0")"

docker compose exec -T media-monitoring python -c "
import os, relevance
print('fetching', relevance.DEFAULT_MODEL, 'into', os.environ.get('HF_HOME'))
relevance.Reranker()
print('model cached')
"
