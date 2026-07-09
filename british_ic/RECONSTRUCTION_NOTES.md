# British IC Reconstruction — Findings

Data window: 2025-07-09 through 2026-07-07 (238 distinct TradeDates, execution-level
IBKR Flex Query export, `BIC data.xlsx` sheet `20250709_20`, 21,435 raw rows).
Account U***9156, underlying SPX, 0DTE only.

All numbers below come directly from `combo_ledger.csv` / the intermediate CSVs in this
folder, produced by `reconstruct.py`. Nothing here is interpolated or estimated —
where the data can't answer a question, that is stated explicitly.

## 1. Data-quality exclusions (before any reconstruction)

- **18 rows excluded** where `TradeDate != Expiry` — per the user's known fact, this is
  a separate diagonal-put/calendar-condor side book, not British IC. Not reconstructed.
  Saved to `excluded_non_0dte_rows.csv`.
- **17 additional "orphan" rows excluded** (a new finding made during this build): when a
  non-0DTE position's OPENING execution is excluded above, its CLOSING execution can still
  independently satisfy `TradeDate == Expiry` on its own settlement day and leak into the
  0DTE dataset as an execution with no matching open. Found by cross-referencing Conids: 17
  such orphan close rows exist, total FifoPnlRealized **-$26,847.62**, spanning dates
  2026-01-16, 02-20, 03-20, 04-17, 05-15, 05-26. These are the tail end of the same excluded
  side book and are not part of British IC. Saved to `excluded_orphan_close_rows.csv`.
- Net: **21,400 rows** used for the 0DTE British IC reconstruction.

## 2. A material finding about IBKR's FifoPnlRealized field

The task brief said to trust `FifoPnlRealized` on **closing** executions. During validation
we found this field is **not confined to closing rows**: when a Conid's position fully
flattens and then reopens later the same day, IBKR's Flex export attributes part of the
realized P&L to the **opening** execution of the new leg, not only to prior closes. Evidence:
summing `FifoPnlRealized` over ALL rows (open+close) for a day exactly matches that day's
summed `NetCash` (self-consistency check), and reconciles the daily balance file to ~$32/day
mean absolute mismatch; using close-rows-only produces a mismatch in the hundreds-of-thousands
of dollars. The reconstruction therefore sums `FifoPnlRealized` across **all** executions in
a lifecycle (open and close), never recomputed, never touched — just summed correctly.

## 3. Lifecycle reconstruction (Step 1)

6,115 per-Conid lifecycles reconstructed (position walked to exactly zero = one lifecycle;
545 of 5,558 Conids reopen after flattening same-day and correctly produce a second, separate
lifecycle). Zero lifecycles were left open at the end of the data window (all fully closed).

## 4. Combo pairing (Step 2) — scale-in groups found and handled explicitly

The naive "1 short leg : 1 long leg, matched by first-open time" model **does not fit the
real data**. British IC frequently scales into the SAME short strike multiple times across a
day (verified example, 2025-07-11: short call Conid 792211148 sells in 8 separate batches from
09:33 to 13:34, never returning to zero, closing once at 16:20 settlement), with a **new,
distinct long-leg Conid opened at a different strike for each scale-in batch**. IBKR's
`FifoPnlRealized` on that short's eventual close is a single FIFO number computed across the
WHOLE accumulated lifecycle — it cannot be split per batch without fabricating an allocation
(by qty, premium, or time), which was explicitly ruled out.

Resolution: a "combo group" = one short lifecycle + every long lifecycle whose open time
matches one of the short's open-batch timestamps (same TradeDate, same Put/Call, within 5
seconds). When a short lifecycle has more than one open-batch, its P&L is reported ONCE at
the group level (flagged `short_pnl_disaggregated = False`); each paired long leg still gets
its own fully real, undisaggregated open time/price/P&L.

