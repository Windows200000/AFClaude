# Rule exceptions log (AFK session 2026-09-26)

**No CLAUDE.md or guard rule was broken on purpose tonight.** For transparency, these are the state-touching actions I took. All of them were inside this project's own scope:

| time (Berlin) | action | why | undo |
|---|---|---|---|
| 00:01, recurring | `claude -p [--no-session-persistence] /usage` | get fresh usage numbers (local slash command, no model call); it rewrites the `cachedUsageUtilization` cache in `~/.claude.json`, which the CLI does on its own anyway | none needed |
| 00:04 | launched bg probe session `d39ad479` (haiku, cwd work/AFClaude/probe) | test env inheritance for resumed sessions | stopped by me at 00:05; `claude rm d39ad479` to delete it from the list |
| 00:05 | launched bg probe session `c0b93373` (haiku) | same (first probe hung on a permission prompt) | stopped by me at 00:06; `claude rm c0b93373` |
| 00:06 | a bare `--resume` of probe c0b93373 | prove wake-in-place + read effective env | **denied by the auto-mode classifier [Create Unsafe Agents]**; not retried, no workaround |
| 00:11 | started `keepalive.py` in **DRY-RUN**, detached (pid 3056629), watching my own session | live detection/decision test; it never resumes anything | `touch work/AFClaude/STOP` |

Not touched: the `claude daemon` (pid 3051692, which carries `CLAUDE_GUARD_DISABLE=1`, see PROGRESS.md 00:04), the guard hooks, settings.json, and all other sessions.
| 26.09. 14:21 | added `CLAUDE_GUARD_DISABLE=1` to the env line of `ka_resume.sh` | user asked explicitly ("setup hook bypass, that's a core part of this project"); only sessions launched by AFClaude get it, and their continue message tells them to respect the hooks anyway | remove `CLAUDE_GUARD_DISABLE=1` from that printf line (commit ac73533) |
| 29.09. 20:09 | move Docker data-root /var/lib/docker → /mnt/BlockVolume/docker on ovm1 (daemon.json data-root, systemd drop-in RequiresMountsFor=/mnt/BlockVolume, SELinux fcontext equivalence + restorecon, docker restart = all containers down for a few min) | user asked explicitly ("Then move docker root"; the root disk was at 99%) | rm /etc/docker/daemon.json + /etc/systemd/system/docker.service.d/blockvolume.conf, daemon-reload, mv /var/lib/docker.old back (until deleted), restart docker |
| 01.10. 14:10 | cut-over to the manager container: host crontab's 7 AFClaude lines removed (backup data/host_crontab.bak), host watcher stopped (STOP), keepalive state + log copied to data/keepalive/, container recreated with AFCLAUDE_SCHEDULER=on (docker/.env), the pre/post-reset samples of 01.10. spooled in the container's at shim | owner's request "AFClaude should live in a docker container as the manager" | touch data/keepalive/STOP; AFCLAUDE_SCHEDULER=off in docker/.env + `docker compose -f docker/compose.yml up -d`; `crontab data/host_crontab.bak`; copy data/keepalive/keepalive_state.json back to the repo root; `./start_keepalive.sh f2897285-dd97-49d9-b29a-2334b4753dee --arm` |
