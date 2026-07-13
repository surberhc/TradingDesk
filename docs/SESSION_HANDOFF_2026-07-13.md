# Session Handoff — 2026-07-10 → 2026-07-13 — S8 cleanup, InvesTech shelved, catalog-rebuild bug fixed

Note on dates: this handoff covers one continuous span of work from roughly 2026-07-10 into
2026-07-13 (the sandbox's real clock crossed that range within a single conversation). It
supersedes/extends `docs/SESSION_HANDOFF_2026-07-10.md` rather than duplicating it — read that
doc for the detailed blow-by-blow of the S8 mechanical-simulator failure; this doc picks up
from its open items.

## ⚠️ TOP-LINE OPEN ITEM — not buried

**Two InvesTech Windows scheduled tasks are still live/enabled** (`InvesTech Phase1 Feed`
22:30, `InvesTech Phase2 Breadth` 23:00) despite the project being fully shelved 2026-07-10.
Andrew was asked directly whether to disable them and never answered. Check current state
before assuming either way — don't let these keep running unnoticed just because the project
itself is closed.

## What's solid and can be trusted

1. **S8/British IC item cleanup — five conductor items closed.**
   - **#11** (HY-OAS spike history pull) — declined by Andrew, not pursuing.
   - **#13** (intraday/path-dependent exit-rule test) — declined by Andrew, not pursuing.
   - **#15** (unresolved data artifacts — the $1,222 balance mismatch, unmatched TAT rows) —
     declined by Andrew, not pursuing.
   - **#16** (fill-cost validation + more-crash-data prerequisite) — resolved-and-closed:
     `british_ic/LONGLEG_SLIPPAGE_ISOLATION.md` (commit `563f4cd`) already measures long-leg
     exit slippage against real 1-min SPXW bid/ask quotes (median $0.05/contract, 2.0x
     half-spread) — the gap was stale language in `docs/S8_SPEC.md` claiming this was "not yet
     modeled," corrected in 3 spots. The "more crash-event data" half stays open but is already
     tracked under #12, not a separate item.
   - **#19** (does TAT's automated system control the long leg's exit?) — resolved per Andrew
     directly: no, it does not. B2 is TradingDesk's own designed correction, matching how
     `docs/S8_SPEC.md` §3 already characterized it ("the one deliberate design addition"), not
     a pre-existing TAT rule being discovered.
2. **S8 mechanical-simulator code discarded, not just stashed** (commit `8372db0`) — per
   Andrew's explicit call to abort (see `docs/SESSION_HANDOFF_2026-07-10.md` for the failure
   diagnosis). `git rm`'d: `s8_mechanical_simulator.py`, `SIMULATOR_BUILD_PROGRESS.md`,
   `SIMULATOR_STAGE_B_PROGRESS.md`, `s8_sim_calibration.py`, `s8_sim_calibration_compare.py`,
   `s8_sim_extended_run.py`, plus the schedule-derivation helpers (`derive_grids_from_buckets.py`,
   `rebuild_entry_schedule.py`). The 3 diagnostic scripts that document how the root cause was
   found (`s8_replication_test.py`, `s8_short_leg_only_diag.py`, `s8_real_slippage_check.py`,
   commit `13ab44a`) were deliberately kept for the compliance record, even though they now
   import from a deleted module and can't be re-run standalone — their output is already
   captured on disk. Closes conductor **#23**.
3. **#20 (which single template a bot should run) reviewed, left open, explicitly re-scoped.**
   `british_ic/S8_SINGLE_TEMPLATE_COMPARISON.md`'s own verdict already says "no clear winner,
   more data needed" — re-reading it confirmed there's no new test design to invent here. The
   honest next step is re-running the same comparison once **#12** (Andrew's in-progress Flex
   Query pull back to 2024-09-16) lands. #20 is now explicitly gated on #12, not floating as an
   independent open question.
