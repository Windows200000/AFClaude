#!/usr/bin/env bash
# Continue a Claude Code session WITHOUT --bg (bg sessions get retired by the
# daemon after 60 min idle and inherit the daemon's env). The session lives in a
# detached tmux session named ka-<first 8 of uuid>, as a normal interactive,
# Remote-Control-visible process whose env is exactly what this script sets.
#
#   ka_resume.sh --session <full-uuid> --message <text> [--cwd DIR] [--name NAME]
#                [--model M] [--effort E] [--new]
#
# - tmux session already running  -> type the message into it (send-keys)
# - otherwise                      -> start `claude --resume <uuid> ... "<msg>"` in tmux
#   (--new: start a brand-new session with --session-id <uuid> instead of resuming)
set -euo pipefail
DIR="$(dirname "$(readlink -f "$0")")"
SID="" MSG="" CWD=/mnt/BlockVolume/Claude NAME="" MODEL=claude-opus-5-5 EFFORT=high NEW=0
while [ $# -gt 0 ]; do
  case "$1" in
    --session) SID="$2"; shift 2;;
    --message) MSG="$2"; shift 2;;
    --cwd) CWD="$2"; shift 2;;
    --name) NAME="$2"; shift 2;;
    --model) MODEL="$2"; shift 2;;
    --effort) EFFORT="$2"; shift 2;;
    --new) NEW=1; shift;;
    *) echo "unknown arg $1" >&2; exit 2;;
  esac
done
[[ "$SID" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]] || { echo "need full --session uuid" >&2; exit 2; }
[ -n "$MSG" ] || { echo "need --message" >&2; exit 2; }
T="ka-${SID:0:8}"
NAME="${NAME:-$T}"

if tmux has-session -t "=$T" 2>/dev/null; then
  # "=name" (exact match) is valid for has-session but is read as a pane name by send-keys
  tmux send-keys -t "$T" -l "$MSG"
  sleep 1
  tmux send-keys -t "$T" Enter
  echo "sent-keys $T"
  exit 0
fi

# Unattended launches must never stop at the "trust this folder?" prompt. Mark the
# launch dir trusted in ~/.claude.json (per-path, no parent inheritance), but only
# inside the workspace root, so a stray cwd can't auto-trust an arbitrary folder.
TRUST_ROOT="${KA_TRUST_ROOT:-/mnt/BlockVolume/Claude}"
python3 - "$CWD" "$TRUST_ROOT" <<'PY' || echo "warning: could not mark $CWD trusted; launch may stop at the trust prompt" >&2
import json, os, sys, tempfile
cwd, root = (os.path.realpath(p) for p in sys.argv[1:3])
if cwd != root and not cwd.startswith(root + os.sep):
    sys.exit(f"{cwd} is outside {root}; not auto-trusting")
path = os.path.expanduser("~/.claude.json")
data = json.load(open(path))
proj = data.setdefault("projects", {}).setdefault(cwd, {})
if proj.get("hasTrustDialogAccepted") is True:
    sys.exit(0)
proj["hasTrustDialogAccepted"] = True
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".claude.json.")
with os.fdopen(fd, "w") as fh:
    json.dump(data, fh, indent=2)
os.chmod(tmp, os.stat(path).st_mode & 0o777)
os.replace(tmp, path)
print(f"marked {cwd} trusted", file=sys.stderr)
PY

if [ "$NEW" = 1 ]; then START=(--session-id "$SID"); else START=(--resume "$SID"); fi
# Permission mode for unattended runs: auto (classifier judges, nothing hangs) for
# models that support it; dontAsk (anything not pre-allowed is denied, nothing
# hangs) otherwise, e.g. Haiku, which silently falls back to manual under "auto".
case "$MODEL" in
  *haiku*) MODE=dontAsk;;
  *) MODE=auto;;
esac
# Narrow allow rules, checked before the auto-mode classifier, for this launch only:
# AFClaude sessions may start/continue sessions via this script and list sessions.
ALLOW="Bash($DIR/ka_resume.sh:*),Bash(claude agents:*)"
RUN="$DIR/run/$T.sh"
mkdir -p "$DIR/run"
{
  echo '#!/usr/bin/env bash'
  printf 'cd %q || exit 1\n' "$CWD"
  printf 'exec env -i HOME=%q USER=%q LOGNAME=%q SHELL=/bin/bash LANG=C.UTF-8 TERM=xterm-256color ' "$HOME" "$USER" "$USER"
  # Hook bypass for AFClaude-launched sessions only (user-requested, core to the
  # project: unattended runs must not hang on a guard "ask"). The continue
  # message tells the session to read the guard hooks and respect them anyway.
  printf 'XDG_RUNTIME_DIR=%q PATH=%q CLAUDE_GUARD_DISABLE=1 ' "/run/user/$(id -u)" "$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin"
  printf 'claude'
  printf ' %q' "${START[@]}" --remote-control --name "$NAME" --model "$MODEL" --effort "$EFFORT" \
    --permission-mode "$MODE" "--allowedTools=$ALLOW" -- "$MSG"
  echo
} >"$RUN"
chmod 700 "$RUN"
tmux new-session -d -s "$T" -x 220 -y 50 "bash $(printf %q "$RUN")"
echo "started $T"
