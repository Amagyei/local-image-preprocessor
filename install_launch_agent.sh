#!/bin/sh
# Starts the image service at login, restarts it if it crashes, and keeps the Mac awake while it runs.
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
LABEL=com.serwakollections.local-image-server
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/local-image-server.log"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>Label</key>
	<string>$LABEL</string>
	<key>ProgramArguments</key>
	<array>
		<string>/usr/bin/caffeinate</string>
		<string>-is</string>
		<string>$HERE/start.sh</string>
	</array>
	<key>RunAtLoad</key>
	<true/>
	<key>KeepAlive</key>
	<true/>
	<key>ThrottleInterval</key>
	<integer>10</integer>
	<key>StandardOutPath</key>
	<string>$LOG</string>
	<key>StandardErrorPath</key>
	<string>$LOG</string>
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
# Removing the old copy finishes in the background, so the first tries can fail.
for attempt in 1 2 3 4 5 6; do
	launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null && break
	[ "$attempt" = 6 ] && { echo "Couldn't start $LABEL" >&2; exit 1; }
	sleep 5
done
echo "Installed $PLIST"
