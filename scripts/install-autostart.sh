#!/bin/zsh
# Start Northstar automatically when you log in to this Mac (and restart it if it crashes).
# Undo with: launchctl unload ~/Library/LaunchAgents/com.northstar-finance.plist && rm ~/Library/LaunchAgents/com.northstar-finance.plist
set -e
DIR="$(cd "$(dirname "$0")/.." && pwd)"
PLIST="$HOME/Library/LaunchAgents/com.northstar-finance.plist"
mkdir -p "$DIR/data"
cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.northstar-finance</string>
  <key>ProgramArguments</key><array><string>$DIR/scripts/run.sh</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$DIR/data/server.log</string>
  <key>StandardErrorPath</key><string>$DIR/data/server.log</string>
</dict></plist>
PL
launchctl unload "$PLIST" 2>/dev/null || true
# Remove the agent from before the rename, if it was installed
OLD="$HOME/Library/LaunchAgents/com.ledger-agent.plist"
[ -f "$OLD" ] && launchctl unload "$OLD" 2>/dev/null && rm -f "$OLD"
launchctl load "$PLIST"
echo "Northstar will now start at login. Open http://localhost:8766"
