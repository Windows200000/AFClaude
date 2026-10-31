#!/usr/bin/env bash
# Live end-to-end test of the compaction step (D-144/D-214, design §5.5; owner D-216) with the
# hand-off saved directly before it (D-232/D-233): on a THROWAWAY Claude session, does the
# keep-alive's own code get a fresh hand-off written, compact the session in place, and continue it
# with that hand-off (not a stale one), and does the session read it?
#
#   bash tools/test_compact_live.sh        (from the repo root; allowed by .claude/settings.json)
#
# What it does, all in ONE throwaway tmux session `afc-compacttest` (no other tmux session is ever
# touched; it refuses to run if that name already exists) and a TEMP AFClaude DB (the live DB is
# only read, to check it got no row):
#   0. makes a temp dir with an empty DB and exports AFCLAUDE_DB / AFCLAUDE_CONFIG /
#      AFCLAUDE_DATA_DIR / DISPATCHER_DATA_DIR / KEEPALIVE_PROGRESS_FILE into it (like testenv.py),
#      for this script's python and for the throwaway session (so its `tasks.py handoff write`
#      lands in the temp DB);
#   1. starts `claude --model haiku --session-id <new uuid> --permission-mode dontAsk` with cwd =
#      the repo's data/ dir (must exist and be trusted already; nothing is created or trusted here),
#      in the clean env ka_resume.sh uses, with no MCP servers (--strict-mcp-config: the afclaude
#      MCP server would write the live DB), Edit/Write denied, and only
#      `python3 <repo>/tasks.py handoff write` allowed;
#   2. sends two short prompts (a codeword task: remember the codeword, skip the commit/PROGRESS
#      steps of a later save prompt, put the codeword into the hand-off) and waits for each reply
#      in the transcript (~/.claude/projects/<encoded cwd>/<uuid>.jsonl);
#   3. runs handle_fire's compaction step with the keep-alive's own code, in one python process:
#      keepalive.compact_with_handoff() (save prompt, wait for a NEW handoffs row of the session,
#      SAVE_TIMEOUT; /compact, wait for the marker, COMPACT_TIMEOUT; the fresh hand-off's text; any
#      failure raises, as it aborts a real start, D-234), then the continue message (that text + a
#      one-line test question instead of the run's continue prompt) and
#      keepalive.consume_handoff(). Only keepalive.fire is replaced: it types into
#      `afc-compacttest` the keep-alive's way (tmux send-keys -l, 1 s, Enter; ka_resume.sh would
#      target the row's afc- name) and saves pane captures; keepalive.log / progress_note / alert only print.
#      No fire or run record is made, no real session is involved;
#   4. checks: a new handoffs row of THIS session in the temp DB; the compaction marker and
#      keepalive.compacted_since(); the continue text is the fresh template (compact_resume.md)
#      with that row; the session's reply to it names the
#      codeword and says FRESH (it read the inserted text); same transcript; the row consumed; the
#      live DB has no row of this session;
#   5. saves pane captures and the step result to run/test_compact_live-<time>.pane.txt, kills the
#      tmux session, deletes the temp dir, prints a PASS/FAIL summary per step with durations;
#      exit 0 = PASS, 1 = FAIL, 2 = could not run.
# Costs a few Haiku turns; up to ~40 min worst case (5 + 10 min save, 5 + 10 min compaction).
# Needs tmux, claude and python3 on PATH.
set -uo pipefail

REPO="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
T=afc-compacttest
CWD="$REPO/data"
LIVE_DB="$REPO/data/afclaude.db"
SID="$(python3 -c 'import uuid; print(uuid.uuid4())')"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$REPO/run"
PANE="$REPO/run/test_compact_live-$STAMP.pane.txt"
CODE="PELICAN-$((RANDOM % 900 + 100))"
REPLY_TIMEOUT=180          # s per normal reply
CREATED=0
TMPD=""

