# British IC — Entry/Stop Mechanics (from TAT-tradelog.xlsx)

Data source: `NT BIC Data\TAT-tradelog.xlsx`, sheet `TAT-tradelog`, 4,687 rows,
Sept 2024 – Mar 2026. This is a **different, coarser-granularity dataset** than the
IBKR Flex reconstruction in this folder (`RECONSTRUCTION_NOTES.md`) — that project
covers 2025-07-09 to 2026-07-07 execution-level data and found paired-exit rate ~49%,
long legs mostly decaying to near-total loss (median -1.01x), no clean single
take-profit rule for the long leg. This document does **not** repeat or redo that
work — it is purely entry/stop **mechanics** (timing, template naming, stop formula,
re-entry pattern) derived from the separate TAT trade log. `ProfitLoss` in this file
is not used anywhere below (it's proven wrong per the task brief) — only entry-side
fields (times, strikes, deltas, prices, stop fields).

Analysis scripts are throwaway, run from
`C:\Users\andre\AppData\Local\Temp\claude\...\scratchpad\` — not saved into this repo.

---

## 1. Entry timing distribution

All 4,687 rows have `TimeOpened` between 08:45 and ~14:15 (the data appears to be in
a timezone where the trading day opens at 08:45 — consistent with CT, per the
Tiingo-schedule convention already in use elsewhere in TradingDesk).

**Overall distribution (15-min buckets), all templates combined:**

| Time bucket | Count | % |
|---|---|---|
| 08:45–09:00 | small | – |
| 09:15–09:45 | moderate | – |
| 12:00–12:15 | 41 | 21.6%* |
| 12:15–12:30 | 44 | 23.2%* |
| 12:30–12:45 | 23 | 12.1%* |
| 12:45–13:00 | 16 | 8.4%* |

*(percentages shown are from the Calls-80-$3 template slice printed during the run;
see full per-template tables below, which are the more informative cut — **entry
timing is template-specific, not a single global distribution**.)

**Key finding: each Template has its own fixed intraday entry schedule**, essentially
a small number of clock-time "slots" per day, not a continuous/random distribution:

- **`British IC - Calls/Puts - 80 - $4`** — fires mainly in the morning session:
  clusters at 08:45, 09:15, 09:45 (and periodically 10:15/10:45), i.e. a 30-minute
  grid roughly 08:45–11:00.
- **`British IC - Calls/Puts - 80 - $3`** — fires mid-morning through early afternoon,
  concentrated 12:00–13:00 (this specific template shows ~65% of its 186 rows inside
  the single 12:00–12:45 window), tapering off by 14:15.
- **`British IC - Calls/Puts - 50 - $4`** (n=107 each) — almost entirely afternoon:
  12:15–12:30 (33.6%), 12:45–13:00 (27.1%), 13:00–13:15 (21.5%), 13:30–13:45 (15.0%).
  Essentially 4 clock slots account for ~97% of all entries; 3 stray early-morning
  singletons (08:45, 09:45, 10:45) are the only outliers.
- **`British IC - Puts - 80 - $2`** (n=37, the smallest template) — spread across
  late morning/early afternoon (10:00–14:15), no single dominant slot, consistent
  with it being a rarer/secondary configuration.

**Conclusion:** entries are **scheduled by clock time per template**, not triggered by
market conditions at arbitrary moments — the same few time-of-day slots repeat across
the whole 18-month sample for a given template. This matters directly for item 5
below.

---

## 2. What "80" / "50" mean in the template name

Templates: `British IC - {Puts|Calls} - {80|50} - ${2|3|4}`.

Tested the literal hypotheses against the actual short-leg delta at entry
(`PutDelta`/`CallDelta`):

| DeltaToken | Side | n | mean \|delta\|×100 | median | std |
|---|---|---|---|---|---|
| 80 | Puts | 1,446 | 24.2 | 23.7 | 6.7 |
| 50 | Puts | 905 | 25.8 | 25.2 | 7.5 |
| 80 | Calls | 1,399 | 25.7 | 25.4 | 6.7 |
| 50 | Calls | 937 | 27.0 | 26.6 | 7.4 |

Controlling for width (isolating just the `$4` templates, the largest bucket, so
width isn't confounding the comparison):

| DeltaToken | Side | n | mean \|delta\|×100 | median |
|---|---|---|---|---|
| 80 | Puts | 1,219 | 23.8 | 23.2 |
| 50 | Puts | 107 | 28.0 | 26.9 |
| 80 | Calls | 1,213 | 25.2 | 24.9 |
| 50 | Calls | 107 | 28.5 | 27.7 |

**"80" does not mean delta=0.80** — realized short-leg deltas cluster at ~22–29%
regardless of the 80/50 label, nowhere close to 80. It also doesn't cleanly mean
"100−delta = 80%" (a probability-OTM framing) — the actual (100−|delta|×100) values
run ~72–76% across both labels, not a clean 80/50 split, and there's no template
where the number in the name matches either delta or 1−delta well.

**What actually differs cleanly by the 80/50 label is the stop configuration.**
Cross-tabulating `StopMultiple` against template shows it is **fully deterministic**
per exact template name (zero within-template variance):

| Template | StopMultiple |
|---|---|
| `...- 80 - $4` | 3.3 (both Puts & Calls) |
| `...- 80 - $3` | 2.4 |
| `...- 80 - $2` (Puts only) | 2.0 |
| `...- 50 - $4` | 3.2 |
| `...- 50 - $3` | 2.4 |
| `...- 50 - $2` | 2.0 |

Interpreting the stop multiple as an implied max-loss-to-credit ratio and converting
to a breakeven win rate (`multiple / (multiple + 1)`):

| StopMultiple | Implied breakeven win rate |
|---|---|
| 2.0 | 66.7% |
| 2.4 | 70.6% |
| 3.2 | 76.2% |
| 3.3 | 76.7% |

**Conclusion: "80"/"50" is a target-configuration label (loosely, an intended
win-rate/aggressiveness setpoint tied to the stop multiple used), not a literal
delta value.** The "80" family consistently uses the wider/looser stop multiples
(3.3 or 2.4) implying a higher target win rate (~77% / ~71%), while the "50" family
mixes 2.0/2.4/3.2 — closer to a 67–76% breakeven band. The realized short-leg delta
(~22–29% across the board) is a secondary consequence of hitting the target credit
price (see item 3), not something separately dialed by the 80/50 token. This is an
empirical, not a hand-waved, conclusion — the delta hypothesis was directly tested
and fails; the stop-multiple/win-rate-label hypothesis is what the data actually
supports.

---

## 3. What "$2" / "$3" / "$4" mean

Tested against literal point-width (`ShortPut - LongPut` / `LongCall - ShortCall`):

| WidthToken | n | mean width (pts) | median | std | min | max |
|---|---|---|---|---|---|---|
| 2 | 844 | 48.3 | 45 | 9.5 | 15 | 85 |
| 3 | 1,197 | 55.6 | 50 | 13.6 | 5 | 85 |
| 4 | 2,646 | 73.9 | 80 | 10.7 | 15 | 85 |

**This rules out literal point-width** — actual widths run 5–85 points, nowhere near
2–4. Tested instead against `PriceOpen` (the entry credit received per spread):

| WidthToken | n | mean PriceOpen | median | std | min | max |
|---|---|---|---|---|---|---|
| 2 | 844 | $2.08 | $2.10 | 0.29 | $1.35 | $2.75 |
| 3 | 1,197 | $3.04 | $3.00 | 0.41 | $2.15 | $4.50 |
| 4 | 2,646 | $4.13 | $4.15 | 0.38 | $3.10 | $9.40 |

Per exact template, `PriceOpen` medians land almost exactly on the labeled dollar
figure (e.g. `...-$2` templates: median PriceOpen $2.05–$2.15; `...-$3`: median
$3.00–$3.08; `...-$4`: median $3.95–$4.20). `TotalPremium` (= PriceOpen × 100 × Qty,
roughly) confirms the same pattern (mean ~$615/$608/$740 across the $2/$3/$4 groups,
matching a ~$2/$3/$4-per-spread credit times 100 multiplier).

**Conclusion: "$2"/"$3"/"$4" is the target entry credit price per spread
(`PriceOpen`), not a strike-width.** The strategy strike-selects (short strike, then
width) to hit approximately that credit target; the resulting point-width and short-
leg delta are downstream consequences, which is exactly why width varies 15–85 points
and delta varies ~22–29% even within one nominal "$4" template — the credit target is
the fixed input, not the width or the delta.

---

## 4. Stop formula (`StopType == 'ShortREL'`)

`StopType` values: `ShortREL` (4,611 rows, 98.4%) and `Short` (76 rows, 1.6%).
Derivation below is for `ShortREL`; spot-checking the 76 `Short` rows shows they fit
the **same** formula (e.g. PriceOpen=4.30, StopMultiple=3.3, PriceStopTarget=7.6 —
matches exactly), so the formula generalizes to both.

Candidate formulas tested (MAE against actual `PriceStopTarget`, n=4,610 non-null
`ShortREL` rows):

| Candidate formula | MAE | Exact match (±0.01) |
|---|---|---|
| `PriceOpen − StopMultiple×PriceShort` | 13.75 | 0% |
| `PriceShort×(1+StopMultiple) − PriceLong` | 8.12 | 0% |
| `−StopMultiple×PriceOpen` | 16.57 | 0% |
| `PriceOpen×(1−StopMultiple)` | 13.09 | 0% |
| `PriceOpen + StopMultiple` (raw, unrounded) | 0.017 | 65.1% |
| **`floor(10×(PriceOpen + StopMultiple)) / 10`** | **0.0000** | **99.98%** (4,609/4,610; last row differs by <0.001, a float rounding artifact) |

**Formula: `PriceStopTarget = floor(10 × (PriceOpen + StopMultiple)) / 10`** — i.e.
add the stop multiple (as dollars) directly to the entry credit price, then round
**down** to the nearest $0.10 tick. This makes economic sense as a stop-loss target
quoted in spread-price terms: the position is stopped when the spread's price rises
to (entry credit + StopMultiple), i.e. the position is down `StopMultiple` dollars
per spread from where it was opened.

**Sample rows (15, spanning all 4 observed StopMultiple values 2.0/2.4/3.2/3.3):**

| PriceOpen | PriceShort | StopMultiple | PriceStopTarget (actual) | Predicted | Residual |
|---|---|---|---|---|---|
| 2.35 | 2.47 | 2.0 | 4.3 | 4.3 | 0.0 |
| 3.00 | 3.47 | 2.4 | 5.4 | 5.4 | 0.0 |
| 4.55 | 5.10 | 3.3 | 7.8 | 7.8 | 0.0 |
| 2.25 | 2.25 | 2.0 | 4.2 | 4.2 | 0.0 |
| 2.25 | 2.62 | 2.0 | 4.2 | 4.2 | 0.0 |
| 3.70 | 4.02 | 3.3 | 7.0 | 7.0 | 0.0 |
| 4.80 | 4.80 | 3.3 | 8.1 | 8.1 | 0.0 |
| 3.60 | 3.60 | 2.4 | 6.0 | 6.0 | 0.0 |
| 2.25 | 2.35 | 2.0 | 4.2 | 4.2 | 0.0 |
| 2.65 | 2.75 | 2.0 | 4.6 | 4.6 | 0.0 |
| 3.60 | 3.72 | 3.3 | 6.9 | 6.9 | 0.0 |
| 2.30 | 2.40 | 2.0 | 4.3 | 4.3 | 0.0 |
| 2.05 | 2.17 | 2.0 | 4.0 | 4.0 | 0.0 |
| 1.80 | 2.00 | 2.0 | 3.8 | 3.8 | 0.0 |
| 3.40 | 3.72 | 2.4 | 5.8 | 5.8 | 0.0 |

Note `PriceShort` (the SHORT leg's own entry price) does **not** appear in the
formula at all — the stop target is derived entirely from the spread's own
`PriceOpen` and `StopMultiple`, not from the individual short leg's price. This was
tested and rejected as a candidate (row 1 of the candidate table above).

**Full-set summary statistics (n=4,610):**
- Mean absolute error: **0.0000**
- Median absolute error: **0.0000**
- Exact match rate (tolerance <0.001): **99.98%** (4,609 / 4,610)
- No systematic bias, no unresolved residual pattern — the formula is exact.

---

## 5. Scale-in / re-entry pattern

Built same-day, same-`TradeType` (PutSpread/CallSpread) groups, then found all pairs
of entries whose short-to-long strike **ranges overlap** (a proxy for "the same
strike family / underlying zone"). 613 (Date, TradeType) groups had ≥2 entries;
17,024 overlapping pairs total, 8,891 of which share the **same exact Template**
(the cleanest scale-in test, since different templates run on independent clock
schedules per item 1).

**Same-template overlapping pairs (n=8,891):**

| Metric | Value |
|---|---|
| Gap: next open − prior open (min), median | 36.0 |
| Gap: next open − prior open (min), 25th/75th pct | 22.6 / 85.0 |
| Gap: next open − prior close (min), median | −65.1 |
| Gap: next open − prior close (min), 25th/75th pct | −146.0 / +15.7 |
| Prior trade's Status (i.e. what happened to the position before the "next" one opened) | Stopped 4,807 (54%) / Expired 3,810 (43%) / Manual Closed 274 (3%) |
| Pairs where next-open lands within ±5 min of prior-close | **428 / 8,891 = 4.8%** |

Even restricted to prior=`Stopped` trades (n=4,807, the case where a stop-out
"freeing up" the slot could plausibly trigger a fast re-entry), the median gap from
the prior close to the next open is **+11.9 minutes**, but the interquartile range
runs **−22.0 to +53.2 minutes** — wide and centered near, not tightly locked to,
zero. Combined with the 25th percentile being negative (i.e., the "next" trade in
many cases opened *before* the prior one even closed), this is inconsistent with a
"wait for stop-out, then re-enter" rule.

**Concrete examples (real timestamps, 2024-09-16):**

| Template | Prior entry (strike, open→close, status) | Next entry (strike, open) | Gap open→open | Gap (next open − prior close) |
|---|---|---|---|---|
| Calls-80-$4 | 5640, 08:45:07→08:52:43, Stopped | 5650, 09:15:13 | 30.1 min | 22.5 min |
| Calls-80-$4 | 5640, 08:45:07→08:52:43, Stopped | 5635, 09:45:06 | 60.0 min | 52.4 min |
| Calls-80-$4 | 5650, 09:15:13→15:00:02, Expired | 5635, 09:45:06 | 29.9 min | **−314.9 min** |
| Calls-50-$4 | 5630, 12:25:08→12:52:53, Stopped | 5635, 12:55:07 | 30.0 min | 2.2 min |
| Puts-80-$4 | 5585, 08:45:02→15:00:02, Expired | 5610, 09:15:00 | 30.0 min | **−345.0 min** |
| Puts-80-$4 | 5610, 09:15:00→09:26:40, Stopped | 5595, 09:45:01 | 30.0 min | 18.4 min |
| Puts-50-$4 | 5615, 12:25:02→15:00:02, Expired | 5625, 12:55:01 | 30.0 min | **−125.0 min** |

**Conclusion: entries follow a fixed clock-time grid per template (predominantly
15/30/45/60-minute steps, matching the fixed daily schedule found in item 1),
regardless of whether the prior same-strike-family position has already stopped
out or is still open.** The negative gaps above (next trade opening *hours before*
the prior one even closes, because the prior one ran to expiry) are the clearest
proof this is a time-scheduled ladder of new positions, not a "reload on stop-out"
mechanism. Only ~5% of pairs happen to land within a tight ±5-minute window of the
prior trade's close — and that rate is consistent with what a fixed 15/30-minute
grid would produce by chance when a stop-out itself occurs on that same grid,
not evidence of an explicit re-entry trigger.

---

## Summary

| Item | Finding |
|---|---|
| 1. Entry timing | Fixed clock-time schedule per template (not continuous/random); e.g. `-80-$4` fires ~08:45–11:00 grid, `-50-$4` fires ~12:15–13:45 grid |
| 2. "80"/"50" | NOT the short-leg delta (realized delta ~22–29% regardless of label). It's a stop-multiple/target-win-rate configuration label: "80" family → StopMultiple 3.3/2.4 (~77%/71% breakeven win rate); "50" family → 2.0/2.4/3.2 (~67–76%) |
| 3. "$2"/"$3"/"$4" | Target entry credit price per spread (`PriceOpen`), NOT strike width (widths actually range 5–85 points) |
| 4. Stop formula | `PriceStopTarget = floor(10×(PriceOpen + StopMultiple))/10` — exact match 99.98% (4,609/4,610 ShortREL rows) |
| 5. Scale-in pattern | Fixed time-of-day schedule per template, independent of whether the prior same-strike position already stopped out (only 4.8% of same-template overlapping pairs open within ±5 min of the prior's close; median gap to prior close is negative/wide, not tightly zero) |
