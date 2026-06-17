#!/usr/bin/env bash
# bayou helper installer — run it directly:
#   curl -fsSL https://raw.githubusercontent.com/epsteinj/bayou-desktop/main/helper/install.sh | bash
#
# Sets up a private venv, installs the engine + agent harness from GitHub, and
# registers a macOS LaunchAgent that auto-starts on login and listens on
# ws://127.0.0.1:8780 for the bayou web UI. Everything stays on your machine.
set -euo pipefail

HELPER_HOME="$HOME/.bayou_desktop"
VENV="$HELPER_HOME/venv"
OSS_DIR="$HELPER_HOME/bayou-oss"
OSS_REPO="${BAYOU_OSS_REPO:-https://github.com/epsteinj/bayou-oss}"
SERVER_URL="${BAYOU_SERVER_URL:-https://raw.githubusercontent.com/epsteinj/bayou-desktop/main/backend/server.py}"
PLIST="$HOME/Library/LaunchAgents/com.bayou.helper.plist"
PORT="${BAYOU_PORT:-8780}"
ALLOWED="${BAYOU_ALLOWED_ORIGINS:-https://epsteinj.github.io}"   # the deployed UI origin
PYV="${BAYOU_PYTHON:-3.12}"

[ "$(uname)" = "Darwin" ] || { echo "✗ bayou helper is macOS-only (needs Apple Silicon + Metal)"; exit 1; }
echo "▸ bayou helper → $HELPER_HOME"
mkdir -p "$HELPER_HOME/logs"

echo "▸ fetching the agent harness…"
if [ -d "$OSS_DIR/.git" ]; then git -C "$OSS_DIR" pull -q || true
else rm -rf "$OSS_DIR"; git clone --depth 1 -q "$OSS_REPO" "$OSS_DIR"; fi
curl -fsSL "$SERVER_URL" -o "$HELPER_HOME/server.py"

echo "▸ creating venv + installing engine (mlx, mlx-lm, fastapi…) — a few minutes"
if command -v uv >/dev/null 2>&1; then
  uv venv --python "$PYV" "$VENV"
  uv pip install --python "$VENV/bin/python" -q \
     mlx mlx-lm fastapi uvicorn "websockets==12" huggingface_hub "$OSS_DIR"
else
  PY="$(command -v "python$PYV" || command -v python3.12 || command -v python3.11 || true)"
  [ -z "$PY" ] && { echo "✗ need Python >= 3.11 (brew install python@3.12, or set BAYOU_PYTHON)"; exit 1; }
  "$PY" -m venv "$VENV"; "$VENV/bin/pip" install -q --upgrade pip
  "$VENV/bin/pip" install -q mlx mlx-lm fastapi uvicorn "websockets==12" huggingface_hub "$OSS_DIR"
fi

echo "▸ registering the background service…"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.bayou.helper</string>
  <key>ProgramArguments</key>
  <array><string>$VENV/bin/python</string><string>$HELPER_HOME/server.py</string></array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>BAYOU_OSS</key><string>$OSS_DIR</string>
    <key>BAYOU_PORT</key><string>$PORT</string>
    <key>BAYOU_ALLOWED_ORIGINS</key><string>$ALLOWED</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$HELPER_HOME/logs/helper.log</string>
  <key>StandardErrorPath</key><string>$HELPER_HOME/logs/helper.log</string>
</dict></plist>
EOF

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
sleep 2
echo ""
echo "✓ bayou helper installed and running."
echo "  → Open  https://epsteinj.github.io/bayou-web/  and it'll connect."
echo "    (First time? the page will offer to download a model.)"
echo "  logs:      $HELPER_HOME/logs/helper.log"
echo "  uninstall: curl -fsSL https://raw.githubusercontent.com/epsteinj/bayou-desktop/main/helper/uninstall.sh | bash"
