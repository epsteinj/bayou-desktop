#!/usr/bin/env bash
# Install the bayou helper as a macOS LaunchAgent: it sets up a private venv,
# installs the engine + agent harness, and registers a background service that
# auto-starts on login and listens on ws://127.0.0.1:8780 for the (hosted) UI.
set -euo pipefail

HELPER_HOME="$HOME/.bayou_desktop"
VENV="$HELPER_HOME/venv"
SRC="$(cd "$(dirname "$0")/.." && pwd)"          # bayou-desktop repo root
OSS="${BAYOU_OSS:-$HOME/projects/bayou-oss}"      # the open-sourced bayou harness
PLIST="$HOME/Library/LaunchAgents/com.bayou.helper.plist"
PORT="${BAYOU_PORT:-8780}"

echo "▸ bayou helper → $HELPER_HOME"
mkdir -p "$HELPER_HOME/logs"
cp "$SRC/backend/server.py" "$HELPER_HOME/server.py"

echo "▸ creating venv + installing engine (mlx, mlx-lm, fastapi, the bayou harness)…"
if command -v uv >/dev/null 2>&1; then
  uv venv "$VENV" >/dev/null 2>&1 || true
  uv pip install --python "$VENV/bin/python" -q \
     mlx mlx-lm fastapi uvicorn "websockets==12" huggingface_hub "$OSS"
else
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install -q --upgrade pip
  "$VENV/bin/pip" install -q mlx mlx-lm fastapi uvicorn "websockets==12" huggingface_hub "$OSS"
fi

echo "▸ registering LaunchAgent (auto-start, keep-alive)…"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.bayou.helper</string>
  <key>ProgramArguments</key>
  <array><string>$VENV/bin/python</string><string>$HELPER_HOME/server.py</string></array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>BAYOU_OSS</key><string>$OSS</string>
    <key>BAYOU_PORT</key><string>$PORT</string>
    <key>BAYOU_ALLOWED_ORIGINS</key><string>${BAYOU_ALLOWED_ORIGINS:-}</string>
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
echo "✓ bayou helper running on ws://127.0.0.1:$PORT"
echo "  logs:    $HELPER_HOME/logs/helper.log"
echo "  stop:    helper/uninstall.sh   (chats/models in $HELPER_HOME are kept)"
echo "  Open the bayou site — it will connect automatically. No model yet?"
echo "  the site's first-run screen will download one."