die() { echo "CANNOT RUN: $*" >&2; exit 2; }
cleanup() {
  if [ "$CREATED" = 1 ] && tmux has-session -t "=$T" 2>/dev/null; then
    tmux kill-session -t "=$T"
  fi
  if [ -n "$TMPD" ] && [ -d "$TMPD" ]; then rm -r -- "$TMPD"; fi
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

# ---- 0. temp DB + data dirs (testenv.py's variables), for this script and the throwaway session
TMPD="$(mktemp -d -t afc-compacttest.XXXXXX)" || die "mktemp failed"
export AFCLAUDE_DB="$TMPD/afclaude.db" AFCLAUDE_CONFIG="$TMPD/afclaude.json" AFCLAUDE_DATA_DIR="$TMPD" \
       DISPATCHER_DATA_DIR="$TMPD" KEEPALIVE_PROGRESS_FILE="$TMPD/PROGRESS.md"
STEP="$TMPD/step.json"
echo '{"mcpServers": {}}' >"$TMPD/mcp.json"   # the session's only MCP config (--strict-mcp-config): none

# ---- helpers (the transcript is read with keepalive.py's own readers)
py() { (cd "$REPO" && python3 - "$@"); }
py "$LIVE_DB" <<'PY' || die "could not create the temp DB"
import os, sys
import store
db = os.path.realpath(os.environ["AFCLAUDE_DB"])
if os.path.realpath(store.DB_PATH) != db or db == os.path.realpath(sys.argv[1]):
    sys.exit(f"store.DB_PATH is {store.DB_PATH}, not the temp DB")
store.connect(db, create=True).close()
PY
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
field() {   # field <python expression over r = the step result> -> prints it ('' if missing)
  python3 - "$STEP" "$1" <<'PY'
import json, sys
try:
    r = json.load(open(sys.argv[1]))
    v = eval(sys.argv[2], {"r": r})
except Exception:
    v = ""
print("" if v is None else v)
PY
}

# ---- 1. start the throwaway session (ka_resume.sh's clean env; no --remote-control: throwaway)
RUN="$REPO/run/$T.sh"
{
  echo '#!/usr/bin/env bash'
  printf 'cd %q || exit 1\n' "$CWD"
  printf 'exec env -i HOME=%q USER=%q LOGNAME=%q SHELL=/bin/bash LANG=C.UTF-8 TERM=xterm-256color ' "$HOME" "$USER" "$USER"
  printf 'XDG_RUNTIME_DIR=%q PATH=%q CLAUDE_GUARD_DISABLE=1 ' "/run/user/$(id -u)" "$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin"
  printf 'AFCLAUDE_DB=%q AFCLAUDE_CONFIG=%q AFCLAUDE_DATA_DIR=%q DISPATCHER_DATA_DIR=%q ' \
    "$AFCLAUDE_DB" "$AFCLAUDE_CONFIG" "$TMPD" "$TMPD"
  printf 'claude --model haiku --session-id %q --permission-mode dontAsk --name %q' "$SID" "$T"
  printf ' %q' --strict-mcp-config "--mcp-config=$TMPD/mcp.json" \
    "--allowedTools=Bash(python3 $REPO/tasks.py handoff write:*)" "--disallowedTools=Edit,Write,NotebookEdit"
  echo
} >"$RUN"
chmod 700 "$RUN"
T_START=$SECONDS
tmux new-session -d -s "$T" -x 220 -y 50 "bash $(printf %q "$RUN")" || die "could not start tmux session $T"
CREATED=1
echo "started $T: session $SID in $CWD, temp DB $AFCLAUDE_DB (pane log: $PANE)"

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
declare -A RES DUR
# ---- 2. the codeword task + a second short prompt
S1="$(now)"
send "This is a throwaway test session: there is no project here and nothing to change. Remember this codeword for later: $CODE. When you are later asked to save your work and write a hand-off: skip the commit and PROGRESS.md steps (do not create, edit or commit any file), write the hand-off only with the exact python3 tasks.py command that message gives, and put the codeword into its summary. Reply with just: OK"
wait_reply "$S1" && R1="$REPLY" || { R1="(no reply)"; FAILS+=("no reply to prompt 1"); }
P1="${PATH_T:-}"
S2="$(now)"; send "What is 17 + 25? Reply with just the number."
wait_reply "$S2" && R2="$REPLY" || { R2="(no reply)"; FAILS+=("no reply to prompt 2"); }
[ -n "$P1" ] || { capture "no transcript"; echo "FAIL: no transcript for $SID"; exit 1; }
RES[prompts]=$([ -n "$R1" ] && [ "$R1" != "(no reply)" ] && [ "$R2" != "(no reply)" ] && echo PASS || echo FAIL)
DUR[prompts]=$((SECONDS - T_START))

# ---- 3. handle_fire's compaction step with the keep-alive's own functions (fire -> this tmux session)
capture "before the keep-alive's compaction step"
TS=$SECONDS
echo "running keepalive.compact_with_handoff (save -> /compact -> the fresh hand-off) -> continue (up to ~40 min)"
py "$SID" "$CWD" "$P1" "$T" "$PANE" "$STEP" "$LIVE_DB" <<'PY'
import json, os, subprocess, sys, time
from datetime import datetime, timezone
UTC = timezone.utc   # the host python may be 3.9 (no datetime.UTC)
import keepalive as ka
import store

sid, cwd, path, tname, pane, out_file, live_db = sys.argv[1:8]
if os.path.realpath(store.DB_PATH) in (os.path.realpath(live_db), "") \
        or os.path.realpath(store.DB_PATH) != os.path.realpath(os.environ["AFCLAUDE_DB"]):
    sys.exit(f"refusing: store.DB_PATH {store.DB_PATH} is not the temp DB")


def say(kind, m):
    print(f"  [{kind}] {m}", flush=True)


ka.log = lambda m: say("keepalive.log", m)
ka.progress_note = lambda line: say("progress note", line)
ka.alert = lambda subject, body="": say("alert", f"{subject} | {body}")
store.NOTIFY = lambda subject, body="": "test: not sent"


def capture(label):
    p = subprocess.run(["tmux", "capture-pane", "-p", "-t", tname], capture_output=True, text=True).stdout
    with open(pane, "a") as fh:
        fh.write(f"===== {label} ({datetime.now(UTC).isoformat()})\n{p}\n")


sends = []


def fire(session_id, cwd_, message, plan, new=False, name=None, model=None):
    """keepalive.fire for the throwaway session: ka_resume.sh's send-keys branch, into tmux `tname`."""
    if session_id != sid or plan != "send-keys" or new:
        return 1, "", f"test fire: unexpected call ({session_id[:8]}, {plan}, new={new})"
    what = " ".join(message.split())[:40]
    capture(f"before typing {what!r}")
    r1 = subprocess.run(["tmux", "send-keys", "-t", tname, "-l", message])
    time.sleep(1)
    r2 = subprocess.run(["tmux", "send-keys", "-t", tname, "Enter"])
    time.sleep(3)
    capture(f"3 s after Enter on {what!r}")
    sends.append({"at": datetime.now(UTC).isoformat(), "text": what})
    return (r1.returncode or r2.returncode), f"sent-keys {tname}\n", ""


ka.fire = fire


def ser(d):
    return {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in (d or {}).items()}


def flat(s):
    return " ".join((s or "").split())


res = {"templates": {"compact_resume": flat(ka.load_prompt("compact_resume")).split("{")[0]}}
try:
    res["before_id"] = ka.newest_handoff_id(sid)
    # -- handle_fire's compaction step (keepalive.py): exactly keepalive.compact_with_handoff, minus
    #    the fire/run record; any failure raises failstop.Failed (D-234), as it aborts a real start
    import failstop
    t0 = datetime.now(UTC)
    insert = handoff = None
    try:
        # the throwaway session is no project's task-manager: its hand-off text names the temp PROGRESS.md
        insert, handoff, rec = ka.compact_with_handoff(sid, cwd, path, progress=ka.PROGRESS_FILE)
    except failstop.Failed as e:
        rec = e.record
        res["failed"] = f"{e.why}: {e.detail}" if e.detail else e.why
    t1 = datetime.now(UTC)
    save = rec.get("save") or {}
    res["save"] = save
    sent = ka.parse_ts(rec.get("sent_at")) if rec.get("sent_at") else None   # /compact typed
    res["save_s"] = round(((sent or t1) - t0).total_seconds())
    res["compact_s"] = round((t1 - sent).total_seconds()) if sent else 0
    res["comp"] = {k: v for k, v in rec.items() if k != "save"}
    res["comp"].setdefault("status", "not reached")
    conn = store.connect()
    try:
        res["rows"] = [{"id": h["id"], "session_id": h["session_id"], "created_at": h["created_at"],
                        "summary": (h.get("body") or {}).get("summary")}
                       for h in store.list_handoffs(conn, sid, limit=5)]
    finally:
        conn.close()
    res["insert"] = flat(insert)
    res["handoff_id"] = handoff["id"] if handoff else None
    if insert:
        msg = flat(insert + " [compaction live test] Do not use any tool for this reply. Answer in one line: "
                   "the codeword from the hand-off above, then FRESH if that hand-off was introduced as the one "
                   "you saved before the compaction, or STALE if it was labelled stale.")
        sent_at = datetime.now(UTC)
        rc, o, err = ka.fire(sid, cwd, msg, "send-keys")
        res["continue_sent_at"] = sent_at.isoformat().replace("+00:00", "Z")
        res["continue_rc"] = rc
        if handoff and rc == 0:
            res["consumed"] = ka.consume_handoff(handoff, "test_compact_live")
            conn = store.connect()
            try:
                res["consumed_at"] = (store.get_handoff(conn, handoff["id"]) or {}).get("consumed_at")
            finally:
                conn.close()
except Exception as e:   # noqa: BLE001 - reported as a step failure
    res["error"] = f"{type(e).__name__}: {e}"[:300]
res["sends"] = sends
with open(out_file, "w") as fh:
    json.dump(res, fh)
with open(pane, "a") as fh:
    fh.write("===== keep-alive step result\n" + json.dumps(res, indent=1) + "\n")
PY
DUR[step]=$((SECONDS - TS))
[ -s "$STEP" ] || { capture "no step result"; echo "FAIL: the keep-alive step wrote no result"; exit 1; }
ERR="$(field 'r.get("error")')"
[ -n "$ERR" ] && FAILS+=("keep-alive step raised: $ERR")
FAILED="$(field 'r.get("failed")')"
[ -n "$FAILED" ] && FAILS+=("compact_with_handoff failed (a real start would be aborted, D-234): $FAILED")

# ---- 4a. the save: a new handoffs row of THIS session in the temp DB
SAVE_ST="$(field 'r["save"].get("status")')"
SAVE_ID="$(field 'r["save"].get("handoff_id")')"
BEFORE_ID="$(field 'r["before_id"]')"
ROW="$(field '[x for x in r["rows"] if x["id"] == r["save"].get("handoff_id")][0]')"
ROW_SID="$(field '[x for x in r["rows"] if x["id"] == r["save"].get("handoff_id")][0]["session_id"]')"
ROW_SUM="$(field '[x for x in r["rows"] if x["id"] == r["save"].get("handoff_id")][0]["summary"]')"
if [ "$SAVE_ST" = saved ] && [ -n "$SAVE_ID" ] && [ "$SAVE_ID" -gt "${BEFORE_ID:-0}" ] && [ "$ROW_SID" = "$SID" ]; then
  RES[save]=PASS
else
  RES[save]=FAIL; FAILS+=("save: status $SAVE_ST, row ${SAVE_ID:-none} (before: $BEFORE_ID), session ${ROW_SID:-?}")
fi
grep -qF "$CODE" <<<"$ROW_SUM" || NOTES+=("the hand-off summary does not hold the codeword")
DUR[save]="$(field 'r.get("save_s")')"

# ---- 4b. the compaction: keepalive.compact_session's status, the marker, compacted_since()
COMP_ST="$(field 'r["comp"]["status"]')"
SC="$(field 'r["comp"].get("sent_at")')"
DONE_AT="$(field 'r["comp"].get("done_at")')"
CS="NOT checked"
MARKS="none"
if [ -n "$SC" ]; then
  CS="$(cd "$REPO" && python3 -c "import sys, keepalive as ka; e = ka.compacted_since(sys.argv[1], ka.parse_ts(sys.argv[2])); print('found' if e else 'NOT found')" "$P1" "$SC")"
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
fi
LAT="$(python3 -c "
import sys
from datetime import datetime
a, b = sys.argv[1], sys.argv[2]
p = lambda s: datetime.fromisoformat(s.replace('Z', '+00:00'))
print(round((p(b) - p(a)).total_seconds()) if a and b else '')" "$SC" "$DONE_AT" 2>/dev/null)"
if [ "$COMP_ST" = compacted ] && [ "$CS" = found ]; then RES[compact]=PASS
else RES[compact]=FAIL; FAILS+=("compaction: status $COMP_ST, compacted_since() $CS"); fi
DUR[compact]="$(field 'r.get("compact_s")')"

# ---- 4c. the continue text: the fresh template with that row
INSERT="$(field 'r.get("insert")')"
INS_ID="$(field 'r.get("handoff_id")')"
KIND="$(field '"fresh" if r["insert"] and r["insert"].startswith(r["templates"]["compact_resume"]) else "none" if not r["insert"] else "other"')"
if [ "$KIND" = fresh ] && [ -n "$SAVE_ID" ] && [ "$INS_ID" = "$SAVE_ID" ] && grep -qF "Hand-off #$SAVE_ID " <<<"$INSERT"; then
  RES[continue]=PASS
else
  RES[continue]=FAIL; FAILS+=("continue text: template $KIND, hand-off ${INS_ID:-none} (saved: ${SAVE_ID:-none})")
fi
grep -qF "$CODE" <<<"$INSERT" || NOTES+=("the continue text does not hold the codeword")

# ---- 4d. the session's reply to the continue: it read the inserted hand-off
CSENT="$(field 'r.get("continue_sent_at")')"
TR=$SECONDS
if [ -n "$CSENT" ] && [ "$(field 'r.get("continue_rc")')" = 0 ]; then
  wait_reply "$CSENT" && R3="$REPLY" || R3="(no reply)"
else
  R3="(no continue sent)"
fi
DUR[reply]=$((SECONDS - TR))
capture "after the reply to the continue"
if grep -qF "$CODE" <<<"$R3" && grep -qw FRESH <<<"$R3" && ! grep -qw STALE <<<"$R3"; then RES[reply]=PASS
else RES[reply]=FAIL; FAILS+=("reply to the continue: $R3"); fi
FILES="$(transcript)"
NFILES="$(grep -c . <<<"$FILES")"
SAME="yes"
{ [ "$NFILES" = 1 ] && [ "${PATH_T:-}" = "$P1" ]; } || { SAME="NO ($NFILES files: $FILES)"; FAILS+=("transcript changed"); }

# ---- 4e. consumed in the temp DB; the live DB untouched for this session (read-only)
CONS="$(field 'r.get("consumed")') $(field 'r.get("consumed_at")')"
if [ "$(field 'r.get("consumed")')" = True ] && [ -n "$(field 'r.get("consumed_at")')" ]; then RES[consume]=PASS
else RES[consume]=FAIL; FAILS+=("hand-off not consumed: $CONS"); fi
LIVE_ROWS="$(python3 - "$LIVE_DB" "$SID" <<'PY'
import os, sqlite3, sys
if not os.path.exists(sys.argv[1]):
    print("no live DB"); sys.exit()
c = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
try:
    print(c.execute("SELECT count(*) FROM handoffs WHERE session_id=?", (sys.argv[2],)).fetchone()[0])
finally:
    c.close()
PY
)"
if [ "$LIVE_ROWS" = 0 ] || [ "$LIVE_ROWS" = "no live DB" ]; then RES[livedb]=PASS
else RES[livedb]=FAIL; FAILS+=("the LIVE DB has $LIVE_ROWS hand-off row(s) of the throwaway session $SID"); fi

