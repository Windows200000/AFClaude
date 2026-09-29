#!/usr/bin/env bash
# Run by the USER (not by Claude): lets the AFClaude manager container ask this host
# to run a small whitelist of commands (docker/host_exec.py), over SSH to localhost.
#   - generates a dedicated ed25519 key in docker/secrets/ (gitignored, mode 600)
#   - appends ONE restricted line to ~/.ssh/authorized_keys (idempotent):
#       command=host_exec.py, from=Docker's private range only, no pty/forwarding
#   - self-test: runs host_exec.py directly with one allowed and one rejected command
# Undo: delete the line containing "afclaude-host-bridge" from ~/.ssh/authorized_keys.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
SECRETS="$DIR/secrets"; KEY="$SECRETS/host_bridge_ed25519"
EXEC="$DIR/host_exec.py"
FROM="${AFCLAUDE_BRIDGE_FROM:-172.16.0.0/12}"   # Docker bridge networks; not reachable from the internet
chmod 755 "$EXEC"
mkdir -p "$SECRETS"; chmod 700 "$SECRETS"
[ -f "$KEY" ] || ssh-keygen -q -t ed25519 -N '' -C afclaude-host-bridge -f "$KEY"
chmod 600 "$KEY"
LINE="command=\"$EXEC\",from=\"$FROM\",no-pty,no-port-forwarding,no-agent-forwarding,no-X11-forwarding,no-user-rc $(cat "$KEY.pub")"
mkdir -p ~/.ssh; chmod 700 ~/.ssh; touch ~/.ssh/authorized_keys; chmod 600 ~/.ssh/authorized_keys
if grep -q 'afclaude-host-bridge' ~/.ssh/authorized_keys; then
  echo "bridge key already installed (line with 'afclaude-host-bridge' exists)"
else
  cp -p ~/.ssh/authorized_keys ~/.ssh/authorized_keys.bak-afclaude
  printf '%s\n' "$LINE" >> ~/.ssh/authorized_keys
  echo "installed bridge key (backup: ~/.ssh/authorized_keys.bak-afclaude)"
fi
echo "self-test:"
SSH_ORIGINAL_COMMAND="claude agents --json" "$EXEC" >/dev/null && echo "  allowed command: OK"
if SSH_ORIGINAL_COMMAND="cat /etc/passwd" "$EXEC" 2>/dev/null; then echo "  REJECT TEST FAILED"; exit 1; else echo "  rejected command: OK"; fi
