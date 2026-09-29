#!/usr/bin/env bash
# Mark folders as trusted in ~/.claude.json (hasTrustDialogAccepted), so unattended
# AFClaude launches never stop at the "trust this folder?" prompt.
# Meant to be run by the USER (the auto-mode classifier won't let a Claude session
# edit its own config).
#
#   ./trust_dirs.sh              # the default AFClaude folders below
#   ./trust_dirs.sh DIR [DIR..]  # specific folders
#
# Only folders inside /mnt/BlockVolume/Claude (override: KA_TRUST_ROOT). Missing
# folders are created. Writes atomically and keeps a backup (~/.claude.json.bak-trust).
# Note: a running Claude process may later rewrite ~/.claude.json from its in-memory
# copy and drop the flag; the script re-reads and reports, so just run it again if a
# folder shows up as missing.
set -euo pipefail
ROOT="${KA_TRUST_ROOT:-/mnt/BlockVolume/Claude}"
W="$ROOT/work"
if [ $# -eq 0 ]; then
  set -- "$W/AFClaude" "$W/AFClaude/probe" "$W/AFClaude/data" "$W/AFClaude-poc" \
         "$W/spending-categorizer-report-generator" "$W/microslop-goolag-extension"
fi
mkdir -p "$@"
cp -p ~/.claude.json ~/.claude.json.bak-trust
python3 - "$ROOT" "$@" <<'PY'
import json, os, sys, tempfile
root = os.path.realpath(sys.argv[1])
path = os.path.expanduser("~/.claude.json")
data = json.load(open(path))
projects = data.setdefault("projects", {})
wanted = []
for d in sys.argv[2:]:
    d = os.path.realpath(d)
    if d != root and not d.startswith(root + os.sep):
        print(f"skip (outside {root}): {d}")
        continue
    wanted.append(d)
    p = projects.setdefault(d, {})
    print(("already trusted: " if p.get("hasTrustDialogAccepted") is True else "trusting:        ") + d)
    p["hasTrustDialogAccepted"] = True
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".claude.json.")
with os.fdopen(fd, "w") as fh:
    json.dump(data, fh, indent=2)
os.chmod(tmp, os.stat(path).st_mode & 0o777)
os.replace(tmp, path)
check = json.load(open(path)).get("projects", {})
bad = [d for d in wanted if check.get(d, {}).get("hasTrustDialogAccepted") is not True]
print("verified: all trusted" if not bad else f"NOT trusted after write (rerun): {bad}")
PY
