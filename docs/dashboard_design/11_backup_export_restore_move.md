## 11. Backup, export, restore, move (D-161, D-165)

### 11.1 DB backup bundle
`backup.py`, a runner job, daily by default; also on demand from F10 (step-up):
1. Consistent snapshot without stopping: SQLite online backup (`VACUUM INTO`).
2. `manifest.json`: installation id and epoch, app version, schema version, created at, row counts per table, sha256 of every file.
3. The master key, wrapped with the backup passphrase.
4. Encrypted as a whole (passphrase → argon2id → authenticated encryption). Stored in `/data/backups` (retention setting), optionally copied off-host (a mounted directory first, S3/SFTP later).
5. The WAL archive of the retention window (below) goes into the bundle, so a restored or moved install can still roll back within that window.

**Point-in-time recovery: the WAL archive (D-175).** The DB keeps its WAL for a reasonable timeframe: WAL changes are archived instead of being discarded at checkpoint, for `db_wal_retention_days` (default 7, range 1–30). The DB can then be rolled back to any moment in that window, e.g. to just before a bad write, a corrupting bug or a wrong bulk change, which a daily bundle alone can't do.
- **Tool: Litestream, not custom code.** Existing options checked first: Litestream (streams SQLite WAL changes to a file, S3 or SFTP target; since v0.5 in the LTX format with compaction and `litestream restore -timestamp` for point-in-time restore, retention configurable), `sqlite3_rsync` (snapshots only, no point in time), LiteFS (FUSE-based replication with a lease, built for multi-node, far heavier than needed), frequent `VACUUM INTO` snapshots (coarse and costly on disk), the SQLite session extension (app-level changesets, much custom code). Litestream fits: it already solves the hard parts (copying WAL frames before a checkpoint can drop them, detecting a break in WAL continuity and re-snapshotting, timestamp restore). Copying the WAL ourselves across three writing containers would repeat that with more risk.
- **How it runs:** the pinned Litestream binary (checksum-verified) is in the image; the runner runs it as a supervised child process (§5.1) with a config generated from the settings, replicating `/data/afclaude.db` to a file replica in `/data/wal-archive` (an off-host target later, like the backups). Per Litestream's guidance every AFClaude connection sets `PRAGMA wal_autocheckpoint = 0` and Litestream does the checkpoints. If Litestream is down, the WAL grows: the runner alerts on replication lag, and past a WAL size limit it checkpoints itself (Litestream re-snapshots when it's back; the gap in the window is shown). The cross-container setup and restore times are verified in phase 5c.
- **Disk cost:** the archive holds the snapshots plus the compressed changes of the window, roughly the DB size plus what was written in `db_wal_retention_days` (for AFClaude's tens-of-MB DB, telemetry appends dominate; expected tens to a few hundred MB for 7 days, measured in 5c). F10 shows the archive's current size and the oldest restorable moment next to the setting; the bundles grow by the archive size.
- **Rollback** (F10 → "roll back to…", step-up; or `afclaude restore --at <time>`): automation pauses and all services stop writing (writes get a clear "maintenance" error), Litestream restores the chosen moment into a new file, the integrity check runs, the current DB is kept as `afclaude.db.before-<time>`, the new file is swapped in, the installation epoch is bumped and the install starts paused like any restore (§11.3). A rollback only rewinds the DB: sessions that ran and commits that were pushed in the meantime stay; the banner says so.

### 11.2 Full export: DB + the required Claude data (D-165)
The dashboard's **Export** (F10, step-up) = the §11.1 bundle + a `claude/` part read from the host through the bridge + a generated reinstate guide. Every item is in the manifest with its checksum, so a restore can prove it is complete.

| item | source | note |
|---|---|---|
| transcripts of AFClaude-run sessions, incl. subagent files and per-session side files (e.g. file history) | `<config>/projects/<encoded working-root paths>/` | selected by `driven_sessions` and the working root; the exact file list is fixed in phase 5d against the real layout |
| credentials | `<config>/.credentials.json` | **included by default** (D-174); the option is shown ticked and in red with the warning that whoever has the bundle and passphrase can use the Claude account and that the old host must stop using it; untick it to leave the credentials out |
| permission setup for the autonomous modes | `settings.json` (allow rules, default permission mode, env) | the bypass env and per-launch flags are code + DB settings, already in the bundle; the project allow rules AFClaude needs ship in the repo's `.claude/settings.json` and the user-level steps (MCP registration, reminder hook, trust) are listed in the README's Permissions section (D-217) |
| hooks | the guard and reminder hook scripts that `settings.json` references | |
| MCP registration | AFClaude's `mcpServers` entry in `.claude.json` | |
| trust flags | `hasTrustDialogAccepted` of the root and project dirs in `.claude.json` | |
| `CLAUDE.md` files | the working root's, and the config dir's user-level one | project repos carry their own in git |
| project repos | not copied: re-cloned from their remotes (the save step pushed); a project without a remote goes in as a `git bundle` | |

Excluded: caches, the owner's other sessions and projects, anything outside the selection. With the shared default dir, the same items are picked out of shared files (by key and path prefix).

### 11.3 Restore and the reinstate guide
The export carries a step-by-step guide generated for its content (also shown in the dashboard):
1. New host: install docker, tmux, Claude Code (the manifest's version or newer), sshd and the bridge (`install_host_bridge.sh`).
2. Deploy the containers with the same `AFCLAUDE_PUBLIC_URL` (passkeys are bound to the domain; a new domain means re-registering passkeys and updating the OIDC redirect URI) and `AFCLAUDE_DATA` pointing at a new, empty data directory (§10.2).
3. `/setup` → "restore from a backup or export" (setup code + passphrase), or `afclaude restore <bundle>`: decrypt, verify checksums and manifest, refuse a schema newer than the code, migrate forward if older, bump the installation epoch, record `restored_from`. The bundle's WAL archive comes along, so the restored DB can be taken at the bundle's latest moment or any earlier one in its window (§11.1).
4. Choose the working root (no default, D-187; the bundle's old path is shown for reference, not prefilled): the same path as before (transcripts are keyed by the encoded cwd, so this needs no path map) or a new one with a path map (§11.5).
5. The runner writes the `claude/` part to the host through a whitelisted restore command: config dir files, trust flags, MCP registration, hooks, `CLAUDE.md`.
6. Sign Claude in for that config dir if the credentials weren't included.
7. Clone the project repos into the root (the guide lists them with their remotes).
8. Post-restore validation: every manifest item present with its checksum, plus all host checks (§10.5) incl. the end-to-end session.
9. The install **starts paused** (D-173, setting `restore_starts_paused`, default on) with a banner "restored from <backup time>: check and resume". One tap resumes; the projects' task-managers `--resume` their session where the transcript is present, otherwise they start fresh from the latest hand-off (§5.5).

### 11.4 Move to another host
1. Old host: pause automation; the task-managers (and any long-running agents) get the save prompt (§5.5), so their hand-offs are current and their work is pushed; wait for running sessions to finish or save.
2. Take a final full export marked "move": the old installation becomes read-only and stays paused (a fence against two installs driving the same account and repos).
3. New host: §11.3.
- A restore test (export → fresh install → equal row counts and checksums → a dry-run dispatcher pass decides the same) runs in CI and as part of gate 8a.

### 11.5 One-off migration of this installation (D-165, D-180)
Today AFClaude's sessions share a root directory and the default `~/.claude` with sessions and projects AFClaude doesn't manage. This installation is set up again **on this host, under a new data directory** (`AFCLAUDE_DATA`) **and a new working root**, with its own config dir (§10.4), through the normal setup with a restore (§10.2, §11.3). Both folders are chosen at that setup, with no defaults (D-187). That run is **the end-to-end test of the whole backup → export → restore path, covering both the data folder and the working folder**, and the validation that the export is complete:
- **One-off flow derived from the normal backup and export.** The migration runs the normal backup (§11.1) and export (§11.2) code, with one addition: a selection filter that picks only AFClaude's sessions and projects out of the shared root and shared config dir, never other sessions (from `driven_sessions`, `projects.path`, the task-manager sessions and the own-sessions list; the owner confirms the list). It carries nothing the export wouldn't carry.
- **Restored at setup.** The new install starts with `AFCLAUDE_DATA` set to the new, empty directory, takes "restore from a backup or export" with that bundle and chooses the new working root in the same setup, so setup, restore, the `claude/` part and the post-restore validation all run exactly as on a new host.
- **Path map.** Restore takes `old prefix → new root` and rewrites the encoded project-dir names and the cwd in the session metadata. This is a normal restore feature, not a migration-only one. Whether `--resume` accepts a moved transcript is verified in phase 5e; if not, those projects continue from their hand-offs.
- **Nothing by hand.** Whatever turns out missing or wrong on the new side is fixed by correcting or expanding the export (code + manifest); then export and restore run again from scratch, until the post-restore validation, the end-to-end session and one real task-manager continue pass.
- Cut-over: the old setup is paused and fenced, the new one unpaused; the old data directory and the old transcripts stay in place, untouched.

