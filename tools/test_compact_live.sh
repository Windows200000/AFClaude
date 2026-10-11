#!/usr/bin/env bash
# Live test of the compaction step (D-144/D-214, design §5.5; owner D-216): does `/compact <prompt>`,
# typed into a live interactive Claude session exactly as the keep-alive does it, compact the
# session in place, and does the session answer afterwards in the same transcript?
#
#   bash tools/test_compact_live.sh        (from the repo root; allowed by .claude/settings.json)
#
# What it does, all in ONE throwaway tmux session `afc-compacttest` (no other tmux session is ever
# touched; it refuses to run if that name already exists):
#   1. starts `claude --model haiku --session-id <new uuid> --permission-mode dontAsk` with cwd =
#      the repo's data/ dir (must exist and be trusted already; nothing is created or trusted here),
#      in the clean env ka_resume.sh uses;
#   2. sends two short prompts and waits for each reply in the transcript
#      (~/.claude/projects/<encoded cwd>/<uuid>.jsonl);
#   3. sends keepalive.compact_command() the keep-alive's way (tmux send-keys -l, 1 s, Enter) and
#      polls the transcript for a compact_boundary / isCompactSummary entry (10 min timeout);
#      if nothing happened after 20 s and the text still sits in the input box, it sends one more
#      Enter and reports the slash-menu/Enter issue;
#   4. checks keepalive.compacted_since() finds the marker;
#   5. sends a follow-up prompt and checks it is answered in the same transcript (and whether the
#      codeword from step 2 survived the compaction: recall);
#   6. saves pane captures around the /compact step to run/test_compact_live-<time>.pane.txt,
#      kills the tmux session, prints a PASS/FAIL summary; exit 0 = PASS, 1 = FAIL, 2 = could not run.
# Costs a few Haiku turns. Needs tmux, claude and python3 on PATH.
set -uo pipefail

REPO="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
T=afc-compacttest
CWD="$REPO/data"
SID="$(python3 -c 'import uuid; print(uuid.uuid4())')"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$REPO/run"
PANE="$REPO/run/test_compact_live-$STAMP.pane.txt"
CODE="PELICAN-$((RANDOM % 900 + 100))"
REPLY_TIMEOUT=180          # s per normal reply
COMPACT_TIMEOUT=600        # s for the compaction (keepalive.COMPACT_TIMEOUT)
CREATED=0

die() { echo "CANNOT RUN: $*" >&2; exit 2; }
cleanup() {
  if [ "$CREATED" = 1 ] && tmux has-session -t "=$T" 2>/dev/null; then
    tmux kill-session -t "=$T"
  fi
}
trap cleanup EXIT

command -v tmux >/dev/null || die "tmux not found"
command -v claude >/dev/null || die "claude not found"
[ -d "$CWD" ] || die "$CWD does not exist (run from the main checkout, whose data/ dir is trusted)"
python3 - "$CWD" <<'PY' || die "$CWD is not trusted in ~/.claude.json (run ./trust_dirs.sh yourself first)"
import json, os, sys
d = os.path.realpath(sys.argv[1])
p = json.load(open(os.path.expanduser("~/.claude.json"))).get("projects", {}).get(d, {})
sys.exit(0 if p.get("hasTrustDialogAccepted") is True else 1)
PY
tmux has-session -t "=$T" 2>/dev/null && die "tmux session $T already exists (a test still running?); not touching it"

