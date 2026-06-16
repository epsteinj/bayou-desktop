#!/usr/bin/env bash
# Dev convenience: start the telemetry backend, then the Tauri shell.
set -e
( python3 backend/server.py & echo $! > /tmp/bayou_backend.pid )
trap 'kill $(cat /tmp/bayou_backend.pid) 2>/dev/null' EXIT
npm run tauri dev
