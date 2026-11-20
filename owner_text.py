#!/usr/bin/env python3
"""Plain words for the owner (D-345, D-347): what an alert's phone push and the quickview's pause
banner say, kept apart from the technical detail.

    "a lot of them seem kinda nothing or illegabpe because they are too condensed for me as a human
     to get it without the whole context" (D-345)

A fail-stop (failstop.fail) or an alert (notify.notify) keeps its technical subject and detail: they
go to the component's log, the stored pause reason and the alerts doc, where the task-manager reads
them. The owner sees only the owner text, found here from that subject: RULES (a pattern on the
subject -> the sentence) for the known subjects, runner job failures by the job's plain name (JOBS),
and GENERIC for every subject without a rule (never the raw subject). One table serves both the push
(push_line, at alert time) and the banner (for_pause, from the stored reason at export time), so the
two always say the same, and no schema holds a second text. Every text here passes
actions.notice_unreadable (test_owner_text checks each one): no ids, commands, file names, hashes or
code names, and no "Nothing to do" line (D-347). What stops, pauses and alerts does not change
(D-234/D-235/D-257/D-282): only the words the owner sees.
"""
from __future__ import annotations

import re
from typing import Optional

PUSH_MAX = 180          # a push longer than this is cut (at a sentence, else a word)
GENERIC = "Something inside AFClaude went wrong. The task-manager has the technical details."
HAND = "AFClaude was paused by hand."

LOGIN = ("Your Claude login on the server has expired, so AFClaude can't use Claude. Sign in to Claude again in a "
         "terminal on the server (Remote Control can't do it).")
# The quickview's login card (export_quickview.auth_block, D-320/D-347): same facts as LOGIN, more room.
LOGIN_CARD_TITLE = "Your Claude login has expired"
LOGIN_CARD = ("AFClaude can't use Claude until you sign in again, so no automatic runs can work meanwhile. To fix it, "
              "open Claude Code in a terminal on the server and sign in to Claude again (this can't be done over "
              "Remote Control). This notice goes away by itself once Claude works again.")
LOGIN_UNKNOWN_TITLE = "AFClaude can't tell if your Claude login still works"
LOGIN_UNKNOWN = ("Its saved login status could not be read. If automatic runs stop working, check that you are still "
                 "signed in to Claude on the server. The task-manager has the technical details.")
USAGE = ("AFClaude couldn't read how much of your Claude usage is left, so it holds new work rather than risk running "
         "into your limit.")
BUDGET = "AFClaude couldn't work out how much of your weekly Claude usage it may use, so it holds new work."
CRASH = "A part of AFClaude stopped with an unexpected error, so it holds new work until the cause is found."
START = "A planned work session did not start, because a check right before the start failed."
RECORD = "AFClaude couldn't save the record of a step it took, so it stopped to keep its records correct."
INBOX_MSG = ("A message you sent could not be passed on to the task-manager. It is still saved; the work it is about "
             "waits until the problem is fixed.")
DB = "AFClaude can't use its database right now, so nothing new starts. It goes on by itself once the database works again."
REVIEW = "A scheduled review of how much Claude usage AFClaude may use did not finish."