# ---- 5. summary (the EXIT trap kills the tmux session and deletes the temp dir)
echo
echo "================ compaction live test ($STAMP) ================"
echo "session       $SID (haiku, dontAsk, cwd $CWD, temp DB)"
echo "transcript    $P1"
echo "[${RES[prompts]}] prompts      ${DUR[prompts]}s  1: $R1 | 2: $R2"
echo "[${RES[save]}] save         ${DUR[save]}s  save step: $SAVE_ST, row #${SAVE_ID:-none} (newest before: #$BEFORE_ID), session ${ROW_SID:0:8}"
echo "               summary: ${ROW_SUM:0:160}"
echo "[${RES[compact]}] compaction   ${DUR[compact]}s  compaction: $COMP_ST; markers: $MARKS; Enter->marker ${LAT:-?}s; compacted_since(): $CS"
echo "[${RES[continue]}] continue     template $KIND, hand-off #${INS_ID:-none}: ${INSERT:0:200}"
echo "[${RES[reply]}] reply        ${DUR[reply]}s  $R3 (same transcript: $SAME)"
echo "[${RES[consume]}] consumed     $CONS"
echo "[${RES[livedb]}] live DB      hand-off rows of $SID: $LIVE_ROWS"
echo "keep-alive step ${DUR[step]}s in all; pane log + step result: $PANE"
for n in "${NOTES[@]}"; do echo "note          $n"; done
if [ ${#FAILS[@]} -eq 0 ]; then
  echo "RESULT        PASS"
  exit 0
fi
echo "RESULT        FAIL: $(IFS=';'; echo "${FAILS[*]}")"
exit 1