# ---- helpers (the transcript is read with keepalive.py's own readers)
py() { (cd "$REPO" && python3 - "$@"); }
transcript() {
  py "$SID" <<'PY'
import glob, os, sys
import keepalive as ka
hits = glob.glob(os.path.join(ka.PROJECTS_DIR, "*", sys.argv[1] + ".jsonl"))
print("\n".join(hits))
PY
}
now() { date -u +%Y-%m-%dT%H:%M:%S.%3NZ; }
# reply_after <path> <iso time>: prints the text blocks of the latest finished assistant
# message after that time, or nothing if none has a text block yet. A single assistant turn
# can be split across several jsonl entries (e.g. a thinking-only entry followed by a
# separate text entry, both with stop_reason end_turn); thinking-only entries are skipped
# and the caller (wait_reply) keeps polling until a text block shows up.
reply_after() {
  py "$1" "$2" <<'PY'
import sys
import keepalive as ka
path, since = sys.argv[1], ka.parse_ts(sys.argv[2])
found = ""
for e in ka.iter_entries(path):
    if e.get("type") != "assistant" or e.get("isSidechain"):
        continue
    ts = ka.parse_ts(e.get("timestamp"))
    m = e.get("message") or {}
    if ts and ts >= since and m.get("stop_reason") in ("end_turn", "stop_sequence", "max_tokens"):
        text = " ".join(ka.message_text(e).split())
        if text:
            found = text
if found:
    print(found[:200])
PY
}
send() {   # the keep-alive's way (ka_resume.sh): literal text, 1 s, Enter
  tmux send-keys -t "$T" -l "$1"
  sleep 1
  tmux send-keys -t "$T" Enter
}
capture() { { echo "===== $1 ($(now))"; tmux capture-pane -p -t "$T" 2>/dev/null; echo; } >>"$PANE"; }
wait_reply() {   # wait_reply <iso time> -> sets REPLY, PATH_T
  local since="$1" t0=$SECONDS
  REPLY=""
  while [ $((SECONDS - t0)) -lt $REPLY_TIMEOUT ]; do
    PATH_T="$(transcript | head -1)"
    if [ -n "$PATH_T" ]; then
      REPLY="$(reply_after "$PATH_T" "$since")"
      [ -n "$REPLY" ] && return 0
    fi
    sleep 3
  done
  return 1
}

# ---- 1. start the throwaway session (ka_resume.sh's clean env; no --remote-control: throwaway)
RUN="$REPO/run/$T.sh"
{
  echo '#!/usr/bin/env bash'
  printf 'cd %q || exit 1\n' "$CWD"
  printf 'exec env -i HOME=%q USER=%q LOGNAME=%q SHELL=/bin/bash LANG=C.UTF-8 TERM=xterm-256color ' "$HOME" "$USER" "$USER"
  printf 'XDG_RUNTIME_DIR=%q PATH=%q CLAUDE_GUARD_DISABLE=1 ' "/run/user/$(id -u)" "$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin"
  printf 'claude --model haiku --session-id %q --permission-mode dontAsk --name %q\n' "$SID" "$T"
} >"$RUN"
chmod 700 "$RUN"
tmux new-session -d -s "$T" -x 220 -y 50 "bash $(printf %q "$RUN")" || die "could not start tmux session $T"
CREATED=1
echo "started $T: session $SID in $CWD (pane log: $PANE)"

# wait for the TUI: pane content non-empty and unchanged for 3 s (max 60 s)
prev="" stable=0 t0=$SECONDS
while [ $((SECONDS - t0)) -lt 60 ]; do
  cur="$(tmux capture-pane -p -t "$T" 2>/dev/null)" || die "tmux session $T died at startup"
  if [ -n "${cur//[[:space:]]/}" ] && [ "$cur" = "$prev" ]; then stable=$((stable + 1)); else stable=0; fi
  [ $stable -ge 3 ] && [ $((SECONDS - t0)) -ge 5 ] && break
  prev="$cur"; sleep 1
done
capture "startup"
if grep -qiE "trust this folder|do you trust" <<<"$cur"; then
  echo "FAIL: the session stopped at the trust prompt" ; exit 1
fi

FAILS=() NOTES=()
# ---- 2. two short prompts
S1="$(now)"; send "Remember this codeword for later: $CODE. Reply with just: OK"
wait_reply "$S1" && R1="$REPLY" || { R1="(no reply)"; FAILS+=("no reply to prompt 1"); }
P1="${PATH_T:-}"
S2="$(now)"; send "What is 17 + 25? Reply with just the number."
wait_reply "$S2" && R2="$REPLY" || { R2="(no reply)"; FAILS+=("no reply to prompt 2"); }
[ -n "$P1" ] || { capture "no transcript"; echo "FAIL: no transcript for $SID"; exit 1; }

# ---- 3. /compact exactly as keepalive.compact_command() builds it
CMD="$(cd "$REPO" && python3 -c 'import keepalive; print(keepalive.compact_command())')" || die "keepalive.compact_command() failed"
capture "before /compact"
SC="$(now)"; TC=$SECONDS
tmux send-keys -t "$T" -l "$CMD"
sleep 1
capture "/compact typed, before Enter"
tmux send-keys -t "$T" Enter
sleep 3
capture "3 s after Enter"
MENU="no"
MARK="" t0=$SECONDS
while [ $((SECONDS - t0)) -lt $COMPACT_TIMEOUT ]; do
  MARK="$(py "$P1" "$SC" <<'PY'