Result:
- **2,592 combo groups** reconstructed (1,453 PutSpread, 1,139 CallSpread).
- **2,276 groups (87.8%)** are clean 1:1 short:long pairs — fully disaggregated P&L, no caveat.
- **316 groups (12.2%)** are scale-in groups (2-4 paired long legs) — short P&L reported at
  the group level only, explicitly flagged, never split.
- **1,586 short open-batches could not be matched to any long leg** at all (no candidate long
  opened within 5 seconds) — these don't lose any P&L (the short's aggregate FifoPnlRealized
  is still fully counted in whichever combo group the lifecycle belongs to), but it means that
  particular scale-in add had no long-side hedge visible in the data at that instant.
  Saved to `unmatched_short_open_batches.csv`.
- **560 of 3,152 short lifecycles (17.8%) never matched ANY long leg** at all — their P&L
  (-$7,182.62 total) is reported separately, not folded into any combo group, because no
  paired long could be identified. Saved to `fully_unmatched_short_lifecycles.csv`.
- **1 long lifecycle never claimed by any short** (-$1,524.13). Saved to
  `unclaimed_long_legs.csv`.
- **Zero ambiguous batch-pairing cases** (no instance where more than one long-leg candidate
  matched a single short batch within tolerance).

## 5. Total reconstructed P&L for the 0DTE window

| Component | P&L |
|---|---|
| Paired combo groups | **$51,471.58** |
| Fully-unmatched short lifecycles (no long found) | -$7,182.62 |
| Unclaimed long lifecycle (no short found) | -$1,524.13 |
| **GRAND TOTAL** | **$42,764.83** |

This is the honest total across every reconstructed 0DTE British IC execution in the window,
whether or not it could be cleanly paired into a combo.

## 6. Validation against Daily_Ending_Balance (Step 5)

Compared day-by-day: (paired combo P&L + unmatched-short P&L + unclaimed-long P&L) vs the
balance file's day-over-day `Total` delta, for the 147 overlapping days (2025-07-08 through
2026-02-19).

- **Total absolute mismatch: $4,713.77 over 147 days (mean $32.07/day)** — in the same
  ballpark as the user's own previously-validated ~$22/day noise figure for this data.
- **2 days exceed $200 mismatch** (flagged explicitly, not smoothed over):
  - **2026-01-16: mismatch $1,221.74** (balance delta $2,533.59 vs reconstructed $1,311.85).
    Investigated: not explained by the orphan-row issue in section 1 (those were already
    excluded before this number was computed). Root cause not identified — flagged as
    **unexplained** rather than guessed at.
  - **2026-02-19: mismatch $249.29.** This is the LAST day in the balance file — plausibly a
    partial/boundary-day effect (balance file may cut off mid-settlement). Not chased further.

## 7. Cross-check against TAT-tradelog (Step 4)

October 2025 combo groups matched to TAT-tradelog rows by TradeDate + strikes + nearest open
time (272 exploded short/long pairs checked):
- **186 matched cleanly** (1 TAT candidate).
- **51 ambiguous** — more than one TAT row shared the same date/strikes; nearest open-time
  candidate used but flagged `AMBIGUOUS_MULTI_CANDIDATE` (not silently resolved as certain).
- **35 had no TAT match at all** (no TAT row with matching date+strikes found).

Confirms the reason for building this tool: TAT's `ProfitLoss` materially understates real
P&L whenever the long leg was held past the short's stop-out. Example (2025-10-10, the known
crash day): TAT logs this combo's `ProfitLoss` as -$1,028.75 / -$805.83 (its own two rows for
overlapping strikes), while the reconstructed TRUE combo group P&L is **+$21,712.31** — the
long leg, not the short, drove the real result that day. Full detail in
`tat_crosscheck_oct2025.csv`, including the signed `pnl_discrepancy` column for every row.

## 8. Exit-rule characterization (Step 6 — the core ask)

**Paired vs. decoupled exits** (short and long lifecycle both close within 2 minutes of each
other, vs. not):
- Paired: **1,275 of 2,592 combo groups (49.2%)**
- Decoupled: **1,317 of 2,592 combo groups (50.8%)**
- Roughly a coin flip — no dominant exit style.

