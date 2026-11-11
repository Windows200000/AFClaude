#!/usr/bin/env bash
# Run by the USER (not by Claude; D-121): installs the AFClaude host kit and the SSH bridge to it, so the
# manager container can ask this host to run a small whitelist of commands (docker/host-kit/host_exec.py).
#
#   docker/install_host_bridge.sh --root DIR [--root DIR ...] [--data DIR] [--from CIDR] [--claude FILE]
#     --root    an allowed root (a launch's cwd must be inside one); at least one; only used when
#               host.json does not exist yet (change it later with the setup, or delete the file)
#     --data    the data directory the container mounts (default: <checkout>/data); the bridge key goes
#               into <data>/bridge/
#     --from    the `from=` of the authorized_keys line: the network the runner connects from
#               (default $AFCLAUDE_BRIDGE_FROM or 172.16.0.0/12, Docker's private range)
#     --claude  the Claude binary (default ~/.local/bin/claude)
#
# What it does, in this order (docs/phase5b_plan.md §3.2):
#   1. copies the kit (host_exec.py, ka_resume.sh) to <share>/afclaude/host-kit/<version>/ (share =
#      $XDG_DATA_HOME or ~/.local/share); a version dir is never changed: a different file under an
#      installed version means HOST_KIT_VERSION was not bumped, and the install stops
#   2. writes <share>/afclaude/host.json (mode 0600) unless there is one that passes the kit's own check
#   3. generates the bridge key <data>/bridge/host_bridge_ed25519 (mode 0600) unless it exists
#   4. self-test of the new kit directly (host_exec.py as sshd would run it: allowed and rejected command)
#   5. points <share>/afclaude/host-kit/current at the version, and puts ONE restricted line into
#      ~/.ssh/authorized_keys (command=current/host_exec.py, from=, no pty/forwarding); a line with the
#      same key is replaced, every other line stays. It checks the result, and when that fails puts the
#      file and the link back and exits 1
# The OLD bridge line (the pre-kit install, comment "afclaude-host-bridge") is never removed here: it stays
# until the new one works; the closing message says how to remove it.
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="$(dirname "$DIR")"
ROOTS=(); DATA="$REPO/data"; FROM="${AFCLAUDE_BRIDGE_FROM:-172.16.0.0/12}"; CLAUDE_BIN="$HOME/.local/bin/claude"
die() { echo "install_host_bridge: $*" >&2; exit 1; }
while [ $# -gt 0 ]; do
  case "$1" in
    --root) [ $# -ge 2 ] || die "--root needs a directory"; ROOTS+=("$2"); shift 2;;
    --data) [ $# -ge 2 ] || die "--data needs a directory"; DATA="$2"; shift 2;;
    --from) [ $# -ge 2 ] || die "--from needs a network"; FROM="$2"; shift 2;;
    --claude) [ $# -ge 2 ] || die "--claude needs a file"; CLAUDE_BIN="$2"; shift 2;;
    -h|--help) sed -n '2,/^set -euo/p' "$0" | sed '$d'; exit 0;;
    *) die "unknown argument $1 (see --help)";;
  esac