# (pattern on the subject, the owner's sentence); the first match wins, so the specific ones come first.
RULES: tuple[tuple[re.Pattern[str], str], ...] = tuple((re.compile(p, re.I), t) for p, t in (
    # the Claude login (claude_auth.SUBJECT, host check logged_in)
    (r"^Claude login expired|^host check logged_in failed", LOGIN),
    # records that could not be written (run_log rows, hand-offs and notes not marked)
    (r"could not be written to run_log|its \w+ row could not be written|not marked (?:consumed|included)", RECORD),
    # the usage reading and the budget model
    (r"usage reading failed", USAGE),
    (r"^budget model failed|^last-stretch slot time failed", BUDGET),
    # session starts and hand-overs
    (r"^start of \S+ aborted|did NOT continue .*preflight failed|at a (?:postponed )?session-window start$", START),
    (r"^the new task-manager of .* did not reply|^no reply from \S+ .*to its hand-off",
     "A new task-manager session was started but did not answer, so AFClaude stopped instead of leaving the work "
     "half handed over."),
    (r"^compaction of \S+ failed",
     "A task-manager session got too long and could not be tidied up safely (its notes for carrying on were "
     "missing), so AFClaude stopped instead of losing work."),
    (r"^compaction \S+ of \S+ still recorded as running",
     "The tidy-up of a long task-manager session is still marked as running although it should have finished."),
    (r"session ran longer than one session length", "A Claude session has been running longer than planned."),
    (r"--session disagrees with the task-manager registry",
     "AFClaude's session watcher was started for the wrong session, so it stopped."),
    (r"^missed session-window start",
     "AFClaude missed a planned start of work by more than half an hour (the server or its service was probably "
     "down), so it did not start late."),
    (r"^the previous runner did not exit cleanly",
     "AFClaude's background service did not shut down cleanly last time (for example the server restarted), so it "
     "holds new work until nothing is left half done."),
    (r"^a scheduled job has no valid run time|^a planned act is not valid",
     "One of AFClaude's planned jobs has an invalid time or setting, so it was not run."),
    (r"^AFCLAUDE_LAYOUT is not valid", "AFClaude's setup on the server is not valid, so it could not start."),
    (r"^the master key is not usable", "AFClaude can't unlock its stored secrets, so it could not start."),
    # crashes and failed regular passes
    (r"(?:web service|inbox API) was not running|web service crashed",
     "The service behind your message box and dashboard had stopped."),
    (r"\bcrashed\b|^(?:watcher|postponed window-start|fill-up|compaction) pass failed|"
     r"^long-running session check failed", CRASH),
    # your messages (inbox)
    (r"^inbox message", INBOX_MSG),
    (r"^inbox (?:activity|delivery) check failed",
     "AFClaude couldn't check whether your messages reached the task-manager, so it holds work to make sure none "
     "get lost."),
    (r"^proposal wake failed", "AFClaude couldn't wake the task-manager to look at its agents' suggestions."),
    (r"was not running$", "A background service of AFClaude had stopped and is being started again."),
    # the server health check (hostcheck.py; logged_in is LOGIN above)
    (r"^host check disk failed", "The server is running low on disk space."),
    (r"^host check clock failed", "The server's clock is off, which can make AFClaude start work at the wrong time."),
    (r"^host check usage failed", "The server's health check couldn't read your Claude usage."),
    (r"^host check \S+ failed",
     "A health check of the server found a problem with the tools AFClaude needs to run Claude."),
    # the usage reviews (usage_review.py)
    (r"review stopped at the usage limit: retried",
     "A review of how much Claude usage AFClaude may use hit your usage limit; it runs again after the limit resets."),
    (r"review hit the usage limit",
     "A scheduled review of how much Claude usage AFClaude may use ran into your usage limit and could not finish."),
    (r"review not started: automation is paused",
     "A planned review of how much Claude usage AFClaude may use was skipped because AFClaude is paused."),
    (r"\breview\b.*(?:did not finish|died|launch failed|scheduling failed|launch was lost)", REVIEW),
    (r"^the nightly task .* was not done", "A planned nightly task was not finished."),
    # run-now requests (run_now.py)
    (r"^run-now request .* failed", "A run you asked to start right away failed."),
    # the database (store.py's error path, the sampler's check, a pause that can't be read)
    (r"DB error \(|^database check failed", DB),
    (r"DB healthy again", "AFClaude's database works again, and work goes on."),
    (r"^no database at|pause setting could not be read",
     "AFClaude can't read its own database, so it holds all work to be safe."),
    (r"tried to write to AFClaude's database after the switch",
     "Something on the server outside AFClaude tried to change its database and was blocked. Tell the task-manager."),
    (r"^restored from a backup",
     "AFClaude's data was restored from a backup, so it waits until someone checks that everything is in order."),
    (r"prompt override\(s\) no longer fit",
     "After an update, some custom instructions for AFClaude's sessions no longer fit; the sessions that use them "
     "can't start until they are fixed."),
    # roles (actions.py)
    (r"^owner role given to",
     "Full owner rights over AFClaude were given to another user or machine. If that wasn't you, tell the "
     "task-manager right away."),
    (r"^role refusal:", "AFClaude blocked one of its agents from doing something it isn't allowed to do."),
    (r"roles_enforce setting|^a refusal (?:could not be audited|alert could not be sent)|"
     r"shadow log could not be written|^the read filter failed",
     "AFClaude's permission check for its agents failed, so it holds work to stay on the safe side."),
    # scheduled backups (backup.py, phase 5c B3)
    (r"^the daily database backup failed",
     "The daily backup of AFClaude's database failed, so no new backup was made. The older backups are kept."),
    # proposals, deploys
    (r"agent proposal\(s\) waiting for review",
     "Suggestions from AFClaude's agents have been waiting a long time for the task-manager's review."),
    (r"^deploy:", "An update of AFClaude on the server did not go through cleanly."),
    (r"^cut-over: the ROLLBACK failed",
     "Moving AFClaude to its new setup failed, and so did going back to the old one. Tell the task-manager right away."),
    (r"^cut-over:", "Moving AFClaude to its new setup on the server failed."),
))

