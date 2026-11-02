# Usage split S2: hand-off (branch usage-split-s2)

State: the build of docs/usage_split_plan.md §5 S2 is complete; the Opus review is done (clean) and its fixes F1-F3 are applied on this branch.

Done:
- `store.py`: additive table `typed_messages` (TYPED_SCHEMA, no version bump) and `add_typed_message`, `typed_messages`, `typed_since`.
- `actions.py`: action `typed.record` (audited, runner-only, hash only, never the text).
- `keepalive.fire(..., *, driver, prompt, actor="keepalive")`: `driver` is required (auto | owner); `record_typed()` writes the row before any take-over, stop or send, and a failed record raises, so nothing is sent (D-235).
- Callers: keepalive create_task_manager (auto, manager_first_start), _fire_start (auto, the stall's prompt), compact_session (auto, compact), _await_save (auto, session_end_save / _retry / _refresh), _compact_now (auto, compact_after / compact_after_run); dispatcher (auto, continue / continue_foreign, actor dispatcher); inbox_worker (owner, inbox, runner:inbox); usage_review (auto, its one-off bundle, runner:usage_review); tools/test_compact_live.sh (auto).
- `usage_split.classify()` (each prompt's verdict plus `missed` = a keep-alive-prefixed prompt with no typed row).
- `usage_sampler`: `track_driver` / `driver_at` / `_Typed`; scan reads main transcripts first; prompts from before the record's first row are matched by the prefix list (source "prefix"); Haiku's list leaves out AFClaude-typed prompts; an unclassifiable entry (SplitError) is reported after the sample row (alert + pause, §0); a missed send (keep-alive prefix, no typed row) is only a logged line and a count (`meta["missed_sends"]` per session) for S3 to decide on, no alert (F1: it fires falsely on owner-typed or pasted prefixes, queued prompts landing > 10 min late and the deploy changeover).
- Tests: new test_typed_messages.py; driver assertions added in test_compaction (its fake fire is strict now), test_inbox, test_dispatcher, test_pick, test_usage_review, test_usage_sampler; direct fire calls in test_manager_launch / test_remote_control / test_compaction pass driver.

Review fixes (applied):
- F1: a missed send is a log line plus `missed_sends`, never an alert/pause (see above).
- F2: the dispatcher's continue of a live rc thread (`rc_continue.send`, no fire) records an `auto` typed row (`ka.record_typed`, prompt continue_foreign) in the before-send hook; a failed record sends nothing. test_typed_messages checks the call sites of both.
- F3: launch_nightly.sh (legacy, not in cron, only referenced by the README) is marked unsupported in a comment; no functional change.
- Plan §3.1: one line on inbox texts carried by a window start counting as auto.

Deploy order: merge early in a run (not near a window start), rebuild the container drained right away, and allow no host-side `fire` path between the merge and the rebuild (the host code would send without the new record, or the old container would run new callers against a table it lacks). Then the plan's "Done when" live check: a stall continue and a window start show driver auto, an inbox message owner.

Left: the live check after deploy; S3 decides on missed_sends.