done
[[ "$FROM" =~ ^[0-9A-Fa-f:.,/*?!-]+$ ]] || die "--from: only addresses, networks and patterns, got '$FROM'"
[[ "$DATA" == /* && "$CLAUDE_BIN" == /* ]] || die "--data and --claude must be absolute paths"
SHARE="${XDG_DATA_HOME:-$HOME/.local/share}"
[[ "$SHARE" == /* ]] || die "the share directory '$SHARE' is not absolute"
command -v python3 >/dev/null || die "python3 is needed"
command -v ssh-keygen >/dev/null || die "ssh-keygen is needed"

BASE="$SHARE/afclaude"; KITS="$BASE/host-kit"; HOSTJSON="$BASE/host.json"
SRC_EXEC="$DIR/host-kit/host_exec.py"; SRC_KA="$REPO/ka_resume.sh"
[ -f "$SRC_EXEC" ] && [ -f "$SRC_KA" ] || die "kit sources not found ($SRC_EXEC, $SRC_KA)"
VERSION="$(python3 "$SRC_EXEC" --print-version)"
[[ "$VERSION" =~ ^[0-9A-Za-z._-]+$ ]] || die "bad kit version '$VERSION'"
VDIR="$KITS/$VERSION"
KEY="$DATA/bridge/host_bridge_ed25519"
AK="$HOME/.ssh/authorized_keys"
STAGE=""; PREV_LINK=""; AK_SAVED=""
cleanup() { [ -z "$STAGE" ] || rm -rf "$STAGE"; }
trap cleanup EXIT

selftest() {   # $1: a kit directory; host_exec.py exactly as sshd runs it: SSH_ORIGINAL_COMMAND, no arguments
  local out rc
  out="$(env -u SSH_CONNECTION SSH_ORIGINAL_COMMAND=kit-version "$1/host_exec.py")" || { echo "  allowed command (kit-version): FAILED" >&2; return 1; }
  python3 -c 'import json, sys; sys.exit(0 if json.loads(sys.argv[1])["kit_version"] == sys.argv[2] else 1)' "$out" "$VERSION" \
    || { echo "  kit-version does not report $VERSION: FAILED" >&2; return 1; }
  echo "  allowed command: OK"
  rc=0; env -u SSH_CONNECTION SSH_ORIGINAL_COMMAND="cat /etc/passwd" "$1/host_exec.py" >/dev/null 2>&1 || rc=$?
  [ "$rc" = 126 ] || { echo "  rejected command: FAILED (exit $rc, wanted 126)" >&2; return 1; }
  echo "  rejected command: OK"
}

# 1. the kit, as a new version directory
mkdir -p "$KITS"; chmod 700 "$BASE" "$KITS"
if [ -d "$VDIR" ]; then
  cmp -s "$SRC_EXEC" "$VDIR/host_exec.py" && cmp -s "$SRC_KA" "$VDIR/ka_resume.sh" \
    || die "kit version $VERSION is installed with different files: bump HOST_KIT_VERSION in docker/host-kit/host_exec.py (a new whitelist command or a changed shape is a new version)"
  echo "kit version $VERSION already installed: $VDIR"
else
  STAGE="$(mktemp -d "$KITS/.stage.XXXXXX")"
  install -m 755 "$SRC_EXEC" "$STAGE/host_exec.py"
  install -m 755 "$SRC_KA" "$STAGE/ka_resume.sh"
fi

# 2. host.json
if [ -e "$HOSTJSON" ] || [ -L "$HOSTJSON" ]; then
  python3 "$SRC_EXEC" --check-config "$HOSTJSON" || die "$HOSTJSON exists and is not valid: fix or delete it, then run the install again"
  echo "host.json kept: $HOSTJSON"
else
  [ "${#ROOTS[@]}" -ge 1 ] || die "no host.json yet: give the allowed root(s) with --root DIR"
  args=(--write-config "$HOSTJSON" --repo "$REPO" --claude "$CLAUDE_BIN")
  for r in "${ROOTS[@]}"; do args+=(--root "$r"); done
  python3 "$SRC_EXEC" "${args[@]}" || die "could not write $HOSTJSON"
  echo "host.json written: $HOSTJSON"
fi

# 3. the bridge key, in the data dir (travels with the data dir, never with the repo)
mkdir -p "$DATA/bridge"; chmod 700 "$DATA/bridge"
[ -f "$KEY" ] || ssh-keygen -q -t ed25519 -N '' -C afclaude-host-bridge-kit -f "$KEY"
chmod 600 "$KEY"
[ -f "$KEY.pub" ] || ssh-keygen -y -f "$KEY" > "$KEY.pub"

# 4. self-test of the new kit before anything points at it
echo "self-test of the kit:"
selftest "${STAGE:-$VDIR}"
if [ -n "$STAGE" ]; then mv -T "$STAGE" "$VDIR"; STAGE=""; fi

# 5. current link and authorized_keys line; both are put back when the final check fails
[ -L "$KITS/current" ] && PREV_LINK="$(readlink "$KITS/current")"
ln -s "$VERSION" "$KITS/.current.$$"; mv -T "$KITS/.current.$$" "$KITS/current"
mkdir -p "$HOME/.ssh"; chmod 700 "$HOME/.ssh"; touch "$AK"; chmod 600 "$AK"
AK_SAVED="$AK.bak-afclaude"; cp -p "$AK" "$AK_SAVED"
BLOB="$(cut -d' ' -f2 "$KEY.pub")"
LINE="command=\"$KITS/current/host_exec.py\",from=\"$FROM\",no-pty,no-port-forwarding,no-agent-forwarding,no-X11-forwarding,no-user-rc $(cat "$KEY.pub")"
NEWAK="$(mktemp "$AK.XXXXXX")"
{ grep -vF -- " $BLOB" "$AK" || true; printf '%s\n' "$LINE"; } > "$NEWAK"
chmod 600 "$NEWAK"; mv "$NEWAK" "$AK"

back_out() {
  cp -p "$AK_SAVED" "$AK"
  if [ -n "$PREV_LINK" ]; then ln -s "$PREV_LINK" "$KITS/.current.$$"; mv -T "$KITS/.current.$$" "$KITS/current"; else rm -f "$KITS/current"; fi
  die "$1; authorized_keys and the current link are back as they were"
}
echo "self-test of the line:"
FP="$(ssh-keygen -lf "$KEY.pub" | cut -d' ' -f2)"
FPS="$(ssh-keygen -lf "$AK" || true)"      # (captured: a `| grep -q` under pipefail can fail on SIGPIPE)
grep -qF -- "$FP" <<<"$FPS" || back_out "the key is not readable from $AK"
KEYLINES="$(grep -F -- " $BLOB" "$AK" || true)"
grep -qF -- "command=\"$KITS/current/host_exec.py\",from=\"$FROM\",no-pty" <<<"$KEYLINES" || back_out "the line in $AK is not the one written"
[ "$(wc -l <<<"$KEYLINES")" = 1 ] || back_out "more than one line carries the bridge key"
selftest "$KITS/current" || back_out "the kit behind the current link fails its self-test"
echo "  authorized_keys line: OK"

echo
echo "host kit $VERSION installed: $VDIR (current -> $VERSION)"
echo "  authorized_keys: $AK (backup of the state before: $AK_SAVED)"
echo "  bridge key for the container: $KEY (compose still mounts the old key until the deploy that switches it)"
OLD="$(grep -c ' afclaude-host-bridge$' "$AK" || true)"
if [ "$OLD" != 0 ]; then
  echo
  echo "$OLD old bridge line(s) (the pre-kit install, comment 'afclaude-host-bridge') are still in $AK."
  echo "They stay until the container uses the new key and the new bridge worked. Then remove them with:"
  echo "  sed -i.bak-afclaude '/ afclaude-host-bridge\$/d' $AK"
fi
