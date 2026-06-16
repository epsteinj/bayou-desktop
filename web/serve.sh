#!/usr/bin/env bash
# Serve the bayou UI over a real http origin (for testing the hosted flow, or
# as a trivial local host). For production, deploy ui/ to any static host and
# set the helper's BAYOU_ALLOWED_ORIGINS to that origin.
PORT="${1:-8788}"
cd "$(dirname "$0")/../ui"
echo "bayou UI → http://localhost:$PORT   (helper must be running on :8780)"
exec python3 -m http.server "$PORT" --bind 127.0.0.1
