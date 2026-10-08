#!/usr/bin/env bash
# Short real end-to-end test of the tmux path in ka_resume.sh, on a cheap Haiku
# session (a few cents): new -> send-keys continue -> kill -> resume. Checks that
# every reply lands in the SAME transcript (no fork) and that the process has the
# hook bypass env. Alerts via notify.py on failure. Exit 0 = PASS.
# NOT a unit test: it starts a real Claude session and writes ~/.claude.json (trust entry).
# Run it only on purpose: AFCLAUDE_E2E=1 ./test_e2e_tmux.sh
if [ "${AFCLAUDE_E2E:-}" != "1" ]; then
    echo "test_e2e_tmux.sh is a REAL end-to-end test (starts a Claude session, edits ~/.claude.json); set AFCLAUDE_E2E=1 to run it" >&2
    exit 2
fi
set -uo pipefail
DIR="$(dirname "$(readlink -f "$0")")"
CWD="$DIR/probe"; mkdir -p "$CWD"
SID="$(python3 -c 'import uuid; print(uuid.uuid4())')"
T="ka-${SID:0:8}"
fail() { echo "FAIL: $*"; tmux kill-session -t "$T" 2>/dev/null; python3 "$DIR/notify.py" "e2e tmux test FAILED: $*" "session $SID"; exit 1; }
cleanup() { tmux kill-session -t "$T" 2>/dev/null; }
trap cleanup EXIT

wait_reply() {  # wait_reply <token> <seconds>
  local tok="$1" n="$2" f
  for _ in $(seq 1 "$n"); do
    f=$(ls ~/.claude/projects/*/"$SID".jsonl 2>/dev/null | head -1)
    if [ -n "$f" ] && python3 - "$f" "$tok" <<'EOF'
import json, sys
f, tok = sys.argv[1], sys.argv[2]
for l in open(f):
    try: o = json.loads(l)
    except json.JSONDecodeError: continue
    m = o.get("message") or {}
    if o.get("type") == "assistant" and m.get("model") != "<synthetic>":
        c = m.get("content")
        if isinstance(c, list) and any(isinstance(x, dict) and x.get("type") == "text" and tok in x.get("text", "") for x in c):
            sys.exit(0)
sys.exit(1)
EOF
    then return 0; fi
    sleep 2
  done
  return 1
}

common=(--session "$SID" --cwd "$CWD" --model haiku --effort low --name "AFClaude e2e test (test, tmux killed after)")
echo "1/3 new session $SID"
"$DIR/ka_resume.sh" "${common[@]}" --new --message "AFClaude e2e test. Reply with only: e2e-ok1" || fail "launch"
wait_reply e2e-ok1 45 || fail "no reply to new-session prompt"

echo "2/3 send-keys continue"
out=$("$DIR/ka_resume.sh" "${common[@]}" --message "Reply with only: e2e-ok2") || fail "send-keys launcher"
[[ "$out" == sent-keys* ]] || fail "expected send-keys path, got: $out"
wait_reply e2e-ok2 30 || fail "no reply to send-keys message"

PID=$(pgrep -f "session-id $SID" | head -1)
tr '\0' '\n' < /proc/"$PID"/environ 2>/dev/null | grep -q '^CLAUDE_GUARD_DISABLE=1$' || fail "hook bypass env missing in pid $PID"

echo "3/3 kill + resume"
tmux kill-session -t "$T"; sleep 3
out=$("$DIR/ka_resume.sh" "${common[@]}" --message "Reply with only: e2e-ok3") || fail "resume launcher"
[[ "$out" == started* ]] || fail "expected fresh resume, got: $out"
wait_reply e2e-ok3 45 || fail "no reply after resume"
n=$(ls ~/.claude/projects/*/"$SID".jsonl | wc -l)
[ "$n" = 1 ] || fail "expected 1 transcript, found $n"

echo "PASS: $SID (new, send-keys, resume all in one transcript; bypass env present)"
