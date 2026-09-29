#!/usr/bin/env python3
"""Failure alerts for AFClaude.

notify(subject, body) always appends to ALERTS.md (and PROGRESS.md), then tries a
first-party Claude push notification via a tiny headless Haiku run (PushNotification
tool only, dontAsk). Claude Code suppresses the push when it thinks the user is at
a terminal ("Not sent — this terminal is active"), so the push is best-effort; the
tool's answer is recorded next to the alert.

CLI: notify.py "subject" "body"
"""
import json
import os
import subprocess
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
ALERTS = os.path.join(HERE, "ALERTS.md")
PROGRESS = os.path.join(HERE, "PROGRESS.md")
BERLIN = ZoneInfo("Europe/Berlin")
ENV = {"HOME": os.path.expanduser("~"), "USER": "opc", "PATH": "/home/opc/.local/bin:/usr/bin:/bin",
       "LANG": "C.UTF-8"}


def push(text):
    prompt = f'Use the PushNotification tool (status "proactive") to send exactly this text, then reply DONE: "{text}"'
    try:
        r = subprocess.run(["claude", "-p", "--model", "haiku", "--no-session-persistence",
                            "--output-format", "stream-json", "--verbose", "--permission-mode", "dontAsk",
                            "--tools=PushNotification", "--allowedTools=PushNotification",
                            "--strict-mcp-config"], input=prompt, cwd=os.path.join(HERE, "data"),
                           env=ENV, capture_output=True, text=True, timeout=180)
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


def notify(subject, body="", do_push=True):
    now = datetime.now(BERLIN).strftime("%Y-%m-%d %H:%M")
    os.makedirs(os.path.join(HERE, "data"), exist_ok=True)
    result = push(f"AFClaude: {subject}"[:180]) if do_push else "push skipped"
    with open(ALERTS, "a") as fh:
        fh.write(f"- **{now}** {subject}\n  {body.strip()[:1500]}\n  _(push: {result})_\n")
    with open(PROGRESS, "a") as fh:
        fh.write(f"- {now[11:]} [ALERT] {subject} (details in ALERTS.md)\n")
    return result


if __name__ == "__main__":
    print(notify(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else ""))
