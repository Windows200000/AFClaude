#!/usr/bin/env python3
"""Print the last N user/assistant messages of a session transcript (debug helper)."""
import glob, json, os, sys
sid, n = sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 4
f = next(iter(glob.glob(os.path.expanduser(f"~/.claude/projects/*/{sid}*.jsonl"))), None)
if not f:
    sys.exit(f"no transcript for {sid}")
rows = []
for l in open(f):
    try:
        o = json.loads(l)
    except json.JSONDecodeError:
        continue
    if o.get("type") not in ("user", "assistant"):
        continue
    m = o.get("message") or {}
    c = m.get("content")
    if isinstance(c, list):
        t = " | ".join(x.get("text") or x.get("type", "") + (":" + json.dumps(x.get("input"))[:150] if x.get("input") else "")
                       + (":" + str(x.get("content"))[:200] if x.get("type") == "tool_result" else "") for x in c if isinstance(x, dict))
    else:
        t = str(c)
    rows.append(f"{o.get('timestamp')} {o['type']:9} {m.get('model') or '':22} {t[:300]!r}")
print(f)
print("\n".join(rows[-n:]))
