# Goal 12: usage split autonomous vs manual (design plan)

Plan for goal 12: "Usage split autonomous vs manual: tag AFClaude-driven periods in user sessions; estimate other-device usage; a separate session→weekly ratio per side from single-side intervals". Design only; each step in §5 is one worker branch.

Binding inputs: D-137 (monitor how session and weekly limits rise, per user; separate AFClaude's and the owner's usage as well as possible; show it for window planning), D-138 (gaps 1, 3 and 4 of the 30.09. list; "4 should work in periods when there is only manual or only auto usage, which should be most the time in the future"), D-140 (the ratio per 5-h session window, with its spread and the rounding error marked), D-266 / D-221 / D-264 (the owner's chats with a task-manager are AFClaude usage but tracked as their own category, "design chats"; a flat weekly design-chat forecast), D-130 / D-131 / D-132 (the sampler's data; Haiku sees the owner's last 4 messages with their tokens and model), D-012 / D-019 / D-022 (the forecast stays gradual, improved from collected data), D-142 (accuracy computed in code; the usage review improves the prediction model, architecture changes are proposals), D-267 (predictor variables for the next review), D-018 (the owner's input to AFClaude is not "owner activity" for the yield), D-206 (approved stalled sessions continue at their reset, any time), D-161 (the DB is the only copy), D-154 (writes through `actions.py`), D-235 (fail fast), D-209 (mypy --strict for new modules), D-263 (questions in plain words).

The gap list D-138 refers to (task-manager, 30.09.): (1) the owner's sessions that AFClaude continues (approved stalls) count as the owner's, although the usage is autonomous: AFClaude should tag the periods it drives; (2) subagents count with their parent session (correct, not on the roadmap); (3) other devices (claude.ai web, desktop, mobile, Claude Code elsewhere) don't appear in local transcripts, so every unexplained rise is "the owner"; (4) the session→weekly ratio is measured for the account only, not per side.

## 0. Already decided

- **Two axes, not one.** "Who drove it" (D-138: auto vs manual) and "whose usage it is" (D-266: AFClaude vs the owner) differ for exactly two kinds of usage. Five categories cover both:

  | category | what | driver side (D-138) | owner of the usage (D-266) |
  |---|---|---|---|
  | `run` | AFClaude-driven turns in AFClaude's sessions (task-manager runs, fill-ups, compactions, reviews, nightly tasks, their subagents) plus AFClaude's calls without a transcript (sampler Haiku, window-start ping, the usage refresh) | auto | AFClaude |
  | `continued` | AFClaude-driven turns in the owner's own sessions (approved stalls, D-206; later the dashboard's `continue_now`) | auto | owner |
  | `design` | owner-driven turns in AFClaude's sessions (typed in, via Remote Control or the dashboard inbox) | manual | AFClaude |
  | `user_local` | owner-driven turns in the owner's sessions on this host | manual | owner |
  | `other_devices` | the rise no local transcript explains (claude.ai, desktop and mobile apps, Claude Code on other machines) | manual | owner |

  The gate's forecast covers everything except `run` (as today: other use + design chats, `pacing.forecast_gate`); "the owner's other use" (`forecast_user`) = `continued` + `user_local` + `other_devices`; the design chats stay their own forecast (D-264). `continued` is the owner's work (D-206) and is counted as the owner's demand; it shows as autonomous only on the driver axis. This is a task-manager reading of D-206 + gap 1, not a new owner rule.
