#!/usr/bin/env python3
"""Public-repo guard: refuse content that must not end up in this (public) repo.

Modes
  check_public.py --staged        added lines + paths of the staged change (pre-commit)
  check_public.py --tree REV      every file of a commit/tree (pre-push, audits)
  check_public.py --pre-push      read git's pre-push stdin; scan each pushed commit's
                                  tree plus the author/committer/message of new commits

What it blocks
  - forbidden paths: local-only notes (BACKLOG.md, ALERTS.md, OPEN_QUESTIONS.md),
    runtime state (data/, run/, probe/, logs, *.db, keepalive_state.json), secrets
    (docker/secrets/, .env, *.key, *.pem, ssh keys), .venv/, __pycache__/
  - private key headers and well-known token formats (GitHub, Anthropic/OpenAI sk-,
    Slack, AWS, Google), plus long high-entropy hex/base64 strings
  - public IPv4 addresses (private, loopback, link-local and documentation ranges pass)
  - the owner's identity: e-mail (stored here only as a SHA-256 hash), and at runtime
    the account/org UUIDs, e-mail and names from ~/.claude.json plus the global git
    identity (read on every run, never written anywhere)
  - extra local terms (domains, host names ...) from a denylist that is NOT in the repo:
    $AFCLAUDE_PUBLIC_DENYLIST or ~/.config/afclaude/public_denylist.txt
    (one term per line, case-insensitive; "re:<regex>" for a regex; '#' comments)

A line containing the marker "public-check: allow" is exempt from the generic secret
and IP checks (not from the identity/denylist checks). Exit status 1 = findings.
Enable the hooks once per clone:  git config core.hooksPath .githooks
"""
import fnmatch
import hashlib
import ipaddress
import json
import math
import os
import re
import subprocess
import sys

ALLOW_MARKER = "public-check: allow"

# sha256 of the owner's e-mail and of its local part (the plain text is never committed)
BLOCKED_HASHES = {  # public-check: allow
    "b3a4a5e61cf870bf225af9ff50146c363dc21179c641e5a78a349932de4cb218",  # public-check: allow
}
LOCAL_PART_HASHES = {  # public-check: allow
    "a8bb23af9dc585cb3bbc8d74db97f3d9925eeb67f1325042ad15aa2c5835614d",  # public-check: allow
}

FORBIDDEN_PATHS = [
    "BACKLOG.md", "ALERTS.md", "OPEN_QUESTIONS.md",
    "data/*", "run/*", "probe/*", "docker/secrets/*", ".venv/*", "*/__pycache__/*", "__pycache__/*",
    "keepalive_state.json", "keepalive.lock", "STOP", "PAUSED",
    "*.log", "*.db", "*.sqlite", "*.sqlite3", "*.bak",
    ".env", "*.env", "*.key", "*.pem", "*.p12", "*.pfx",
    "id_rsa*", "id_ed25519*", "id_ecdsa*", "id_dsa*", ".public-denylist",
]

SECRET_PATTERNS = [
    ("private key", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")),
    ("github token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})")),
    ("sk- api key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}")),
    ("slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("aws key id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("google api key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}")),
    ("gitlab token", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}")),
    ("bearer token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{24,}")),
]
HEX_RE = re.compile(r"(?<![A-Za-z0-9])[0-9a-fA-F]{32,}(?![A-Za-z0-9])")
B64_RE = re.compile(r"(?<![A-Za-z0-9+/_-])[A-Za-z0-9+/_-]{40,}={0,2}")
SRI_RE = re.compile(r"sha(?:256|384|512)-[A-Za-z0-9+/]+={0,2}")
CHECKSUM_CTX_RE = re.compile(r"(?i)sha-?(?:1|224|256|384|512)|checksum|digest|md5")  # published hashes
IPV4_RE = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.]*\d)")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
WORD_RE = re.compile(r"[A-Za-z0-9._+-]{6,}")


