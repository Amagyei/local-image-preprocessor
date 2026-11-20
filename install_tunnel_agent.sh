#!/bin/sh
# Keeps a reverse SSH tunnel from this Mac to the shop server, so the shop's Studio page can reach
# this image service as http://127.0.0.1:7860 on the server. The service itself keeps listening on
# 127.0.0.1 only; nothing here opens a port to the internet.
#
#   ./install_tunnel_agent.sh           makes the key and the launch agent, prints the line for the server
#   ./install_tunnel_agent.sh --start   loads the agent (do this after the server accepts the key)
#   ./install_tunnel_agent.sh --stop    unloads it
#   ./install_tunnel_agent.sh --check   shows whether the tunnel is up
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
REMOTE="${TUNNEL_REMOTE:-}"   # user@server, e.g. TUNNEL_REMOTE=user@host ./install_tunnel_agent.sh
PORT="${TUNNEL_PORT:-7860}"
KEY="$HOME/.ssh/image-host-tunnel"
KNOWN_HOSTS="${TUNNEL_KNOWN_HOSTS:-$HOME/.ssh/image-host-known_hosts}"   # the server's host keys, checked strictly
LABEL=com.serwakollections.image-host-tunnel
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/image-host-tunnel.log"
DOMAIN="gui/$(id -u)"

case "${1:-}" in
--stop)
	launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
	echo "Tunnel stopped."
	exit 0
	;;
--check)
	launchctl print "$DOMAIN/$LABEL" 2>/dev/null | grep -E "state|pid|last exit" || echo "Agent not loaded."
	tail -n 5 "$LOG" 2>/dev/null || true
	exit 0
	;;
esac

[ -n "$REMOTE" ] || { echo "Set TUNNEL_REMOTE=user@server first." >&2; exit 1; }
[ -f "$KNOWN_HOSTS" ] || { echo "Missing $KNOWN_HOSTS: put the server's host keys there (from a source you trust, not a blind ssh-keyscan)." >&2; exit 1; }
mkdir -p "$HOME/.ssh" "$HOME/Library/LaunchAgents"
chmod 700 "$HOME/.ssh"

if [ ! -f "$KEY" ]; then
	ssh-keygen -t ed25519 -N "" -C "image-host-tunnel" -f "$KEY" >/dev/null
fi

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>Label</key>
	<string>$LABEL</string>
	<key>ProgramArguments</key>
	<array>
		<string>/usr/bin/ssh</string>
		<string>-N</string>
		<string>-T</string>
		<string>-i</string>
		<string>$KEY</string>
		<string>-o</string><string>IdentitiesOnly=yes</string>
		<string>-o</string><string>BatchMode=yes</string>
		<string>-o</string><string>ExitOnForwardFailure=yes</string>
		<string>-o</string><string>ServerAliveInterval=30</string>
		<string>-o</string><string>ServerAliveCountMax=3</string>
		<string>-o</string><string>StrictHostKeyChecking=yes</string>
		<string>-o</string><string>UserKnownHostsFile=$KNOWN_HOSTS</string>
		<string>-R</string>
		<string>127.0.0.1:$PORT:127.0.0.1:$PORT</string>
		<string>$REMOTE</string>
	</array>
	<key>RunAtLoad</key>
	<true/>
	<key>KeepAlive</key>
	<true/>
	<key>ThrottleInterval</key>
	<integer>15</integer>
	<key>StandardOutPath</key>
	<string>$LOG</string>
	<key>StandardErrorPath</key>
	<string>$LOG</string>
</dict>
</plist>
EOF

if [ "${1:-}" = "--start" ]; then
	launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
	for attempt in 1 2 3 4 5 6; do
		launchctl bootstrap "$DOMAIN" "$PLIST" 2>/dev/null && break
		[ "$attempt" = 6 ] && { echo "Couldn't start $LABEL" >&2; exit 1; }
		sleep 3
	done
	echo "Tunnel started. Check it with: $0 --check"
	exit 0
fi

echo "Agent file written (not started): $PLIST"
echo
echo "Add this ONE line to ~$(echo "$REMOTE" | cut -d@ -f1)/.ssh/authorized_keys on the server."
echo "It lets this key open the tunnel and nothing else (no shell, no other ports):"
echo
echo "restrict,port-forwarding,permitlisten=\"127.0.0.1:$PORT\" $(cut -d' ' -f1,2 "$KEY.pub") image-host-tunnel"
echo
echo "Then run: $0 --start"
