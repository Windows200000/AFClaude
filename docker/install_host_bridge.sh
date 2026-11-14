#!/usr/bin/env bash
# Run by the USER (not by Claude; D-121), from a shell on the host (an SSH login is fine): installs the AFClaude
# host kit and the SSH bridge to it, so the manager container can ask this host to run a small whitelist of
# commands (docker/host-kit/host_exec.py). This script is the ONLY writer of host.json (D-338): the bridge has
# no command that changes it.
#
#   docker/install_host_bridge.sh --root DIR [--root DIR ...] [--data DIR] [--from CIDR] [--claude FILE]
#     --root    an allowed root (a launch's cwd must be inside one); at least one; only used when
#               host.json does not exist yet (to change it, delete the file and run the install again)
#     --data    the data directory the container mounts read-write (default: <checkout>/data); a copy of the
#               bridge key goes into <data>/bridge/. The kit refuses the Claude binary, the repo and every root
#               inside it (the container can write there), and a launch's cwd inside it
#     --from    the `from=` of the authorized_keys line: the network the runner connects from
#               (default $AFCLAUDE_BRIDGE_FROM or 172.28.0.0/24, compose's AFCLAUDE_SUBNET)
#     --claude  the Claude binary (default ~/.local/bin/claude)
#
# What it does, in this order (docs/phase5b_plan.md §3.2):
#   1. copies the kit (host_exec.py, ka_resume.sh) to <share>/afclaude/host-kit/<version>/ (share =
#      $XDG_DATA_HOME or ~/.local/share); a version dir is never changed: a different file under an
#      installed version means HOST_KIT_VERSION was not bumped, and the install stops
#   2. writes <share>/afclaude/host.json (mode 0600) unless there is one that passes the kit's own check
#      and names this --data
#   3. generates the bridge key in a host-only directory (<share>/afclaude/bridge-key/, 0700) unless it exists,
#      and DERIVES the public key from it (ssh-keygen -y) on every run; only the key type and base64 body of
#      that one line are used. A copy of the private key goes to <data>/bridge/host_bridge_ed25519 (0600,
#      temp file + rename through no-follow directory fds; a symlink there is refused): nothing under <data>
#      (container-writable) is ever read, chmod-ed or fed to ssh-keygen by path (review k2 N1)
#   4. self-test of the new kit directly (host_exec.py as sshd would run it: allowed and rejected command)
#   5. puts ONE restricted line into ~/.ssh/authorized_keys (command=current/host_exec.py, from=, restrict); a
#      line with the same key is replaced, every other line stays; then points <share>/afclaude/host-kit/current
#      at the version. Everything after the first change is undone (authorized_keys from its timestamped
#      backup, the link) when ANY later step fails, and the script exits 1
# The OLD bridge line (the pre-kit install, comment "afclaude-host-bridge") is never removed here: it stays
# until the new one works; the closing message says how to remove it.
set -Eeuo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="$(dirname "$DIR")"
ROOTS=(); DATA="$REPO/data"; FROM="${AFCLAUDE_BRIDGE_FROM:-172.28.0.0/24}"; CLAUDE_BIN="$HOME/.local/bin/claude"
KEEP_BACKUPS=10
die() { echo "install_host_bridge: $*" >&2; exit 1; }
while [ $# -gt 0 ]; do
  case "$1" in
    --root) [ $# -ge 2 ] || die "--root needs a directory"; ROOTS+=("$2"); shift 2;;
    --data) [ $# -ge 2 ] || die "--data needs a directory"; DATA="$2"; shift 2;;
    --from) [ $# -ge 2 ] || die "--from needs a network"; FROM="$2"; shift 2;;
    --claude) [ $# -ge 2 ] || die "--claude needs a file"; CLAUDE_BIN="$2"; shift 2;;
    -h|--help) sed -n '2,/^set -Eeuo/p' "$0" | sed '$d'; exit 0;;
    *) die "unknown argument $1 (see --help)";;
  esac
done
[[ "$FROM" =~ ^[0-9A-Fa-f:.,/!-]+$ ]] || die "--from: only addresses and networks (no wildcards: '*' or '?' would let any host connect), got '$FROM'"
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
KEYDIR="$BASE/bridge-key"; KEY="$KEYDIR/host_bridge_ed25519"   # host-only: the container never sees this directory
DATA_KEY="$DATA/bridge/host_bridge_ed25519"                   # the copy the container uses
AK="$HOME/.ssh/authorized_keys"
STAGE=""; PUBTMP=""; NEWAK=""; PREV_LINK=""; AK_SAVED=""; AK_TOUCHED=0; LINK_SWITCHED=0; DONE=0

