# British IC — 2026 Execution Profile

Scope: 2026-01-01 through 2026-07-07 (last date with real fills in this dataset), 121 trading days, 1830 real trade entries (1478 from combo_ledger.csv + 352 from fully_unmatched_short_lifecycles.csv, confirmed disjoint at the exact (TradeDate, conid, open_dt) level -- no double-counting).

Built fresh by `bic_2026_execution_profile.py`. Prior work in this folder (STRATEGY_MECHANICS.md, template_delta_stats.csv) covered the FULL 2024-09-16..2026-07-07 window, never 2026 alone, and never computed trade-frequency-per-day or account-value/margin sizing at all -- this is new.

**Timezone note:** all clock-time figures below are CT. combo_ledger.csv's raw IBKR timestamps are US/Eastern (confirmed 2026-07-13: raw range 09:07-15:51 fits ET's cash session, not CT's; settlement/expiry closes cluster at a uniform 16:20:00 raw, which only fits as 20 min after the 4:00 PM ET close; cross-checked against template_join.py's zero-offset join to the raw TAT/NinjaTrader log) -- converted here via a -1 hour ET->CT shift before any time-of-day analysis. An earlier version of this report (committed 011d59a) used the raw ET timestamps unconverted while labeling them CT; every clock-time figure in that version was off by exactly 1 hour. This version supersedes it.

## 1. Trade frequency

- Entries/day: **mean 15.12, median 16, min 2, max 26** (n=121 trading days)
- Percentiles: p10=9, p25=12, p75=18, p90=21

| Time bucket (15-min, CT) | Count | % of all entries |
|---|---|---|
| 08:00 | 2 | 0.1% |
| 08:30 | 198 | 10.8% |
| 08:45 | 164 | 9.0% |
| 09:00 | 160 | 8.7% |
| 09:15 | 154 | 8.4% |
| 09:30 | 8 | 0.4% |
| 09:45 | 23 | 1.3% |
| 10:00 | 19 | 1.0% |
| 10:15 | 42 | 2.3% |
| 10:30 | 24 | 1.3% |
| 10:45 | 25 | 1.4% |
| 11:00 | 22 | 1.2% |
| 11:15 | 30 | 1.6% |
| 11:30 | 27 | 1.5% |
| 11:45 | 7 | 0.4% |
| 12:00 | 199 | 10.9% |
| 12:15 | 143 | 7.8% |
| 12:30 | 129 | 7.0% |
| 12:45 | 125 | 6.8% |
| 13:00 | 135 | 7.4% |
| 13:15 | 74 | 4.0% |
| 13:30 | 73 | 4.0% |
| 13:45 | 25 | 1.4% |
| 14:00 | 18 | 1.0% |
| 14:15 | 1 | 0.1% |
| 14:30 | 2 | 0.1% |
| 14:45 | 1 | 0.1% |

Secondary figure (not primary, timestamp-incomplete): total real short open-BATCH events (scale-ins counted separately) = 2609 vs 1830 lifecycles. combo_ledger.csv only retains a captured timestamp for batches that found a paired long; ~779 additional scale-in batches exist but aren't individually timestamped in this ledger, so the entries-per-day figures above are at the lifecycle level (one real, fully-captured timestamp each), a conservative/complete count of distinct positions opened, not of every individual scale-in fill.

### First entry of the day -- checking the literal "9:00 AM" premise

- Modal first-entry clock time across all 121 2026 trading days: **08:43 CT** (84/121 = 69.4% of days)
- 106/121 days have their first entry before 09:15 CT.
- **Corrected verdict (post ET->CT fix): close enough to be broadly consistent with a literal "9:00 AM" premise** -- the day's first entry clusters at ~08:43 CT, 17 minutes before 09:00. (Prior committed version of this report, before the timezone fix, reported the raw ET clock reading of 09:43 mislabeled as CT and concluded a literal "9:00 AM" premise did not hold; with the fix applied, that conclusion no longer holds -- the real CT clock slot is close to the literal 9:00 AM premise.)

## 2. Position sizing per entry

- Entries with known width+credit (combo_ledger subset): 1478/1830 (80.8%). fully_unmatched_short entries (352 rows) have no identified paired long in this reconstruction, so notional/width is not computable for them -- excluded from sizing stats, not zero-filled.
- Notional max-loss ($, = width_pts x 100 x qty - credit x 100 x qty): mean **$22,513**, median **$15,864**, range $7,198-$154,090
- % of account value (account value = PRIOR trading day's close, not same-day EOD, to avoid look-ahead into that day's own P&L): mean **10.588%**, median **7.423%**, p90 18.806%, max 70.376%