import sys
import keepalive as ka
e = ka.compacted_since(sys.argv[1], ka.parse_ts(sys.argv[2]))
if e:
    print((e.get("subtype") or "isCompactSummary") + " " + str(e.get("timestamp")))
PY
)"
  [ -n "$MARK" ] && break
  if [ "$MENU" = no ] && [ $((SECONDS - t0)) -ge 20 ]; then
    pane="$(tmux capture-pane -p -t "$T")"
    if ! grep -qiE "compacting" <<<"$pane" && grep -qF "${CMD:0:40}" <<<"$pane"; then
      MENU="yes (text still in the input box 20 s after Enter; sent a second Enter)"
      capture "menu/Enter issue: before the second Enter"
      tmux send-keys -t "$T" Enter
    else
      MENU="no (checked at 20 s)"
    fi
  fi
  sleep 5
done
LAT=$((SECONDS - TC))
capture "after the compaction wait"
if [ -n "$MARK" ]; then
  # both markers? (informational)
  MARKS="$(py "$P1" "$SC" <<'PY'
import sys
import keepalive as ka
since = ka.parse_ts(sys.argv[2])
seen = set()
for e in ka.iter_entries(sys.argv[1]):
    ts = ka.parse_ts(e.get("timestamp")) if e.get("timestamp") else None
    if ts and ts >= since:
        if e.get("type") == "system" and e.get("subtype") == "compact_boundary":
            seen.add("compact_boundary")
        if e.get("isCompactSummary"):
            seen.add("isCompactSummary")
print(", ".join(sorted(seen)) or "none")
PY
)"
else
  MARKS="none within ${COMPACT_TIMEOUT}s"; FAILS+=("no compaction marker")
fi

# ---- 4. keepalive.compacted_since() on it (the keep-alive's own check)
CS="$(cd "$REPO" && python3 -c "import sys, keepalive as ka; e = ka.compacted_since(sys.argv[1], ka.parse_ts(sys.argv[2])); print('found' if e else 'NOT found')" "$P1" "$SC")"
[ "$CS" = found ] || FAILS+=("compacted_since() found nothing")

# ---- 5. follow-up in the same transcript
sleep 5
capture "before the follow-up"
S3="$(now)"; send "What was the codeword I asked you to remember? Reply with just the codeword, or UNKNOWN."
wait_reply "$S3" && R3="$REPLY" || { R3="(no reply)"; FAILS+=("no reply to the follow-up"); }
capture "after the follow-up"
FILES="$(transcript)"
NFILES="$(grep -c . <<<"$FILES")"
SAME="yes"
{ [ "$NFILES" = 1 ] && [ "$PATH_T" = "$P1" ]; } || { SAME="NO ($NFILES files: $FILES)"; FAILS+=("transcript changed"); }
if grep -qF "$CODE" <<<"$R3"; then RECALL="yes ($CODE)"; else RECALL="no (replied: $R3)"; NOTES+=("codeword not recalled after the compaction"); fi

# ---- 6. summary (the EXIT trap kills the tmux session)
echo
echo "================ compaction live test ($STAMP) ================"
echo "session       $SID (haiku, dontAsk, cwd $CWD)"
echo "transcript    $P1"
echo "prompt 1      $R1"
echo "prompt 2      $R2"
echo "/compact      sent $SC; markers: $MARKS${MARK:+ (first: $MARK)}"
echo "latency       ${LAT}s from Enter to the marker (timeout ${COMPACT_TIMEOUT}s)"
echo "compacted_since()  $CS"
echo "menu/Enter    $MENU"
echo "follow-up     $R3"
echo "same file     $SAME"
echo "recall        $RECALL"
echo "pane log      $PANE"
for n in "${NOTES[@]}"; do echo "note          $n"; done
if [ ${#FAILS[@]} -eq 0 ]; then
  echo "RESULT        PASS"
  exit 0
fi
echo "RESULT        FAIL: $(IFS=';'; echo "${FAILS[*]}")"
exit 1