cleanup() {   # EXIT: temp files go; a run that changed authorized_keys or the link and did not finish undoes both
  local rc=$?
  set +e
  [ -z "$STAGE" ] || rm -rf "$STAGE"
  [ -z "$PUBTMP" ] || rm -f "$PUBTMP"
  [ -z "$NEWAK" ] || rm -f "$NEWAK"
  [ "$DONE" = 1 ] || rm -f "$KITS/.current.$$"
  if [ "$DONE" != 1 ] && { [ "$AK_TOUCHED" = 1 ] || [ "$LINK_SWITCHED" = 1 ]; }; then
    [ "$AK_TOUCHED" = 1 ] && cp -p "$AK_SAVED" "$AK"
    if [ "$LINK_SWITCHED" = 1 ]; then
      if [ -n "$PREV_LINK" ]; then ln -sfn "$PREV_LINK" "$KITS/.current.$$" && mv -T "$KITS/.current.$$" "$KITS/current"; else rm -f "$KITS/current"; fi
    fi
    echo "install_host_bridge: failed (exit $rc); authorized_keys and the current link are back as they were" >&2
  fi
}
trap cleanup EXIT
trap 'exit 130' INT TERM HUP

selftest() {   # $1: a kit directory; host_exec.py exactly as sshd runs it: SSH_ORIGINAL_COMMAND, no arguments
  local out rc
  out="$(SSH_ORIGINAL_COMMAND=kit-version "$1/host_exec.py")" || { echo "  allowed command (kit-version): FAILED" >&2; return 1; }
  python3 -c 'import json, sys; sys.exit(0 if json.loads(sys.argv[1])["kit_version"] == sys.argv[2] else 1)' "$out" "$VERSION" \
    || { echo "  kit-version does not report $VERSION: FAILED" >&2; return 1; }
  echo "  allowed command: OK"
  rc=0; SSH_ORIGINAL_COMMAND="cat /etc/passwd" "$1/host_exec.py" >/dev/null 2>&1 || rc=$?
  [ "$rc" = 126 ] || { echo "  rejected command: FAILED (exit $rc, wanted 126)" >&2; return 1; }
  echo "  rejected command: OK"
}

publish_key() {   # the private key into <data>/bridge/ for the container, with no path under <data> followed:
  # every step goes through a directory fd opened with O_NOFOLLOW (data itself, then bridge/), the new file is
  # created O_EXCL|O_NOFOLLOW under a temporary name and renamed over the target (a rename replaces a link, it
  # never follows one); a target that is a symlink or not a regular file is refused, not replaced
  python3 - "$DATA" "$KEY" <<'PY' || die "could not put the bridge key into $DATA/bridge (a symlink or a stray file there?)"
import os, secrets, stat, sys
data, src = sys.argv[1:3]
with open(src, "rb") as fh:
    blob = fh.read()
NOFOLLOW_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
dfd = os.open(data, NOFOLLOW_DIR)
try:
    try:
        os.mkdir("bridge", 0o700, dir_fd=dfd)
    except FileExistsError:
        pass
    bfd = os.open("bridge", NOFOLLOW_DIR, dir_fd=dfd)
    try:
        os.fchmod(bfd, 0o700)
        try:
            st = os.stat("host_bridge_ed25519", dir_fd=bfd, follow_symlinks=False)
        except FileNotFoundError:
            st = None
        if st is not None and not stat.S_ISREG(st.st_mode):
            sys.exit("host_bridge_ed25519 is a symlink or not a regular file")
        tmp = ".host_bridge_ed25519." + secrets.token_hex(8)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=bfd)
        try:
            with os.fdopen(fd, "wb") as out:
                out.write(blob)
                out.flush()
                os.fsync(out.fileno())
            os.rename(tmp, "host_bridge_ed25519", src_dir_fd=bfd, dst_dir_fd=bfd)
        except BaseException:
            try:
                os.unlink(tmp, dir_fd=bfd)
            except FileNotFoundError:
                pass
            raise
    finally:
        os.close(bfd)
finally:
    os.close(dfd)
PY
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

# 2. host.json (the data directory first: the kit refuses every path it names inside that directory)
mkdir -p "$DATA"
if [ -e "$HOSTJSON" ] || [ -L "$HOSTJSON" ]; then
  python3 "$SRC_EXEC" --check-config "$HOSTJSON" --data "$DATA" || die "$HOSTJSON exists and is not valid for this install: fix or delete it, then run the install again"
  echo "host.json kept: $HOSTJSON"
else
  [ "${#ROOTS[@]}" -ge 1 ] || die "no host.json yet: give the allowed root(s) with --root DIR"
  args=(--write-config "$HOSTJSON" --repo "$REPO" --claude "$CLAUDE_BIN" --data "$DATA")
  for r in "${ROOTS[@]}"; do args+=(--root "$r"); done
  python3 "$SRC_EXEC" "${args[@]}" || die "could not write $HOSTJSON"
  echo "host.json written: $HOSTJSON"
fi