- **Attribution per turn, not per time span.** A turn belongs to whoever sent the prompt that started it: AFClaude (a message it typed or launched with) or the owner (anything else that is a real prompt). This replaces the time rule of goal 0c (rise during a run = AFClaude), which counts the owner's chats during a run as the run and AFClaude's continued stalls as the owner.
- **Costs from the existing cost model.** Each request's cost in session-% comes from `compact_timing`'s cost model (API-price features per token kind, D-237/D-238), not from the two different output-token weightings used today (`limit_ratio.weighted_tokens` with cache weights, `usage_sampler.weighted_tokens` with model weights). Output-only weighting under-counts long-context chats, whose cost is mostly cache reads and re-reads, i.e. exactly the design chats.
- **The per-window record stays the unit** (D-140). A per-side ratio is measured on single-side segments: a window split where the driving side changes, each segment measured first-to-last reading like a whole window. This extends D-140 (finer pieces of the same records); it does not replace the per-window pooled ratio.
- **No owner number is hand-tuned** (D-142): thresholds below are code constants with tests; the review may propose other values.
- **New logic in a new pure module** `usage_split.py` (mypy --strict, D-209); `limit_ratio.py`, `pacing.py`, `stage_eta.py` and the sampler call it.
- **Failures fail fast** (D-235): a request whose model has no price, a transcript that can't be read, a typed message that can't be recorded: an error with alert and pause, the computation for that sample aborted (raw rows kept, so it is recomputed after the fix). The split never reports a partial week as complete; missing coverage is shown as coverage.

## 1. Today (as-is inventory)

| what | where | state |
|---|---|---|
| meters | `usage_sampler.py` every 15 min (`/usage`, integer session % and weekly %, reset times); the watcher's extra readings during runs (`usage_run_readings`) | account-wide only |
| local activity | sampler `scan()`: every transcript (subagents included) since the last offset; per sample only two buckets `activity.own` / `activity.other` (tokens by model, turns, prompts) and `own_w_tokens` / `other_w_tokens` | "own" = AFClaude session by cwd, name or `driven_sessions` kind `own`; per-session detail only in the sampler state |
| time split (quickview "AFClaude vs user") | `limit_ratio.estimate_time_split`: rise during runs (`usage_runs` rows, else fire spans) = AFClaude, all else = user; inside a run, the other-session token share moves to the user; `user_unseen_pct` = rise with no local tokens | still the D-018 rule: design chats count as the **user**, which D-221/D-266 changed |
| design-chat split (forecast, ETA) | `pacing._intervals`: outside runs, rise × own/(own+other) model-weighted output tokens; `stage_eta.usage_split` (`--split`) a second implementation of the same | design chats = AFClaude (D-266); continued stalls and other devices = user |
| token split (secondary) | `limit_ratio.estimate_side_rates` / `estimate_weekly_share`: %/token rate per side from clean 15-min pairs | by session, not by driver; other devices invisible |
| ratio | `limit_ratio.estimate_window_ratio` per window (D-140), pooled Σ Δweekly / Σ Δsession with spread, SE and rounding; `preferred_ratio` → `pacing.ratio_info` | one account-wide ratio; window records carry `own_share` / `other_share` (output tokens) |
| ratio consumers | gate run cost and auto threshold, last stretch (`pacing`), fill-up weekly cost (`fillup`), stage ETA capacity (`stage_eta`), quickview windows per week; `compact_timing` has its own `weekly_per_session` | all use the one ratio |
| cost model | `compact_timing.fit`: OLS of 15-min session rises on all local requests' API dollars by kind, **with a constant** (absorbs rounding and unlogged use) | used for compaction timing only |
| who typed a prompt | transcripts mark AFClaude's tmux-typed messages with origin kind `human`, like the owner's own; `compact_timing.KEEPALIVE_PREFIXES` tells some apart by text | prompts are editable (D-227), so prefixes are not reliable; no record of typed messages |
| yield signal | `pacing.user_signal`: any turn in a non-AFClaude session, or a weekly rise without AFClaude turns | a continued stall counts as the owner being active |
| Haiku (D-131) | last 4 prompts of each non-own session | AFClaude's continue message counts as one of "the owner's" prompts |

Gaps found on the way (fixed by S1, S2 and S6): the quickview split contradicts D-266; a one-off limit reset (the weekly meter drops to near 0 while its reset time stays the same) is not handled: `estimate_time_split` keeps the old maximum and counts nothing after the drop, a window with the drop is excluded as "inconsistent", and the cycle's final weekly % is wrong for the forecast's weekly budget; the two token weightings disagree.

## 2. Existing tools checked (D-007)

