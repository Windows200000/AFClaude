# Usage split S2: hand-off (branch usage-split-s2)

State: the build of docs/usage_split_plan.md §5 S2 is complete and committed; the run was cut short by the usage budget before the final broad test sweep finished.

Done:
- `store.py`: additive table `typed_messages` (TYPED_SCHEMA, no version bump) and `add_typed_message`, `typed_messages`, `typed_since`.
- `actions.py`: action `typed.record` (audited, runner-only, hash only, never the text).
- `keepalive.fire(..., *, driver, prompt, actor="keepalive")`: `driver` is required (auto | owner); `record_typed()` writes the row before any take-over, stop or send, and a failed record raises, so nothing is sent (D-235).
- Callers: keepalive create_task_manager (auto, manager_first_start), _fire_start (auto, the stall's prompt), compact_session (auto, compact), _await_save (auto, session_end_save / _retry / _refresh), _compact_now (auto, compact_after / compact_after_run); dispatcher (auto, continue / continue_foreign, actor dispatcher); inbox_worker (owner, inbox, runner:inbox); usage_review (auto, its one-off bundle, runner:usage_review); tools/test_compact_live.sh (auto).
- `usage_split.classify()` (each prompt's verdict plus `missed` = a keep-alive-prefixed prompt with no typed row).
- `usage_sampler`: `track_driver` / `driver_at` / `_Typed`; scan reads main transcripts first; prompts from before the record's first row are matched by the prefix list (source "prefix"); Haiku's list leaves out AFClaude-typed prompts; a missed send or an unclassifiable entry is reported after the sample row (alert + pause).
- Tests: new test_typed_messages.py; driver assertions added in test_compaction (its fake fire is strict now), test_inbox, test_dispatcher, test_pick, test_usage_review, test_usage_sampler; direct fire calls in test_manager_launch / test_remote_control / test_compaction pass driver.

Test status: green on the 3.12 venv: test_typed_messages, test_usage_split, test_usage_sampler, test_actions, test_prompts, test_pick, test_typing (mypy --strict), test_mcp_server, and the caller suites (keepalive, dispatcher, inbox, inbox_active, compaction, manager_launch, remote_control, usage_review, run_now, fillup). The same DB suites are green under host python3 3.9 (730 tests). A broader sweep over every test module importing store/keepalive/sampler/dispatcher was still running at the cut.

Left:
- Re-run that broad sweep and `tools/check_public.py` once more before merging.
- Plan "Done when": live check after deploy (a stall continue and a window start show driver auto, an inbox message owner).