### Representative first-of-day entry example (modal clock time 08:43)

- Date: 20260108, real fill time: 2026-01-08 08:43:27
- CallSpread, short strike 6925, width 75 pts
- Contracts: 2 (real IBKR-confirmed)
- Credit received: $4.72/spread
- Notional max-loss: $14,056
- Account value at entry (prior close, validated_balance_validation_csv): $205,421.85
- % of account value: **6.843%**
- TAT match: MATCHED, Template: British IC - Calls - 80 - $4 , BuyingPower: $14,070 (6.849% of account value)

### Account value construction & validation

- balance_validation.csv (real IBKR balance) covers 2025-07-09..20260219 only. For 2026-02-20..2026-07-07, account value is **implied** from real P&L (REFERENCE_BALANCE + cum_pnl_actual), anchored to the last real validated balance (2026-02-19), never independently balance-validated. Labeled `implied_from_real_pnl_anchored_2026-02-19` throughout output.
- Verified: mean daily reconstruction mismatch (balance_delta vs reconstructed real P&L) = **$32.07/day** (matches the documented ~$32/day figure).
- IMPORTANT finding: this mismatch is **not zero-mean** (signed mean $+27.34/day), so it compounds. The naive REFERENCE_BALANCE + cum_pnl_actual formula alone drifts to ~$1,222 away from the real validated balance by 20260219. This report does NOT use the naive formula for the uncovered tail -- it re-anchors to the last real validated balance (2026-02-19: $220,712.35) and carries forward only the incremental real P&L from that date, which avoids re-carrying the accumulated ~$4.2k drift, but the possibility of further undetected drift accumulating between 2026-02-20 and 2026-07-07 (a period with NO real balance data to check against) cannot be ruled out and is explicitly flagged, not smoothed over.

## 3. Wing width

- All 2026 (n=1478, real strikes, no TAT dependency): mean **65.0 pts**, median **75.0 pts**, std 15.7, range 30-85
- 352 fully_unmatched_short entries have no identifiable paired long, so width is unknown for those specific trades in this data.

| Template label (TAT, Jan1-Mar19 2026 only) | n | mean width (pts) | median width (pts) |
|---|---|---|---|
| 50 | 279 | 48.0 | 45.0 |
| 80 | 289 | 78.3 | 75.0 |

No width LABEL (80 vs 50 template name) available for 881 combo_ledger rows dated after 2026-03-19 -- but the raw point-width itself IS directly computable from real strikes for the whole period (used above); only the template NAME requires TAT coverage.

## 4. Delta targeted (TAT-covered 2026 period ONLY: 2026-01-01..2026-03-19)

- Puts (n=321): mean |delta| **0.2022**, median 0.1942, std 0.0532
- Calls (n=247): mean |delta| **0.2124**, median 0.1978, std 0.0575

| ComboType | Template | n | mean \|delta\| | median \|delta\| |
|---|---|---|---|---|
| CallSpread | 50 | 124 | 0.2146 | 0.2126 |
| CallSpread | 80 | 123 | 0.2102 | 0.1913 |
| PutSpread | 50 | 155 | 0.2053 | 0.2025 |
| PutSpread | 80 | 166 | 0.1994 | 0.1896 |

**881 combo_ledger rows (2026-03-20..2026-07-07) have NO ground-truth delta in this dataset** -- roughly half of the 2026 window. Not estimated or extrapolated from the Jan-Mar figures above.

## Margin / BuyingPower finding

- TAT join match rate within TAT coverage: 568/597 (95.1%).
- Verified: real IBKR qty matches TAT's own Qty column only 71.7% of the time on clean single-batch matches (n=568); when they differ, real qty is typically HIGHER (TAT undercounts real fills, consistent with documented history in RECONSTRUCTION_NOTES.md).
- **Key finding: TAT's `BuyingPower` is an EXACT deterministic formula** (width x 100 x Qty - PriceOpen x 100 x Qty) on TAT's own Qty -- confirmed to a ratio of 1.000000 across the full TAT dataset (n=4,687), not an independent broker margin/SPAN figure. It is mathematically identical to defined-risk notional max-loss. Because of this, **'% of margin used' and '% of account value from notional max-loss' collapse to the same metric here** -- there is no independent margin-capacity denominator in this data, per the task brief's own guidance not to invent one. Reported once (Item 2 above), not double-counted.
