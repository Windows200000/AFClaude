# Phase 5c: DB backup, restore, move and the WAL archive (implementation plan)

Plan for design §11.1, §11.3, §11.4, §10.2 / §13 step 5c: a backup bundle of the DB, restore from it, an install that starts paused after a restore, a fence for a move, the round-trip test, and a WAL archive by Litestream for rolling back to any moment within `db_wal_retention_days`. Each step in §5 is one worker branch; B1 is built on branch `phase5c`.

Binding inputs: D-161 (everything in the DB; a backup and a move give a clean, complete continue), D-175 (the WAL kept for a reasonable timeframe: point-in-time recovery), D-173 (a restored install starts paused), D-180 / D-187 (`AFCLAUDE_DATA` and the working root have no default and are chosen at setup, with an optional restore; this host's own move is the end-to-end test, 5e), D-165 / D-174 (the export and its credentials, 5d), D-154 (one write path with audit), D-171 (DB errors escalate), D-209 (mypy --strict for backup/restore), D-235 / D-239 / D-282 / D-283 (fail fast: alert + pause; the task-manager clears a pause after investigating), D-287 (scheduled backups: no passphrase encryption, write-once, an unoverwritable latest daily and 2 weeklies, configurable retention).

## 0. Already decided

- **Litestream, not custom code** (design §11.1). Pinned to v0.5.17 (31.08.2026, the latest release on 22.10.; linux-arm64 and amd64 tarballs with `checksums.txt`), checksum-verified in the Dockerfile like supercronic. A file replica in `$AFCLAUDE_DATA/wal-archive`; off-host targets later.
- **Snapshot by the SQLite online backup API, then `VACUUM` of the copy, not `VACUUM INTO`.** The host's Python links SQLite 3.26, which has no `VACUUM INTO` (3.27+; the image has 3.40). One `backup()` call with all pages is one read transaction, so the copy is consistent, and the live DB is opened read-only (`mode=ro`): `create` never writes to it. `store.migrate` already copies this way.
- **No CI service exists** (no workflow in the repo). The "CI round trip" is `test_backup.py` in the unit suite plus gate 8a; a hosted CI is out of scope here.
- **Installation identity in `meta`**, where `install_id` / `installed_at` already are (stamped by `init()` for a new DB since 2d): new keys `install_epoch` (absent = 0) and `restored_from`. Design §7.2's separate `installation` table is folded into `meta`; B8 corrects the text.
- **The live DB has no `install_id`** (it predates the stamp). `create` refuses a DB without one (fail fast); `backup.py stamp` (audited `install.stamp`) sets it once. Running it on the live DB is a deploy step of B1.
- **No passphrase encryption of scheduled backups** (D-287): the owner's reasoning is that on a compromised server, an unencrypted backup is the least of the concerns, and built-in (unencrypted) backup functionality is fine. This drops B2 ("encryption and the master key") and question 1 entirely; the bundle is a plain `tar.gz`. `cryptography` stays an option for anything that later does need it, but nothing in B1–B8 pins it now.
- **Scheduled backups are write-once, with an unoverwritable floor and a configurable retention** (D-287): the daily job's own write path may only create a new bundle and read existing ones, never modify or delete one; pruning to the retention setting is a separate, more-privileged action; the latest daily backup and the 2 most recent weekly backups are always kept and refused by both the write path and pruning, regardless of the retention setting. See §3.1a.
- **The move/export bundle (5d, D-174) is a separate question, not resolved here.** D-174 has the export include the Claude credentials by default; a move bundle is one-off and meant to leave this server for another host, unlike a scheduled backup that sits on the same (possibly compromised) disk it protects against mistakes on. Whether it keeps passphrase encryption is for 5d to decide; flagged, not decided by this plan.
- **Failures fail fast** (D-235): a failed backup job, Litestream down or lagging, a failed restore check: alert + pause (`failstop.fail`, component `backup` / `litestream`), never a partial bundle that looks complete (written as `.partial`, renamed only after its own verify). D-282's later pre-release step turns these into alert-only like every other site.
- **The master key does not exist yet** (5b, §10.2). The manifest records `master_key: null` until it does; when it exists it travels inside the bundle in whatever form it is normally stored at rest — its own protection is a 5b question, independent of D-287 (which is about the backup *file*, not secrets inside the DB it contains).

## 1. Today (as-is inventory)

| what | where | state |
|---|---|---|
| DB path | `store.DB_PATH` = `$AFCLAUDE_DB` or `<repo>/data/afclaude.db`; data files by `$AFCLAUDE_DATA_DIR` (5 modules) | defaults everywhere; compose mounts the host repo's `data/` |
| writers | container jobs (supercronic) and host processes (`tasks.py`, MCP server, SessionStart hook in host sessions) on the same file | WAL mode, SQLite's default auto-checkpoint (1000 pages) in every connection |
| copies | `store._backup_copy` (`<db>.<tag>-<utc>.bak`, backup API) before migrations; 13 hand-made `.bak` copies in `data/` (~400 MB) | no bundle, no manifest, no schedule, no retention |
| identity | `meta.install_id` / `installed_at` (new DBs only), `meta.schema_version` | live DB: no `install_id`; no epoch, no `restored_from` |
| restore | none; `store.connect()` never creates a DB implicitly (D-171/D-187); `health()` reports a DB that came back empty ("replaced") | — |
| schema guard | `SchemaTooNew` / `MigrationNeeded`, `store.py migrate` (backs up first) | built (2a) |
| pause | `automation_paused` + reason (`automation.set`), `project_pauses`, `failstop.py` | built |
| runner | `runner.py` built (3a S1), not started live; cron runs every job | a Litestream child needs the runner live |
| Litestream | not installed (host or image) | — |
| settings | `storage` section: `log_retention_*`, `runner_runs_retention_days` | no backup settings |

## 2. Measurements (22.10., read-only consistent copy of the live DB)

- Backup API copy: **0.16 s, 49.0 MB** (11,960 pages of 4 KB, 26 free); `integrity_check` ok in 0.23 s; gzip -6: **10.3 MB** in 1.0 s. 50 tables; `app_log` 79k rows (mostly the 13.10. import).
- B1 on a copy of that snapshot (after `stamp`): `create` 4.6 s (snapshot, VACUUM, gzip, its own verify) → a **9.6 MB** bundle; `verify` 0.7 s; `restore` 1.8 s.
- Write rate (last 7 days): ~250–750 `app_log`, ~100–480 `audit_log`, 96 samples and 96 Haiku judgements per day, i.e. on the order of 1,000 write transactions a day. At a few changed 4-KB pages each that is roughly 5–25 MB of WAL a day before Litestream's compression and compaction, so the 7-day archive should be the DB snapshot plus well under 200 MB, in line with design §11.1. B4 measures it on the live system.

## 3. Design details

### 3.1 Bundle (format 1)
- One plain file `afclaude-backup-<install_id[:8]>-<utc>.tar.gz` (D-287: no passphrase encryption of scheduled backups; B2 is dropped). Members, fixed names only: `manifest.json`, `db/afclaude.db`; later `wal-archive/…` (B5).
- `manifest.json`: `format` (`afclaude-backup`), `format_version`, `install_id`, `install_epoch`, `app_version` (`actions._code_version()`), `schema_version`, `created_at`, `kind` (`backup` | `move`, B6), `tier` (`daily` | `weekly` | `manual`, §3.1a), `row_counts` per table (all of `sqlite_master` except `sqlite_*` and Litestream's `_litestream_*`), `files` {name: sha256, size}, `master_key`, `wal_archive`.
- The DB inside is the vacuumed snapshot in rollback-journal mode (one file); `store.connect()` turns it back into WAL on first open.

### 3.1a Write-once backups and retention (D-287)
- The scheduled job's own write path (`backup.record`) may only insert a new `backups` row and its file, and read existing ones; it holds no update or delete grant, so a compromised or buggy job cannot erase history. Pruning to retention is a separate action, `backup.prune` (audited, `cli`/owner or a distinct runner grant), never called from the write path itself.
- Each completed bundle is tagged a `tier`: `daily` (every run) or `weekly` (the daily nearest a setting `backup_weekly_day`, default Sunday); a manual `backup.py create` is `manual`. The current latest `daily` bundle and the 2 most recent `weekly` bundles are marked `protected` in the `backups` table; both the write path (can't reuse or overwrite a protected file name) and `backup.prune` refuse to touch a `protected` row, regardless of the retention setting.
- Retention beyond the protected floor is the existing setting `backup_keep` (default 14; count-based, customizable, D-287's "retention should be customizable").

### 3.2 Restore
`backup.py restore BUNDLE --into DIR`: refuses a `DIR` that already holds a DB (a restore never overwrites); extracts into a temp dir inside `DIR`; verifies the member list, every checksum and size, `integrity_check` and the row counts; refuses a `schema_version` newer than the code; runs `store.migrate` when older; then one audited action `install.restored` (actor `cli`, later `owner`): `install_epoch` + 1, `restored_from` {install_id, epoch, created_at, app_version, bundle sha256}, and when the restored DB's `restore_starts_paused` (new setting, default on, D-173) is on, `automation_paused` with "restored from a backup of <time>: check and resume". Last, the file is moved into place. The working-root choice, the `claude/` part and the post-restore validation are 5d.

### 3.3 WAL archive (Litestream)
- The runner runs `litestream replicate -config <generated>` as a supervised child (design §5.1); the config comes from the settings (`db_wal_retention_days`, default 7, range 1–30; snapshot retention = window + 1 day, so the oldest restorable moment is never inside the window).
- Litestream must be the only checkpointer: `store.connect()` sets `PRAGMA wal_autocheckpoint = 0` in every connection (container and host processes), but only from the deploy that starts Litestream, never before (without it the WAL would grow without limit).
- Litestream adds `_litestream_seq` / `_litestream_lock` to the DB: excluded from row counts, schema tests and any table enumeration.
- Lag (newest LTX older than a setting, default 5 min, while the DB changed) or the child exiting: `failstop.fail("litestream", …)`. Past a WAL size limit (setting, default 256 MB) the runner checkpoints itself; the archive then has a gap that the status shows.

### 3.4 Rollback and the write fence
- `meta.write_fence` = {kind `maintenance` | `moved`, since, reason}. `store.transaction()` reads it after `BEGIN IMMEDIATE` (race-free) and raises `DBError` kind `fence`; `connect()` opens a fenced DB `query_only`. Reads go on.
- `backup.py rollback --at <time>`: pause, fence `maintenance`, `litestream restore -timestamp` into a new file, integrity check, keep the current file as `afclaude.db.before-<time>`, swap, `install.restored` (epoch + 1, starts paused). Long-lived processes (runner, inbox API, MCP server) hold the old inode: on a `fence` error they exit and are restarted (compose restart policy), so they open the new file.

### 3.5 Move fence
`backup.py create --move` (later F10's "move" export): pause, fence `moved` in the source DB in the same transaction that records the bundle, then the bundle. The old install stays read-only and paused; only `backup.py unfence` (owner, audited) lifts it, e.g. after an aborted move.

## 4. Order

- B1 has no dependency (built now). B3 (§3.1a's write-once/retention; no encryption step, D-287 drops the former B2) needs the runner live (3a cut-over). B4 needs B3 and the image change (5b's image, or the current one). B5 after B4. B6 has no dependency on the dropped encryption step. B7 with 5b's three-service compose. B8 last.
- `store.connect` / `store.transaction` are imported by every live process: B4's pragma and B5/B6's fence change them, so those steps are built in worktrees and deployed with a restart of every writer, never in parallel with another step that edits `store.py`.

## 5. Steps

**B1: bundle, verify, restore into an empty directory (built on `phase5c`).**
- **Build:** `backup.py` (mypy --strict): `create --out DIR [--db PATH]`, `verify BUNDLE`, `restore BUNDLE --into DIR`, `stamp [--db PATH]`; §3.1 without the archive (plain tar.gz, D-287: no encryption step); §3.2; actions `install.stamp`, `install.restored` (owner-only, audited); setting `restore_starts_paused`. Nothing schedules it.
- **Tests:** round trip on temp DBs (rows, row counts, settings, audit rows equal; epoch + 1; `restored_from`; paused with the reason, or not when the setting is off); create never writes the source (file hash unchanged, a fenced/read-only source still works); refused: DB without `install_id`, non-empty target, tampered member, extra or missing member, path traversal, bad manifest, newer schema; an older schema is migrated; a failure leaves no `.partial` and no target DB.
- **Done when:** suites, `test_typing`, `test_mcp_server`, `check_public` green. Deploy: imports only; then `backup.py stamp` once on the live DB, and one `create` + `verify` + `restore --into` a scratch dir by hand.
- **Risk:** the source opened `mode=ro` needs its `-shm` readable; the live `data/` is. The bundle is plain (D-287); it is written to a directory under the normal data permissions, not world-readable.

**B3: the daily backup job, write-once and retention.**
- **Build:** §3.1a; `backups` table (additive: made bundles with their manifests, size, verified_at, `tier`, `protected`); action `backup.record` (create-only grant, no update/delete); action `backup.prune` (a separate, more-privileged actor, audited deletes, refuses `protected` rows); runner job `backup` (daily, setting `backup_time`), on demand `backup.py create`; retention `backup_keep` (default 14, customizable, D-287); optional copy directory (`backup_copy_dir`, empty = none). Each new bundle is verified (checksums) before `backup.prune` removes anything past retention.
- **Tests:** the latest daily and the 2 most recent weeklies survive `backup.prune` under every `backup_keep` value, including 0; `backup.record`'s actor is refused an update/delete at the grant level, not just by convention; a failed verify pauses and keeps every older bundle; disk-full during write leaves no partial.
- **Done when:** a week of daily bundles (and at least one weekly) on the live install, each verified, with the protected floor intact after a manual prune.
- **Risk:** the runner is not live yet (3a); until then the job does not run.

**B4: Litestream.**
- **Build:** the pinned binary in the image; the generated config; the supervised child; `wal_autocheckpoint = 0` in `store.connect` together with it; lag and size checks (§3.3); `backup.py wal-status` (archive size, oldest restorable moment) for F10.
- **Tests:** in a temp dir with the real binary (skipped with a reason when it is absent): three writer processes, a restore to a timestamp between two writes gives exactly the earlier state; Litestream killed, the WAL grows, the size check checkpoints, Litestream re-snapshots, the gap is reported.
- **Done when:** 7 days on the live install; archive size and restore time of the full window measured and written into design §11.1.
- **Risk:** a host-side process opening the DB with an old checkout (before the pragma) checkpoints behind Litestream's back; Litestream detects it and re-snapshots (more disk, no loss). The deploy updates the host checkout and the container together.

**B5: archive in the bundle, restore to a moment, rollback.** §3.4; `restore --at`, `rollback --at`; the write fence (`maintenance`). **Tests:** rollback on temp DBs with Litestream; writes during the fence fail with kind `fence`; the long-lived processes exit on it. **Done when:** a rollback rehearsal on a copy of the live data dir. **Risk:** a writer that ignores the fence error; the test enumerates the long-lived processes.

**B6: move fence.** §3.5; `create --move`, `unfence`. **Tests:** the source refuses every write after `--move`, reads go on, `unfence` restores writes; the bundle is made after the fence (no write is lost between them). **Done when:** the move rehearsal of design §11.4 on temp volumes passes.

**B7: `AFCLAUDE_DATA` required.** `store.DB_PATH` and the five `AFCLAUDE_DATA_DIR` readers from `$AFCLAUDE_DATA` with no default (unset = a clear error); compose `${AFCLAUDE_DATA:?…}`; the entrypoint refuses an unmounted data dir; `afclaude setup` asks for it and offers "restore from a bundle" (B1's restore). Host-side CLIs get it from the setup's environment (MCP registration, hook command). **Risk:** a host CLI started without it fails at once (intended: D-180); built together with 5b's compose and 5d's working root.

**B8: docs.** Design §7.2 (`installation` in `meta`), §11.1 (backup API, measured sizes), §13 5c status; README file table; mypy list (`backup.py` from B1).

## 6. Questions for the owner

None open. D-287 answers the former question 1 (no passphrase encryption of scheduled backups) and adds the write-once/protected-floor/retention requirements in §0 and §3.1a. The move/export bundle's own encryption (5d, D-174) is a separate, still-open question flagged to 5d, not asked here.
