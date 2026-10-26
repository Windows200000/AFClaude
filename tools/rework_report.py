#!/usr/bin/env python3
"""Rework-per-model report (D-307 #7): how much of each model's merged work needed a later fix.

    python3 tools/rework_report.py [--repo PATH] [REV ...]

Convention (documented in prompts/manager.md's merge rule): every merge commit for a reviewed
step carries a trailer `Worker-Model: sonnet|opus|haiku` naming the model the worker ran on; a
commit or branch that fixes a step already merged carries `Reworks: <branch>` naming that step's
branch (the one the original merge commit's subject names: `Merge branch '<branch>'`). One
Worker-Model value is expected per merge commit.

This script walks `git log` (REV is passed straight through as revision args, e.g. a range such
as `base..head`; default: HEAD's whole history), groups merge commits by Worker-Model, and counts
how many of each model's branches a later commit's Reworks trailer names. Prints, per model:
steps merged, steps reworked, rework rate (reworked / merged). A Reworks trailer naming a branch
no merge commit claims (never carried Worker-Model, or the subject doesn't match) is listed
separately as unresolved, not silently dropped (D-235).
"""
import argparse
import re
import subprocess
import sys
from collections import Counter

MERGE_SUBJECT = re.compile(r"^Merge branch '([^']+)'")
# %x1e/%x00/%x1f are git pretty-format escapes (literal text in the --format argument: an argv
# string can't hold an embedded NUL); git itself emits the actual control bytes into its output,
# which RECORD_SEP/FIELD_SEP/VALUE_SEP below split on.
RECORD_SEP_FMT, RECORD_SEP = "%x1e", "\x1e"
FIELD_SEP_FMT, FIELD_SEP = "%x00", "\x00"
VALUE_SEP_FMT, VALUE_SEP = "%x1f", "\x1f"


class Commit:
    __slots__ = ("sha", "subject", "models", "reworks")

    def __init__(self, sha, subject, models, reworks):
        self.sha = sha
        self.subject = subject
        self.models = models
        self.reworks = reworks


def log(repo, revs):
    """Commit, oldest trailers kept verbatim (lower-cased model names), for `revs` (git log
    revision args) in `repo`. Raises subprocess.CalledProcessError if git log fails."""
    fmt = ("%H" + FIELD_SEP_FMT + "%s" + FIELD_SEP_FMT +
           "%(trailers:key=Worker-Model,valueonly,separator=" + VALUE_SEP_FMT + ")" + FIELD_SEP_FMT +
           "%(trailers:key=Reworks,valueonly,separator=" + VALUE_SEP_FMT + ")" + RECORD_SEP_FMT)
    out = subprocess.run(["git", "-C", repo, "log", "--format=" + fmt, *revs],
                          capture_output=True, text=True, check=True)
    commits = []
    for rec in out.stdout.split(RECORD_SEP):
        rec = rec.strip("\n")
        if not rec:
            continue
        sha, subject, models_raw, reworks_raw = rec.split(FIELD_SEP)
        models = [m.strip().lower() for m in models_raw.split(VALUE_SEP) if m.strip()]
        reworks = [r.strip() for r in reworks_raw.split(VALUE_SEP) if r.strip()]
        commits.append(Commit(sha, subject, models, reworks))
    return commits


def branch_of(subject):
    """The branch name of a `Merge branch '<name>'` subject, or None."""
    m = MERGE_SUBJECT.match(subject)
    return m.group(1) if m else None


def build(commits):
    """-> (merged: Counter[model], reworked: Counter[model], unresolved: list[branch]).
    merged counts every commit that carries a Worker-Model trailer; reworked counts, per model,
    how many Reworks trailers (anywhere in the log) name a branch that model's merge commit
    claims (the merge commit's subject is `Merge branch '<branch>'`); unresolved lists Reworks
    values naming a branch no merge commit claims."""
    branch_model = {}
    merged = Counter()
    for c in commits:
        if not c.models:
            continue
        for model in c.models:
            merged[model] += 1
        branch = branch_of(c.subject)
        if branch and branch not in branch_model:
            branch_model[branch] = c.models[0]
    reworked = Counter()
    unresolved = []
    for c in commits:
        for branch in c.reworks:
            model = branch_model.get(branch)
            if model is None:
                unresolved.append(branch)
            else:
                reworked[model] += 1
    return merged, reworked, unresolved


def report(merged, reworked, unresolved):
    models = sorted(set(merged) | set(reworked))
    if not models:
        return "no Worker-Model trailers found"
    lines = []
    for model in models:
        m, r = merged[model], reworked[model]
        rate = "%.0f%%" % (100 * r / m) if m else "n/a"
        lines.append("%-8s merged %-4d reworked %-4d rework rate %s" % (model, m, r, rate))
    if unresolved:
        lines.append("unresolved Reworks (branch not claimed by any Worker-Model merge): " +
                      ", ".join(sorted(set(unresolved))))
    return "\n".join(lines)


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=".", help="path to the git repo (default: cwd)")
    ap.add_argument("revs", nargs="*", help="git log revision args, e.g. a range (default: HEAD's whole history)")
    args = ap.parse_args(argv)
    try:
        commits = log(args.repo, args.revs)
    except subprocess.CalledProcessError as e:
        print("rework_report: git log failed: %s" % (e.stderr or "").strip(), file=sys.stderr)
        return 2
    merged, reworked, unresolved = build(commits)
    print(report(merged, reworked, unresolved))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