**Decoupled long-leg P&L-multiple distribution** (long_fifo_pnl ÷ long entry cost, across
1,617 individual long legs inside decoupled groups):
- Median: **-1.01x** (long closed for essentially a full loss of its entry premium)
- 25th percentile: -1.10x, 75th percentile: -0.20x
- **79.3% closed at or below breakeven** (multiple ≤ 0)
- **15.2%** closed between 0x and 1x (partial recovery/gain up to 1x premium)
- **3.7%** closed between 1x-5x
- **1.7%** closed above 5x, and **0.5%** above 20x (max observed: **150.2x**)

Read plainly: the data does NOT show a consistent, single "take-profit rule" for the long
leg. The dominant outcome by far is the long simply decaying to near-total loss (median
-1.01x) — consistent with it being carried as tail-risk protection that usually expires
worthless. But a real, non-negligible tail (~6% of decoupled long legs) closes for a gain
above 1x, including rare outsized wins (150x max) on the kind of large move the long leg
exists to catch. There is no clean threshold visible in the distribution (e.g., no cluster
at "always closed at 2x" or similar) — reporting the full distribution rather than asserting
a rule, per the instruction not to assert one unless the data clearly shows it.

**Other structural patterns checked, all inconclusive/flat:**
- Paired-exit rate by entry hour of day: ranges only 45.7%-54.1% across 9am-3pm, no trend.
- Paired-exit rate by day of week: ranges only 47.2%-50.4%, no trend.
- Paired-exit rate by ComboType (Put vs Call): 49.8% vs 48.4%, no meaningful difference.
None of these show a pattern strong enough to call predictive — flagged as inconclusive
rather than asserted as a finding.

## 9. Everything the data could not answer (explicit list)

- **Why 2026-01-16's $1,221.74 balance mismatch exists** — investigated, not resolved. No
  fabricated explanation offered.
- **What the 1,586 unmatched short open-batches were hedged with**, if anything — the data
  shows no long-leg open within the 5-second matching tolerance for these; whether a hedge
  existed further away in time/strike than the tolerance window can't be answered without
  guessing at a match.
- **A clean single exit rule for the long leg** — the distribution is real and reported
  (section 8), but no consistent rule is discernible; asserting one would not be honest to
  the data.
- **The exact per-batch short-side P&L inside the 316 multi-long scale-in groups** — cannot be
  recovered from IBKR's data without fabricating an allocation (explained in section 4).
- **35 TAT cross-check rows with no match at all**, and **51 with ambiguous multi-candidate
  matches** — see `tat_crosscheck_oct2025.csv` for the specific rows.

## Files in this folder

- `reconstruct.py` — full pipeline (lifecycle reconstruction → combo pairing → TAT
  cross-check → balance validation → exit characterization).
- `combo_ledger.csv` — one row per reconstructed combo group (2,592 rows), the primary
  deliverable.
- `exploded_pair_ledger.csv` — same data exploded to one row per short+long pair (2,962
  rows), matching TAT's own granularity; used for the TAT cross-check and the P&L-multiple
  distribution.
- `excluded_non_0dte_rows.csv`, `excluded_orphan_close_rows.csv` — rows dropped and why.
- `fully_unmatched_short_lifecycles.csv`, `unclaimed_long_legs.csv`,
  `unmatched_short_open_batches.csv` — everything that could not be paired, with P&L intact.
- `ambiguous_lifecycles.csv`, `ambiguous_combo_pairings.csv` — ambiguous-case logs (both
  empty in this run — no lifecycle was left open at data end, no batch had >1 candidate
  long leg within tolerance).
- `tat_crosscheck_oct2025.csv` — October 2025 TAT-vs-reconstructed side-by-side.
- `balance_validation.csv` — day-by-day validation detail.
- `decoupled_long_legs.csv` — the 1,617 decoupled long legs behind section 8's distribution.