def sha(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def entropy(s):
    n = len(s)
    return -sum(c / n * math.log2(c / n) for c in (s.count(ch) for ch in set(s)))


def git(*args, input=None):
    return subprocess.run(["git"] + list(args), input=input, capture_output=True, check=True).stdout


# ------------------------------------------------------------------ identity terms
def identity_terms():
    """Plain-text identity strings, read at runtime only (never printed in full)."""
    terms = []  # (label, lowercase term, word-boundary?)
    try:
        with open(os.path.expanduser("~/.claude.json"), encoding="utf-8") as f:
            d = json.load(f)
        oa = d.get("oauthAccount") or {}
        for k in ("accountUuid", "organizationUuid", "emailAddress"):
            if isinstance(oa.get(k), str) and len(oa[k]) >= 6:
                terms.append(("account " + k, oa[k].lower(), False))
        for k in ("fullName", "displayName"):
            if isinstance(oa.get(k), str) and len(oa[k].strip()) >= 4:
                terms.append(("account " + k, oa[k].strip().lower(), True))
        if isinstance(d.get("userID"), str) and len(d["userID"]) >= 16:
            terms.append(("account userID", d["userID"].lower(), False))
    except (OSError, ValueError):
        pass
    for key in ("user.email", "user.name"):
        try:
            v = subprocess.run(["git", "config", "--global", key], capture_output=True,
                               text=True).stdout.strip()
        except OSError:
            v = ""
        if len(v) >= 4 and v.lower() not in ("noreply@anthropic.com", "claude (ai)"):
            terms.append(("git global " + key, v.lower(), key == "user.name"))
    return terms


def denylist_terms():
    path = os.environ.get("AFCLAUDE_PUBLIC_DENYLIST") or os.path.expanduser(
        "~/.config/afclaude/public_denylist.txt")
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("re:"):
                    out.append(re.compile(line[3:], re.I))
                else:
                    out.append(re.compile(re.escape(line), re.I))
    except OSError:
        pass
    return out


# ------------------------------------------------------------------ checks
class Checker:
    def __init__(self):
        self.ident = identity_terms()
        self.deny = denylist_terms()
        self.findings = []

    def add(self, where, kind, text):
        t = text.strip()
        shown = t[:4] + "…" if len(t) > 4 else "…"
        self.findings.append("%s: %s (%s)" % (where, kind, shown))

    def check_path(self, path):
        base = os.path.basename(path)
        for pat in FORBIDDEN_PATHS:
            if fnmatch.fnmatchcase(path, pat) or fnmatch.fnmatchcase(base, pat):
                self.add(path, "forbidden path (local-only / runtime / secret, see .gitignore)", base)
                return

    def check_identity(self, where, line):
        low = line.lower()
        for m in EMAIL_RE.finditer(line):
            e = m.group(0).lower()
            if sha(e) in BLOCKED_HASHES or sha(e.split("@")[0]) in LOCAL_PART_HASHES:
                self.add(where, "owner's e-mail address", m.group(0))
        for m in WORD_RE.finditer(line):
            w = m.group(0).lower()
            if sha(w) in LOCAL_PART_HASHES:
                self.add(where, "owner's e-mail user name", m.group(0))
        for label, term, word in self.ident:
            if word:
                if re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", low):
                    self.add(where, label, term)
            elif term in low:
                self.add(where, label, term)
        for rx in self.deny:
            m = rx.search(line)
            if m:
                self.add(where, "local denylist term", m.group(0))

    def check_line(self, where, line):
        self.check_identity(where, line)
        if ALLOW_MARKER in line:
            return
        for kind, rx in SECRET_PATTERNS:
            m = rx.search(line)
            if m:
                self.add(where, kind, m.group(0))
        scrub = SRI_RE.sub(" ", line)
        for m in HEX_RE.finditer(scrub if not CHECKSUM_CTX_RE.search(scrub) else ""):
            if entropy(m.group(0).lower()) >= 3.0:
                self.add(where, "long hex string (secret?)", m.group(0))
        for m in B64_RE.finditer(scrub):
            s = m.group(0).rstrip("=")
            if HEX_RE.fullmatch(s) or s.count("/") >= 3 or s.count("_") + s.count("-") >= 4:
                continue  # hex handled above; paths and identifiers
            if (re.search(r"[0-9]", s) and re.search(r"[A-Z]", s) and re.search(r"[a-z]", s)
                    and entropy(s) >= 4.3):
                self.add(where, "long base64-like string (secret?)", s)
        for m in IPV4_RE.finditer(line):
            try:
                ip = ipaddress.IPv4Address(m.group(0))
            except ValueError:
                continue
            if ip.is_global:
                self.add(where, "public IPv4 address", m.group(0))

    def check_blob(self, path, data):
        if b"\0" in data[:8000]:
            if re.search(rb"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----", data):
                self.add(path, "private key (binary file)", "-----")
            return
        text = data.decode("utf-8", "replace")
        for i, line in enumerate(text.splitlines(), 1):
            self.check_line("%s:%d" % (path, i), line)


# ------------------------------------------------------------------ modes
def scan_staged(c):
    names = git("diff", "--cached", "--name-only", "-z", "--diff-filter=ACMR").decode().split("\0")
    for p in filter(None, names):
        c.check_path(p)
    diff = git("diff", "--cached", "-U0", "--no-color", "--no-ext-diff", "--diff-filter=ACMR")
    path, lineno = None, 0
    for raw in diff.decode("utf-8", "replace").splitlines():
        if raw.startswith("+++ "):
            path = raw[6:] if raw.startswith("+++ b/") else None
        elif raw.startswith("@@"):
            m = re.search(r"\+(\d+)", raw)
            lineno = int(m.group(1)) if m else 0
        elif raw.startswith("Binary files") and " b/" in raw:
            p = raw.rsplit(" b/", 1)[1].rsplit(" differ", 1)[0]
            c.check_blob(p, git("show", ":" + p))
        elif path and raw.startswith("+"):
            c.check_line("%s:%d" % (path, lineno), raw[1:])
            lineno += 1
    check_identity_meta(c, "commit author", git("var", "GIT_AUTHOR_IDENT").decode())


def scan_tree(c, rev):
    names = git("ls-tree", "-r", "-z", "--name-only", rev).decode().split("\0")
    for p in filter(None, names):
        c.check_path(p)
        c.check_blob(p, git("cat-file", "blob", "%s:%s" % (rev, p)))


def check_identity_meta(c, where, text):
    for line in text.splitlines():
        c.check_identity(where, line)


def scan_pre_push(c):
    zero = "0" * 40
    for line in sys.stdin.read().splitlines():
        parts = line.split()
        if len(parts) != 4 or parts[1] == zero:
            continue
        local_sha, remote_sha = parts[1], parts[3]
        scan_tree(c, local_sha)
        rng = [local_sha, "--not", "--remotes"] if remote_sha == zero else [local_sha, "^" + remote_sha]
        try:
            commits = git("rev-list", *rng).decode().split()
        except subprocess.CalledProcessError:  # remote sha unknown locally (force push)
            commits = git("rev-list", local_sha, "--not", "--remotes").decode().split()
        for h in commits:
            meta = git("log", "-1", "--format=%an <%ae>%n%cn <%ce>%n%B", h).decode()
            check_identity_meta(c, "commit %s (author/committer/message)" % h[:8], meta)


def main(argv):
    c = Checker()
    if argv[:1] == ["--staged"]:
        scan_staged(c)
    elif argv[:1] == ["--tree"] and len(argv) == 2:
        scan_tree(c, argv[1])
    elif argv[:1] == ["--pre-push"]:
        scan_pre_push(c)
    else:
        print(__doc__)
        return 2
    if c.findings:
        print("check_public: refusing, this repo is public. Findings:", file=sys.stderr)
        for f in c.findings:
            print("  " + f, file=sys.stderr)
        print("Fix: unstage/gitignore the file, redact the text, or (generic secret/IP false "
              "positive only) add the marker '%s' to that line. Do not use --no-verify."
              % ALLOW_MARKER, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