- **`/usage` breakdown**: says itself that it is "based on local sessions on this machine; does not include other devices or claude.ai". The sampler already parses it. No per-device or per-surface numbers.
- **ccusage-style tools**: read the same local JSONL transcripts the sampler reads; nothing about other devices or the limit meters.
- **Claude Code OpenTelemetry** (`claude_code.token.usage`, `claude_code.cost.usage`, `api_request` events with `session.id`, account and terminal attributes): per request, but only from the Claude Code instances it is turned on in; for this host it repeats the transcripts, and it doesn't cover the claude.ai chat apps. It could make Claude Code on the owner's other machines visible later (§6).
- **Team plan admin analytics**: per-user spend export, not per device, and the account has one user. The Enterprise and Claude Code Analytics APIs are for other plan types or API organisations.
- **Status line input** (`rate_limits.five_hour` / `seven_day` `used_percentage`): the same account meters at every reply of a session that has a status line. Might give finer readings than `/usage` (optional S7, needs the owner, question 1).

Conclusion: nothing attributes the account meters to devices or to who drove a turn. Gaps 1 and 4 can be solved exactly from local data; gap 3 only as a residual with an error band.

## 3. Design details

### 3.1 Gap 1: who drove a turn

- **Record every typed message.** `keepalive.fire` is the one path for every message AFClaude types or launches with (window starts, fill-ups, last stretch, compaction and save prompts, inbox delivery, the dispatcher's stall continues, reviews, `--work-on`). It gets a required `driver` argument (`auto` | `owner`, no default: a caller that doesn't say is a test failure) and records each send in a new additive table `typed_messages` (session id, sent at, driver, launch category, prompt name, sha256 of the whitespace-normalised text) through an audited action `typed.record` (actor = the component). Inbox deliveries are `owner` (the owner wrote them); everything else is `auto`. A failed record aborts the send (D-235: no untracked message).
- **Driver of a session over time.** The sampler's scan matches each non-meta, non-tool-result user entry of a main transcript against `typed_messages` (same session, hash equal, sent within 10 min before the entry): matched → that row's driver; unmatched real prompt → `owner`. Task notifications, peer messages (`origin.kind` `peer`), compaction summaries and local commands don't change the driver. The session's driver holds until the next prompt that sets one; it is kept per session in the sampler state with its change times (last 24 h), so subagent requests (separate transcripts, parent = their session) take the driver in force at their own timestamp.
- **Session class** (AFClaude's or the owner's): `driven_sessions` (task-manager, review, request, own) plus today's cwd and name markers; a `stall` row marks an owner session that AFClaude drives. Category = class × driver (§0 table).
- **History before S2** (no `typed_messages`): the same classification with the prompt-prefix list as the matcher (`[keep-alive]`, `[AFClaude` except `[AFClaude inbox` = owner, the compaction texts), labelled `source: prefix` in every derived number, never mixed silently with `source: typed`.
- **Haiku (D-131)**: the "last 4 prompts" of a session are the owner's only; AFClaude-typed messages are left out (they aren't the owner's messages).

### 3.2 Per-request cost and the sample row

- Each request gets its modeled cost in session-% (`compact_timing.per_token` of the stored cost model, by model and token kind, the 5-min / 1-h cache writes apart). A model without a price fails the split for that sample (D-235); the price table gets the model, the sample is recomputed.
- AFClaude's calls without a transcript (sampler Haiku calls, the window-start ping, the usage token refresh) are costed from their own logged usage and go to `run`. A call kind that logs no usage gets that logging in S3; until then it is listed as "unlogged auto calls" in the residual's note.
- The sample row gains `split` (additive JSON fields, `v: 1`): modeled session-% per category for the interval, request counts per category, the cost model's id, the driver source (`typed` | `prefix`). Window records (`usage_session_windows`, record version 2) gain the per-category sums, `residual_session` and its error (§3.3), and the segments of §3.4. `--backfill-windows` recomputes them from the samples; a backfill of the `split` field from transcripts is possible only as far back as Claude Code keeps transcripts.

### 3.3 Gap 3: other devices as the residual

