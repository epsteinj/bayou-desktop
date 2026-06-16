#!/usr/bin/env bash
# Stop + remove the bayou helper LaunchAgent. Your chats and downloaded models
# in ~/.bayou_desktop are kept — delete that folder to remove them too.
set -euo pipefail
PLIST="$HOME/Library/LaunchAgents/com.bayou.helper.plist"
launchctl unload "$PLIST" 2>/dev/null || true
rm -f "$PLIST"
pkill -f "$HOME/.bayou_desktop/server.py" 2>/dev/null || true
echo "✓ bayou helper stopped + unregistered."
echo "  kept: ~/.bayou_desktop (chats + models). 'rm -rf ~/.bayou_desktop' to remove."
