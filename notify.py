#!/usr/bin/env python3
"""Failure alerts for AFClaude.

notify(subject, body) tries a first-party Claude push notification via a tiny headless
Haiku run (PushNotification tool only, dontAsk), then always records the alert in the
project's docs in the DB (D-161, design §7.7): an entry in `alerts` and one line in
`progress` (doc.append through actions.py, actor runner:notify; project_docs.append_note).
ALERTS.md is no longer written (D-181: alerts are read with `tasks.py doc show alerts`,
MCP afclaude_doc, the quickview); PROGRESS.md gets the line at the next export. When the
DB itself is broken the alert goes to data/ALERTS.fallback.md (§7.8, D-171). Claude Code
suppresses the push when it thinks the user is at a terminal ("Not sent — this terminal
is active"), so the push is best-effort; the tool's answer is recorded next to the alert.

CLI: notify.py "subject" "body"
"""
import json
import os
import subprocess
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import host  # noqa: E402  (host calls: local on the host, the SSH bridge inside the container)
import project_docs  # noqa: E402  (the alert into the DB: doc.append)
ACTOR = "runner:notify"
BERLIN = ZoneInfo("Europe/Berlin")
ENV = {"HOME": os.path.expanduser("~"), "USER": "opc", "PATH": "/home/opc/.local/bin:/usr/bin:/bin",
       "LANG": "C.UTF-8"}


def push(text):
    prompt = f'Use the PushNotification tool (status "proactive") to send exactly this text, then reply DONE: "{text}"'
    try:
        r = host.run_on_host(["claude", "-p", "--model", "haiku", "--no-session-persistence",
                              "--output-format", "stream-json", "--verbose", "--permission-mode", "dontAsk",
                              "--tools=PushNotification", "--allowedTools=PushNotification",
                              "--strict-mcp-config"], input=prompt, cwd=os.path.join(HERE, "data"),
                             env=ENV, timeout=180)
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"push failed: {e}"
    for line in r.stdout.splitlines():
        try:
            o = json.loads(line)
        except json.JSONDecodeError:
            continue
        for c in (o.get("message") or {}).get("content") or []:
            if isinstance(c, dict) and c.get("type") == "tool_result":
                return str(c.get("content"))[:200]
    return f"push: no tool result (rc={r.returncode})"


def notify(subject: str, body: str = "", do_push: bool = True) -> str:
    now = datetime.now(BERLIN).strftime("%Y-%m-%d %H:%M")
    os.makedirs(os.path.join(HERE, "data"), exist_ok=True)
    result = push(f"AFClaude: {subject}"[:180]) if do_push else "push skipped"
    project_docs.append_note("alerts", f"- **{now}** {subject}\n  {body.strip()[:1500]}\n  _(push: {result})_\n",
                             ACTOR)
    project_docs.append_note("progress", f"- {now[11:]} [ALERT] {subject} (details in the alerts doc)\n", ACTOR)
    return result


if __name__ == "__main__":
    print(notify(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else ""))
