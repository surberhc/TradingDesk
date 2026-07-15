"""
s8_config.py — Frozen constants for S8 (British IC + B2 long-leg auto-close), the SPX
0DTE scheduled credit-spread pair strategy. See docs/S8_SPEC.md for the full ruleset;
section numbers in comments below refer to that document.

Stage 1 of a 5-stage build (paperbot/s8_chain.py, s8_strategy.py, s8_risk.py,
s8_runner.py follow in later stages — NOT built here). This file is DATA ONLY: no
logic, no IBKR imports, nothing computed. Engine modules built in later stages read
these values rather than hard-coding magic numbers, mirroring strategies/config.py's
convention.

Per CLAUDE.md rule #1 (never curve-fit): every constant here traces either directly to
docs/S8_SPEC.md's prose or to a direct, reproducible read of british_ic/'s real-fills
CSVs (ground truth). Nothing here is invented, tuned, or hand-smoothed. Where the real
data does not cleanly support what the spec's prose describes, that gap is stated
plainly in a comment rather than silently resolved either direction.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Account — INTENTIONALLY "TBD" (fail-closed)
# ---------------------------------------------------------------------------
# S8's live pilot targets the new live-TRADING Gateway (connections.ibkr_live_trade, port
# 4003), whose login covers two individual live-trading TEST accounts. S8 will use ONE
# of those two test accounts — but Andrew has not yet provided the specific account
# number, so this stays the "TBD" placeholder on purpose (fail-closed).
#
# While ACCOUNT == "TBD", livebot/s8_runner.py REFUSES to run (returns 2) before making
# any Gateway contact — see its main() TBD guard and module docstring. Set this to the
# real test-account number only when Andrew provides it; do NOT invent one.
ACCOUNT = "TBD"


# ---------------------------------------------------------------------------
# Templates (S8_SPEC.md §2, §2.3) — 11 total
# ---------------------------------------------------------------------------
# The set of 11 templates below is taken verbatim from british_ic/template_delta_stats.csv's
# `Template` column (one row per real, observed side/width/dollar combo across the full
# reconstruction window) — NOT assumed from S8_SPEC.md §2.1's prose, since §2.1 writes
# the families as "{Puts|Calls} - 80/50 - $2/3/4" shorthand that reads as if all 6
# width/dollar combos exist symmetrically on both sides. They do not: Calls-80-$2 does
# not appear anywhere in template_delta_stats.csv (only Puts-80-$2 exists), matching
# §2.1's own separate, asymmetric bullet for "Puts - 80 - $2" as a distinct
# "smaller/secondary configuration" — so the asymmetry is confirmed by the data, not
# an assumption on top of the spec.
#
# `stop_multiple` values below are cross-checked exactly against BOTH S8_SPEC.md §2.3's
# table AND template_delta_stats.csv's `stopmultiple_mean` / `stopmultiple_values`
# columns (which show a single deterministic constant per template, no variance) — the
# two sources agree to the decimal, no discrepancy.
#
# `target_credit` is the template's labeled dollar figure ($2/$3/$4), which S8_SPEC.md
# §2.2 documents as the primary dial ("medians land almost exactly on the labeled
# figure") — NOT the exact observed median (which varies slightly, e.g. $2.05-2.15 for
# the "$2" label per §2.2). The label itself is the frozen target; do not replace it
# with a measured median without Andrew's blessing (that would be re-deriving a
# tunable parameter from a backtest sample, which rule #1 forbids).
#
# `width_label` is the "80"/"50" naming label ONLY — per S8_SPEC.md §2.2, this is
# explicitly NOT a strike-width value ("realized widths range 5-85 points"; the label
# is "best read as a stop-aggressiveness / target-win-rate setpoint," see §2.3's
# implied breakeven win rate column). Do not treat width_label as a literal point-width
# anywhere downstream.
TEMPLATES = {
    "Puts-80-$4":  {"side": "Puts",  "width_label": 80, "target_credit": 4.0, "stop_multiple": 3.3},
    "Puts-80-$3":  {"side": "Puts",  "width_label": 80, "target_credit": 3.0, "stop_multiple": 2.4},
    "Puts-80-$2":  {"side": "Puts",  "width_label": 80, "target_credit": 2.0, "stop_multiple": 2.0},
    "Puts-50-$4":  {"side": "Puts",  "width_label": 50, "target_credit": 4.0, "stop_multiple": 3.2},
    "Puts-50-$3":  {"side": "Puts",  "width_label": 50, "target_credit": 3.0, "stop_multiple": 2.4},
    "Puts-50-$2":  {"side": "Puts",  "width_label": 50, "target_credit": 2.0, "stop_multiple": 2.0},
    "Calls-80-$4": {"side": "Calls", "width_label": 80, "target_credit": 4.0, "stop_multiple": 3.3},
    "Calls-80-$3": {"side": "Calls", "width_label": 80, "target_credit": 3.0, "stop_multiple": 2.4},
    "Calls-50-$4": {"side": "Calls", "width_label": 50, "target_credit": 4.0, "stop_multiple": 3.2},
    "Calls-50-$3": {"side": "Calls", "width_label": 50, "target_credit": 3.0, "stop_multiple": 2.4},
    "Calls-50-$2": {"side": "Calls", "width_label": 50, "target_credit": 2.0, "stop_multiple": 2.0},
    # NOTE: no "Calls-80-$2" — confirmed absent from template_delta_stats.csv (see
    # comment block above). 11 templates total, not 12.
}


# ---------------------------------------------------------------------------
# Entry-time grid per template (S8_SPEC.md §2.1) — EMPIRICALLY DERIVED, not hand-typed
# ---------------------------------------------------------------------------
# METHOD (reproducible — re-run this to audit or refresh):
#   1. Read british_ic/tat_full_join.csv.
#   2. Filter to tat_match == 'MATCHED' only (748 of 1,617 total rows) — the
#      ground-truth-labeled subset where a real TAT (NinjaTrader) log entry was
#      confidently joined to the real IBKR fill, per the col's own value; the other
#      buckets (NO_TAT_COVERAGE, AMBIGUOUS_MULTI_CANDIDATE, NO_MATCH) are excluded
#      because their template label isn't trustworthy ground truth.
#   3. Group by the `tat_Template` column (the TAT log's own template string).
#   4. Take each row's `short_open_dt` (the real IBKR execution timestamp of the short
#      leg's opening fill) as the entry time.
#   5. TIMEZONE INFERENCE — FLAGGED, not certain: `short_open_dt` carries no explicit
#      timezone in the CSV. It is inferred to be US/Eastern (IBKR Flex Query's default
#      reporting timezone), based on: (a) the earliest entries in the -80-$4 templates
#      cluster at 09:33-09:35 raw — only ~3-5 min after the 09:30 ET cash open, and (b)
#      every closed-at-expiry row in this dataset reports a uniform 16:20:00 raw close
#      timestamp, ~20 min after the 16:00 ET cash close (a standard post-close
#      settlement-processing lag), not ~1h20m after a 15:00 CT close. Converting
#      raw-1h (ET->CT) then lines up the -80-$4 morning cluster's peak slots almost
#      exactly on S8_SPEC.md §2.1's stated "08:45, 09:15" grid points. This inference
#      is NOT independently confirmed against a labeled timezone field anywhere in
#      british_ic/ — flagging plainly per the no-silent-smoothing instruction, rather
#      than asserting it as fact. All grid times below are this ET-minus-1h CT
#      conversion, rounded to the nearest 5 minutes.
#   6. "Core" slot = a rounded time appearing >= 3 times in that template's MATCHED
#      subset (a floor chosen only to drop one-off singleton noise, not tuned to hit
#      any particular shape). `coverage_pct` = the share of that template's MATCHED
#      rows falling on a core slot.
#
# Re-entries after a stop-out are NOT modeled as a separate reactive rule here — per
# S8_SPEC.md §2.1, they follow the SAME fixed clock grid, so no separate re-entry
# timing constant is needed; the grid below already includes any such re-entries that
# happened to land in the MATCHED subset.
#
# RESULTS (n = MATCHED rows for that template; times are CT, 5-min rounded):
#
#   Puts-80-$4   (n=244, core coverage 89.3%):
#     08:35(31) 08:45(43) 08:50(36) 09:05(37) 09:15(16) 09:20(18)
#     10:55(3) 11:15(3) 12:05(13) 12:20(10) 12:35(4) 13:00(4)
#   Calls-80-$4  (n=193, core coverage 91.7%):
#     08:35(20) 08:45(50) 08:50(23) 09:05(22) 09:15(11) 09:20(10)
#     10:00(3) 10:40(3) 10:50(3) 12:05(16) 12:20(9) 12:35(3) 13:00(4)
#   Puts-50-$2   (n=121, core coverage 76.0%):
#     11:20(3) 12:05(15) 12:20(10) 12:35(13) 13:00(14) 13:10(13) 13:15(3) 13:25(11) 13:35(10)
#   Calls-50-$2  (n=84,  core coverage 73.8%):
#     12:05(16) 12:20(4) 12:35(6) 13:00(13) 13:10(5) 13:15(5) 13:25(5) 13:35(5) 14:05(3)
#   Puts-80-$3   (n=50,  core coverage 72.0%):
#     11:30(3) 12:05(9) 12:20(10) 12:35(10) 13:00(4)
#   Calls-80-$3  (n=44,  core coverage 68.2%):
#     10:50(3) 11:25(3) 12:05(15) 12:20(6) 12:35(3)
#   Puts-80-$2   (n=8):   ALL singleton slots (12:00,12:20,12:25,12:35,13:05,13:30,13:35,13:40)
#                          — no slot repeats >= 3x. Too thin to name a "core" grid; consistent
#                          with S8_SPEC.md §2.1's own description of this template as having
#                          "no dominant slot," but n=8 cannot statistically confirm that framing
#                          either, and the observed spread (12:00-13:40 CT) is narrower than the
#                          spec's stated 10:00-14:15 CT range. ENTRY_GRID_CT is None here — do
#                          NOT invent a grid; if this template is ever run live, treat its entry
#                          timing as genuinely unresolved pending more MATCHED data.
#   Calls-50-$3  (n=2):   08:45(1) 09:05(1) — too thin to derive anything. None.
#   Puts-50-$3   (n=1):   10:10(1) — a single data point. None.
#   Puts-50-$4   (n=1):   09:50(1) — a single data point. None.
#   Calls-50-$4  (n=0):   ZERO MATCHED rows anywhere in tat_full_join.csv — this template
#                          string never appears in the `tat_Template` column at ANY match
#                          status (checked across all 1,617 rows, not just MATCHED). There is
#                          NO empirical entry-timing evidence for Calls-50-$4 in this dataset at
#                          all. S8_SPEC.md §2.1 asserts a "-50-$4 afternoon grid ~12:15-13:45 CT
#                          (12:15, 12:45, 13:00, 13:30 account for ~97%)" claim that covers this
#                          template's family, but that claim cannot be verified against
#                          real MATCHED fills for the Calls side specifically — flagging this as
#                          the most significant spec-vs-real-data gap found in this derivation.
#
# DISCREPANCIES vs. S8_SPEC.md §2.1's prose (stated plainly, not resolved):
#   1. Calls-50-$4 has ZERO TAT-matched real fills to confirm the spec's stated afternoon
#      grid for this side. Puts-50-$4 has exactly ONE. The spec's "-50-$4" grid claim is
#      effectively UNVERIFIED by this method for both sides of this template family.
#   2. -80-$4 (both sides) shows a real, non-trivial SECOND cluster at 12:05-13:00 CT in
#      the MATCHED data (13-16 occurrences per side) that S8_SPEC.md §2.1's prose does not
#      mention at all (it describes only the "08:45-11:00 CT" morning grid for this
#      family). Not large enough to call it a second official grid, but too large (>10%
#      of each side's MATCHED sample) to dismiss as noise either.
#   3. S8_SPEC.md §2.1 gives no entry-grid prose whatsoever for the "-50-$2" or "-50-$3"
#      families (only -80-$4, -80-$3, -50-$4, and Puts-80-$2 get a description). The real
#      MATCHED data reveals -50-$2 (both sides, n=121/84) has a substantial, real early-
#      afternoon cluster (~12:05-14:05 CT) that is documented here for the first time,
#      not previously written down anywhere in the spec.
#
# ENTRY_GRID_CT: template key -> sorted list of "HH:MM" core entry times (CT), or None
# where the MATCHED sample is too thin (n < 3 occurrences at any single slot) to name a
# grid at all. Consumers (s8_runner.py, a later stage) must treat None as "do not fire
# this template on a schedule yet" rather than defaulting to an empty list silently.
ENTRY_GRID_CT = {
    "Puts-80-$4":  ["08:35", "08:45", "08:50", "09:05", "09:15", "09:20",
                    "10:55", "11:15", "12:05", "12:20", "12:35", "13:00"],
    "Calls-80-$4": ["08:35", "08:45", "08:50", "09:05", "09:15", "09:20",
                    "10:00", "10:40", "10:50", "12:05", "12:20", "12:35", "13:00"],
    "Puts-50-$2":  ["11:20", "12:05", "12:20", "12:35", "13:00", "13:10",
                    "13:15", "13:25", "13:35"],
    "Calls-50-$2": ["12:05", "12:20", "12:35", "13:00", "13:10", "13:15",
                    "13:25", "13:35", "14:05"],
    "Puts-80-$3":  ["11:30", "12:05", "12:20", "12:35", "13:00"],
    "Calls-80-$3": ["10:50", "11:25", "12:05", "12:20", "12:35"],
    "Puts-80-$2":  None,   # n=8, all singleton slots — see discrepancy note above
    "Calls-50-$3": None,   # n=2 — see discrepancy note above
    "Puts-50-$3":  None,   # n=1 — see discrepancy note above
    "Puts-50-$4":  None,   # n=1 — see discrepancy note above
    "Calls-50-$4": None,   # n=0 — ZERO matched fills, most significant gap found
}
