#!/bin/sh
set -e

# profiles.json is configuration you edit from the UI, so it lives in the data
# volume rather than the image. Seed it from the image's copy on first boot.
if [ -n "$PROFILES_PATH" ] && [ ! -f "$PROFILES_PATH" ]; then
    mkdir -p "$(dirname "$PROFILES_PATH")"
    if [ -f /app/profiles.json.seed ]; then
        cp /app/profiles.json.seed "$PROFILES_PATH"
        echo "seeded $PROFILES_PATH from the image"
    fi
fi

exec "$@"