- **Per window**: `residual_session = Δsession (observed) − Σ modeled session-% of all local categories`. Positive beyond its error band = use this host can't see; it is attributed to `other_devices`. In weekly %: `other_devices_week = Δweekly (observed) − Σ_c modeled_c × ratio_side(c)`, summed over the cycle (the cumulative weekly meter makes the cycle total exact to one reading).
- **A cost model fit for this purpose** (S4): the current fit has a constant that absorbs exactly the unlogged use this step wants to see, and it is fitted on intervals that include other-device use. The split fit uses only `run`-only intervals at night (no owner prompt on this host in the hour before, no `continued`), through the origin; it stores the weights, the residual sd per interval and the coefficients' standard errors next to the compaction model (`cost_model.split`), so compaction timing does not change until a review proposes it. Check: out-of-sample `run`-only intervals have a mean residual within ±1 sd/√n of 0; otherwise the fit is reported as biased and the residual is not shown as a number.
- **Error shown with it**: rounding (two readings per window, ±0.5 each), the per-interval model scatter (resid sd × √intervals) and the systematic part (the weights' relative SE × the local modeled cost, which dominates when much local work runs alongside). Displayed as "other devices ≈ X% of the week (± Y)"; "not distinguishable from 0" when |X| < 2Y; never clipped silently (a negative value is shown as the model over-estimating local cost).
- **Certain part kept**: `user_unseen_pct` (rise in intervals with no local tokens at all) stays as the lower bound next to the estimate.

### 3.4 Gap 4: one ratio per side from single-side segments

- **Segments.** Within a window, consecutive sample intervals are labelled by side: `auto` if the manual share (modeled `design` + `user_local` cost plus a residual above its band) is ≤ 5% of the interval's Δsession, `manual` if the modeled `run` + `continued` cost is ≤ 5% of it, else `mixed`; intervals with no activity join the neighbouring segment. A segment is a maximal run of one label; it is measured from its first to its last reading (two readings' rounding, like a window; the exact-0 baseline when the segment starts the window from an idle sample). Usable as a window is (`MIN_D_SESSION` 5 points, no saturation, one weekly cycle, no one-off reset inside).
- **Estimate per side**: pooled `R_side = Σ Δweekly / Σ Δsession` over its usable segments, with the same statistics as `estimate_window_ratio` (spread, SE, rounding sd and worst case), plus `n_segments`, `Σ Δsession` and the share of `other_devices` inside manual segments.
- **How many are needed.** One segment's ratio error from rounding alone is about √((0.41/Δw)² + (0.41/Δs)²) (0.41 = the sd of a difference of two readings rounded to integers): at a ratio near 0.15, a 20-point segment is ±14%, a 5-point segment ±55%, a 60-point segment ±5%. Pooled over a side it falls with the side's Σ Δsession, and the window-to-window spread adds its own √n term. Rule in code, not a fixed count: status `ok` when n ≥ 5 segments and the side's relative SE (rounding and spread combined) ≤ 10%; `low_n` (shown with its interval, not used) below that; `insufficient_data` under 3 segments. As a guide, about 100–150 session points spread over at least 5 segments reach ±10%, so an AFClaude side builds up in a few nights, the manual side as fast as single-side daytime use occurs (D-138 expects it to be most of the time).
- **Difference test.** The snapshot reports `R_auto − R_manual` with its interval. Consumers use a side's own ratio when its status is `ok`, else the pooled ratio, and say which (as `preferred_ratio` does today, flagged).
- **Diagnostics per segment** for the review (D-142): the model mix (share of cost per model), the local hour and weekday, the share of cache reads. A side difference can be a proxy for these (for example a weekly limit that weighs models differently from the session limit, or a session limit whose size changes with the time of day); the review tests that and may propose a ratio by model or hour instead of by side.

### 3.5 One-off limit resets

A weekly meter that drops by ≥ 2 points while its rounded reset time stays the same is a reset event: the cycle is split there (same handling as a weekly reset inside a window: segments on both sides, the interval across the drop dropped), the time and design splits restart their running maximum, and a closed cycle's "final %" for the weekly budget is the sum of its segments' rises. The same for the session meter. Recorded in the weekly cycle records (`reset_events`).