# 3. the bridge key. The authoritative private key lives in a host-only directory ($BASE/bridge-key, 0700, outside
# the data dir): it is generated, chmod-ed and derived there, and nothing under <data> (the container can swap
# any path there for a symlink at any moment) is ever read, chmod-ed or fed to ssh-keygen by this script. The
# container gets a COPY in <data>/bridge/, written without following links (publish_key below)
[ ! -L "$KEYDIR" ] || die "$KEYDIR is a symlink"
mkdir -p "$KEYDIR"; chmod 700 "$KEYDIR"
[ ! -L "$KEY" ] || die "$KEY is a symlink"
[ -e "$KEY" ] || ssh-keygen -q -t ed25519 -N '' -C afclaude-host-bridge-kit -f "$KEY"
[ -f "$KEY" ] && [ ! -L "$KEY" ] || die "$KEY is not a regular file"
chmod 600 "$KEY"
# The public key is derived from the private one on every run; a .pub file next to it is never read. The
# comment inside the private key is the container's text: only the type and the base64 body of the first
# line are kept (the line must be a single line, no tab, no CR), the comment is this script's own.
PUBOUT="$(ssh-keygen -y -f "$KEY")" || die "ssh-keygen -y could not read the private key $KEY"
case "$PUBOUT" in *$'\n'*|*$'\t'*|*$'\r'*) die "the public key derived from $KEY is not one plain line";; esac
read -r PTYPE BLOB _ <<<"$PUBOUT"
[ "$PTYPE" = ssh-ed25519 ] && [[ "$BLOB" =~ ^[A-Za-z0-9+/]+=*$ ]] || die "the public key derived from $KEY is not an ssh-ed25519 key with a base64 body"
PUBLINE="ssh-ed25519 $BLOB afclaude-host-bridge-kit"
PUBTMP="$(mktemp "$BASE/.pub.XXXXXX")"; printf '%s\n' "$PUBLINE" > "$PUBTMP"
ssh-keygen -lf "$PUBTMP" >/dev/null || die "the derived public key does not parse"
publish_key

# 4. self-test of the new kit before anything points at it
echo "self-test of the kit:"
selftest "${STAGE:-$VDIR}"
if [ -n "$STAGE" ]; then mv -T "$STAGE" "$VDIR"; STAGE=""; fi

# 5. authorized_keys line, then the current link; from here on any failure (set -e, a check, a signal) goes
# through cleanup(), which puts both back. The backup comes first, with a timestamp (the last KEEP_BACKUPS stay)
[ -L "$KITS/current" ] && PREV_LINK="$(readlink "$KITS/current")"
mkdir -p "$HOME/.ssh"; chmod 700 "$HOME/.ssh"; touch "$AK"; chmod 600 "$AK"
TS="$(date +%Y%m%d-%H%M%S)"
AK_SAVED="$AK.bak-afclaude-$TS-$$"; cp -p "$AK" "$AK_SAVED"
AK_TOUCHED=1
NEWAK="$(mktemp "$AK.XXXXXX")"
LINE="command=\"$KITS/current/host_exec.py\",from=\"$FROM\",restrict $PUBLINE"
{ grep -vF -- " $BLOB" "$AK" || true; printf '%s\n' "$LINE"; } > "$NEWAK"
chmod 600 "$NEWAK"; mv "$NEWAK" "$AK"; NEWAK=""
LINK_SWITCHED=1
ln -s "$VERSION" "$KITS/.current.$$"; mv -T "$KITS/.current.$$" "$KITS/current"

echo "self-test of the line:"
FP="$(ssh-keygen -lf "$PUBTMP" | cut -d' ' -f2)"
FPS="$(ssh-keygen -lf "$AK" || true)"      # (captured: a `| grep -q` under pipefail can fail on SIGPIPE)
grep -qF -- "$FP" <<<"$FPS" || die "the key is not readable from $AK"
KEYLINES="$(grep -F -- " $BLOB" "$AK" || true)"
grep -qF -- "command=\"$KITS/current/host_exec.py\",from=\"$FROM\",restrict " <<<"$KEYLINES" || die "the line in $AK is not the one written"
[ "$(wc -l <<<"$KEYLINES")" = 1 ] || die "more than one line carries the bridge key"
selftest "$KITS/current" || die "the kit behind the current link fails its self-test"
echo "  authorized_keys line: OK"
DONE=1

mapfile -t OLDBAKS < <(ls -1t "$AK".bak-afclaude-* 2>/dev/null | tail -n +$((KEEP_BACKUPS + 1)))
for b in ${OLDBAKS[@]+"${OLDBAKS[@]}"}; do rm -f -- "$b"; done

echo
echo "host kit $VERSION installed: $VDIR (current -> $VERSION)"
echo "  authorized_keys: $AK (backup of the state before: $AK_SAVED; the last $KEEP_BACKUPS stay)"
echo "  bridge key for the container: $DATA_KEY (a copy of the host-only $KEY; compose still mounts the old key until the deploy that switches it)"
OLD="$(grep -c ' afclaude-host-bridge$' "$AK" || true)"
if [ "$OLD" != 0 ]; then
  echo
  echo "$OLD old bridge line(s) (the pre-kit install, comment 'afclaude-host-bridge') are still in $AK."
  echo "They stay until the container uses the new key and the new bridge worked. Then remove them with:"
  echo "  sed -i.bak-afclaude-$TS-old '/ afclaude-host-bridge\$/d' $AK"
fi