# a runner job (runner.JOBS) by its plain name, for "<job> failed" / "timed out" / "interrupted by a runner restart"
JOBS = {
    "stall_scan": "the check for stuck sessions",
    "sampler": "the regular reading of your Claude usage",
    "dispatcher": "the planner that decides when work starts",
    "quickview": "the update of your dashboard page",
    "docs_import": "the import of the project notes",
    "prune_logs": "the nightly clean-up of old logs",
    "prune_runs": "the nightly clean-up of old run records",
    "status_snapshot": "the regular status snapshot",
    "proposal_alert": "the reminder about agents' suggestions waiting for review",
    "host_check": "the hourly health check of the server",
    "backup": "the daily backup of AFClaude's database",
    "usage_review": "the regular review of how much Claude usage AFClaude may use",
    "window_start": "the planned start of a work session",
    "at": "a scheduled one-off job",
    "watch": "the watch over running sessions",
    "run_readings": "the usage readings during a run",
    "compaction_check": "the check whether a long session needs tidying up",
    "last_stretch": "the last work session before your usage resets",
    "deferred_start": "a postponed start of a work session",
    "fillup": "an extra work session to use spare usage",
    "compaction": "the tidy-up of a long task-manager session",
    "inbox": "the handling of your messages",
    "inbox_verify": "the check that your messages arrived",
    "request": "a run you asked for",
}
JOB_SUBJECT = re.compile(r"^(?P<job>\w+) (?P<what>failed|timed out|interrupted by a runner restart)$")
JOB_WHAT = {"failed": "failed", "timed out": "took too long and was stopped",
            "interrupted by a runner restart": "was cut off by a restart of AFClaude's background service"}

STOPPED = re.compile(r"^AFClaude STOPPED(?: \((?P<project>[^)]*)\))?: (?P<subject>.*)$", re.S)
# one entry of a pause reason failstop.fail stored: "<dd.mm. HH:MM> <component>: <subject>", several joined by " | also: "
ENTRY = re.compile(r"(?:^|\| also: )\d\d\.\d\d\. \d\d:\d\d [\w.-]+: (.+?)(?= \| also: \d\d\.\d\d\. |$)", re.S)


def matched(subject: str) -> Optional[str]:
    """The owner text of a technical subject (a fail-stop's or an alert's), or None without a rule."""
    s = " ".join(subject.split())
    m = JOB_SUBJECT.match(s)
    if m and m["job"] in JOBS:
        job = JOBS[m["job"]]
        return f"{job[0].upper()}{job[1:]} {JOB_WHAT[m['what']]}."
    for rx, text in RULES:
        if rx.search(s):
            return text
    return None


def for_subject(subject: str) -> str:
    """The owner text of a subject: its rule's sentence, else GENERIC (never the raw subject)."""
    return matched(subject) or GENERIC


def fit(text: str, limit: int = PUSH_MAX) -> str:
    """`text` cut to `limit` characters: at the last full sentence that fits, else at a word."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = cut.rfind(". ")
    if end > limit // 2:
        return cut[:end + 1]
    return cut[:cut.rfind(" ")].rstrip(",;:") + "…"


def push_line(subject: str) -> str:
    """The phone push of an alert with this subject (notify.notify): a fail-stop's ("AFClaude STOPPED[ (project)]:
    ...") says AFClaude paused (or paused that project), any other alert starts with "AFClaude:"."""
    m = STOPPED.match(subject.strip())
    if m:
        head = f"AFClaude paused work on {m['project']}: " if m["project"] else "AFClaude paused: "
        return fit(head + for_subject(m["subject"]))
    return fit("AFClaude: " + for_subject(subject))


def for_pause(reason: Optional[str], by: Optional[str] = None) -> list[str]:
    """The banner's sentences for a stored pause reason (failstop.fail's entries, a pause set by hand, a backup
    restore, a pause that can't be read): one sentence per distinct cause, in order. A hand-set reason (not by a
    runner) is shown as written when it is plain words, else HAND."""
    reason = (reason or "").strip()
    subjects = ENTRY.findall(reason)
    if subjects:
        out: list[str] = []
        for s in subjects:
            t = for_subject(s)
            if t not in out:
                out.append(t)
        return out
    known = matched(reason) if reason else None
    if known:
        return [known]
    if not reason or (by or "").startswith("runner:"):
        return [GENERIC]
    import actions      # late: actions is heavy, and the export and notify import this module
    return [reason if not actions.notice_unreadable("", reason) else HAND]