### 3.6 Where the numbers go

| consumer | today | after |
|---|---|---|
| night gate run cost, auto threshold, last-stretch sessions left (`pacing.ratio_info`) | pooled ratio | `R_auto` when `ok`, else pooled (source text says which) |
| fill-up weekly cost (`fillup`), stage ETA session cost and design-chat conversion (`stage_eta`) | pooled ratio via `ratio_info` | `R_auto` (same call) |
| forecast inputs (`pacing._intervals`, `fit_profile`, `forecast_user`, `forecast_design`) | time spans + output-token share outside runs | per-category weekly attribution: other use = `continued` + `user_local` + `other_devices`; design = `design` (also owner turns during a run, today counted as the run); `run` left out |
| yield (`pacing.user_signal`, `minutes_since_user`) | any non-AFClaude-session turn or a rise without AFClaude turns | owner-driven turns (`user_local`; design turns stay excluded, D-018), a rise without local turns, and a sample interval whose residual is above its band (one interval's band is wide, so only a clear rise counts); `continued` turns still hold a start (as today, now on purpose: the owner's approved work comes first, D-001/D-206), with their own reason text |
| review split (`stage_eta --split`) | its own token split | the same per-category numbers as pacing (one implementation) |
| quickview / dashboard F4 and `status_snapshot` | AFClaude vs user share (time rule), one ratio | the five categories in weekly % (± error for `other_devices`), the two axes' totals, `R_auto` and `R_manual` with n and interval, windows per week at `R_auto` for AFClaude and at `R_manual` for the owner's own planning (D-137) |
| usage review (D-267) | categories run / design / other | the five categories per day and hour as predictor inputs (e.g. the owner's last-3-days use = `user_local` + `other_devices`) |
| `compact_timing` `weekly_per_session` | its own fit | unchanged; the review may propose `R_auto` |

## 4. Order

- S1 is independent and fixes live numbers now. S2 before S3 (the live matcher needs `typed_messages`; history uses the prefix matcher). S4 needs S3's per-request costs. S5 needs S3 and S4 (the residual band decides `auto` segments). S6 last, once S5 has run on the live data for at least a week and `R_auto` is `ok`. S7 is optional and waits for question 1. S8 with S6.
- `keepalive.py`, `usage_sampler.py`, `pacing.py` and `limit_ratio.py` are imported by the live watcher, sampler and dispatcher: S1, S2, S3 and S6 are built in worktrees and deployed with the usual drain; never two of them in parallel on the same file.

## 5. Steps

**S1: one-off resets and the D-266 quickview split.**
- **Build:** §3.5 in `limit_ratio` (time split, windows), `pacing` (cycle totals) and the weekly cycle records; the quickview's "AFClaude vs user" switches to pacing's three-way split (run / design / other) until S6 replaces it.
- **Tests:** a synthetic week with a one-off reset: the rises after the drop are counted, the window across it is split not excluded, the budget's closed-cycle total is the sum; design chats show as AFClaude on the quickview.
- **Done when:** suites, `test_typing` green; the live quickview shows the rise after the last one-off reset. **Changes:** the quickview's split numbers; the forecast's weekly-budget input for cycles with a one-off reset.

**S2: typed-message record and the driver (gap 1).**
- **Build:** table `typed_messages` (additive), action `typed.record`; `keepalive.fire(…, driver=)` required, every caller passes it (inbox `owner`, the rest `auto`); `usage_split.driver_of()` (pure: entries + typed rows → driver changes) and the prefix matcher for history; the sampler keeps per-session driver changes; Haiku's prompt list skips AFClaude-typed messages.
- **Tests:** each `fire` caller records the right driver; a send whose record fails is not sent; hash match tolerant of whitespace, refuses a match from another session or older than 10 min; peer, task-notification and compaction entries don't change the driver; a subagent request takes the parent's driver at its time; the prefix matcher on a recorded transcript fixture.
- **Done when:** a live stall continue and a window start show up with driver `auto`, an owner message with `owner`. **Changes:** one DB row per typed message; nothing decides on it yet.

**S3: per-request costs and the split in each sample.**
- **Build:** `usage_split.cost_of()` (per request, cost model); the sampler's scan accumulates per-category modeled session-% (§3.2); the `split` field; window records v2; usage logging for AFClaude's transcript-less calls where missing; `limit_ratio.py --backfill-windows` fills v2 fields.
- **Tests:** synthetic transcripts with all five classes × drivers give the right sums; an unpriced model raises and leaves the offsets unchanged; v1 records still read.
- **Done when:** a week of live samples with `split`; the per-category weekly sums are within the cost model's error of the observed weekly rise in weeks with no other-device use visible. **Changes:** additive sample and window fields only.

**S4: the split fit and other devices (gap 3).**
- **Build:** `compact_timing.fit --scope split` (run-only night intervals, no constant, coefficient SEs, residual sd) stored as `cost_model.split`; `usage_split.residual()` per window and per cycle with the §3.3 error; the bias check.
- **Tests:** synthetic data with a known hidden "device" stream: recovered within its band; with no hidden stream: indistinguishable from 0; a biased fit is reported as such and gives no number.
- **Done when:** on the live data the out-of-sample check passes, and the estimate for weeks with known claude.ai use is visibly above 0 while quiet weeks are within their band. **Changes:** new numbers only.

**S5: per-side ratios (gap 4).**
- **Build:** `usage_split.segments()` and `side_ratios()` (§3.4) in the `limit_ratio` snapshot (`ratio_sides`), with the diagnostics.
- **Tests:** synthetic windows with known per-side ratios and rounding: each side recovered within its interval; mixed intervals never enter a side; status thresholds; a one-off reset inside a segment splits it.
- **Done when:** a week on live data; `R_auto` `ok`; the snapshot shows both sides and the difference with its interval. **Changes:** new numbers only.

**S6: the consumers.**
- **Build:** §3.6: `ratio_info` → `R_auto`; `pacing._intervals` from the categories; the yield rule; `stage_eta --split` on the same numbers; the quickview and `status_snapshot` blocks.
- **Tests:** existing pacing / stage-ETA / fill-up suites with the new inputs; `forecast_backtest()` on the same decision points before and after: the change is kept only if bias and sd are not worse (the numbers go into the step's report); yield: a continued stall holds a start with its own reason, a design chat doesn't, an other-device residual does.
- **Done when:** one week live; the gate's reasons name the ratio's side and source. **Changes:** live gate decisions (run cost, threshold, forecast inputs), the yield reasons, the dashboard numbers.

**S7 (optional, after question 1): status-line meter readings.**
- **Probe first:** in one test session, log the status line's `rate_limits` input for a day: whether it has decimals and how often it updates. Only if it is finer than `/usage`: a small status-line script for AFClaude's sessions that appends the readings (session, time, both meters) to the DB, used by S3–S5 as extra readings between samples.

**S8: docs.** README rows (`usage_split.py`, sampler, limit_ratio, pacing "Design chats" and "User activity"), the Budget rule, dashboard design F4, mypy list.

## 6. Later (not in this plan)

- Claude Code on the owner's other machines could report its own local costs (OpenTelemetry, or the MCP machine users of design §9.7 running a small sampler), turning part of `other_devices` into measured `user_local`. The claude.ai apps stay a residual.
- Per-user ratios across installs (D-137's experience store) once there is more than one account.

## 7. Questions for the owner

1. **A status line in AFClaude's own sessions.** Claude Code can show the limit meters in a status line and hand them to a small script after every reply. That could give AFClaude more and possibly finer readings than its 15-minute checks, which would make the split between your use and AFClaude's more precise. It would be set in the AFClaude folder's settings, so you would also see the status line when you chat with the task-manager there; your other sessions stay unchanged. AFClaude would first test for a day whether the readings are finer, and only keep it if they are. Recommendation: yes, test it.