4. **InvesTech project fully explained, then shelved at Andrew's explicit call** ("We don't
   need it"). Before shelving: ported its ThetaData client from the deprecated v2 REST API to
   v3 (commit `3441701`) after discovering the running Terminal only serves v3; ran a real
   50-ticker timing benchmark (3.12 sec/ticker, extrapolating to ~52 min for an S&P-500-scope
   2-year backfill or ~45 hours for ThetaData's full ~26,225-ticker unfiltered universe); also
   surfaced (did not fix) a real bug where the project's default 420-day lookback exceeds v3's
   new 365-day-per-request cap, silently falling back to Tiingo. Everything documented in
   `investech/PROJECT_STATUS.md`, marked SHELVED not ON HOLD. Closed conductor **#9** and
   **#17**. See memory file `investech-project-shelved.md`.
5. **July 9th 2026 EOD options backfill closed, plus a real production bug found and fixed.**
   The missing day's snapshot was collected early and verified (all 50 roots, on disk), but
   closing out the backfill script's final step — `datacollector/storage.py`'s
   `rebuild_catalog()` — surfaced a real, pre-existing, previously-undiagnosed production bug.
   Traced back to at least a 2026-06-27 commit that had already noted this same function
   "hard-crashing the interpreter" without ever being fixed.

   **Three consecutive background-agent attempts at fixing it failed the same way**: each
   started a long validation/rebuild process and then ended its turn without actually waiting
   for it to finish, requiring the supervising session to discover and kill orphaned processes
   itself each time (via `tasklist`/CPU-time deltas). This cost real elapsed time and visibly
   frustrated Andrew ("you don't know how to run a test... fix the damn problems now"). The
   **4th attempt succeeded** once explicitly instructed up front to run fully synchronously —
   no `run_in_background`, Bash's own blocking `timeout` parameter for anything slow, never end
   a turn treating a step as "still running elsewhere."

   That attempt found and fixed **4 distinct bugs in sequence**, each only surfacing after the
   last was fixed:
   1. Monolithic literal `CREATE VIEW ... read_parquet(...)` embedding all ~312k file paths as
      one SQL array — confirmed superlinear scaling via 2k/20k/100k-file benchmarks, killed
      after 52 min with no completion against the real 311,988-file warehouse.
   2. `con.executemany()` bulk-writing ~312k manifest rows one at a time — killed after 46+ min.
   3. A resume-robustness gap: a kill between the manifest commit and the per-chunk view-build
      loop could leave chunk views un-built with no way to detect it later.
   4. `UNION ALL BY NAME` across chunk views re-binding each chunk's full
      `read_parquet(union_by_name=true)` definition on every reference, compounding to ~O(n^2)
      in chunk count (measured 4s/10s/34s/140s at 2/4/8/16 chunks) — fixed by forcing one
      consistent schema up front so the union becomes plain positional `UNION ALL` (0.29s @ 16
      chunks vs 140s before).

   **Result: full catalog rebuild against the real 311,988-file warehouse went from "never
   completes" to 16.9 seconds.** Independently verified — queried the rebuilt catalog directly
   and confirmed 187,298 rows for 2026-07-09 across exactly 50 roots; ran the datacollector
   test suite (39 passed, 2 pre-existing unrelated argv-script collection errors, confirmed
   unrelated to this change); confirmed both commits exist and no orphaned processes remained.
   Commits `40b4a30` (storage.py fix + test_storage.py), `ada471c` (backfill_20260709.py,
   one-off script committed for the record). Closes conductor **#22**.

## Process lesson (worth a top-line callout on its own)

When delegating any task with a genuinely long-running validation/proof step to a background
worker, the prompt must **explicitly forbid `run_in_background` and require blocking calls with
hard timeouts up front** — don't rely on the worker to choose that discipline on its own. This
was learned the hard way across 3 failed attempts before the 4th succeeded, and Andrew's
patience was visibly gone by the third repeat. Full detail logged in the
`background-agent-coordination-pitfalls` memory file (2026-07-13 addendum).

## Untouched, still open (not part of this session)

Conductor items **#1** (liquidate DU8922144/146 to cash), **#3** (review S0 pilot
WOULD-HAVE-TRANSMITTED logs), **#6** (defensive.py fillna(0.0)-as-worst-percentile design
question), **#7** (pick an account for S4 paper-deploy), **#8** (S5 financing-structure sizing
decision), **#10** (schedule + validate forward-collector depth widening), **#12** (British IC
S8 full-history pull back to 2024-09-16), **#20** (which S8 template — see above, gated on
#12), and **#24** (blocked on IBKR account approval for the live-data Gateway first login) are
all untouched this session and remain exactly as previously tracked.

Also untouched: **pending decision #6** (premium-selling family) — whether to treat the SPX
short-strangle refutation as a comprehensive close of mechanical premium-selling (condor + CSP
+ strangle), per `PREREG_short_strangle_alpha_2026-07-06.md`'s own pre-committed logic, or run
a cheap EOD-only sanity check on a diversified single-name strangle basket first. Recommended
the cheap test previously; still awaiting Andrew's direction.

## Tracking

Closed this session: conductor **#9, #11, #13, #15, #16, #17, #19, #22, #23**. See
`conductor/STATUS.md` for the live open-item list and `conductor/cli.py log` entries dated
2026-07-10 through 2026-07-13 for full narrative detail on each item above.
