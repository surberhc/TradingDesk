# CAN SLIM replica — MECHANICAL SELECTION-from-watchlist backtest

_Does a MACHINE picking entries mechanically out of the advisor's own watch-list pool — then managing them with the proven winning exit (E3) — make money? This is a SELECTION test: the machine's INDEPENDENT entry decision, distinct from prior tests that fixed selection to his picks (execution_backtest) or only checked agreement (detector_vs_outcomes)._

## What the machine does (deterministic, no lookahead)
- **Pool** = names on his weekly watch list (roles watchlist/added), 812 unique tickers, 2018-11 .. 2026-06.
- **Entry** = for each eligible name each week, detect an O'Neil base as-of that week (`base_detector.py`, bars <= that week only). If a VALID base + a breakout through the detector's pivot within the buy zone (<= 5% above pivot) occurs in the following 8 weeks, the machine BUYS at the pivot — regardless of whether he bought it.
- **Manage** = execution_backtest E3 (−7% catastrophic stop until first close above a RISING 50-day SMA, then hold and exit only on a decisive close below the 50-day; NO profit cap) + the weekly invested_pct exposure dial (prior-week only) + his-style sizing (~12% target, 18% cap, ≤7 concurrent), as a path-dependent portfolio, $650,000 start.

## Data coverage (partial-survivorship disclosure)
- Priceable watch-list names: **741/812** (91%). 71 names have no usable daily history — overwhelmingly tickers delisted/acquired/renamed in the 2018-2022 era (real partial-survivorship gap; these are DROPPED and counted, never fabricated).
- Full-span source: IBKR reqHistoricalData (read-only, clientId 42, whole-year durations) primary; Tiingo per-name fallback. Frame is RAW OHLC (chart price, matches his pivots).

## Headline result — machine selection from his pool (2019→2026, INCLUDES the 2022 bear)

| Portfolio | Span | Total ret | CAGR | Max DD | Win% | #trades |
|---|---|---:|---:|---:|---:|---:|
| **Machine + timing dial** | 2018-12-03..2026-07-01 | +85.7% | +8.5% | -19.5% | 44% | 231 |
| Machine, no timing dial | 2018-12-03..2026-07-01 | +324.5% | +21.0% | -16.0% | 44% | 354 |
| Naive: buy ALL watch-listed at his pivot | 2018-12-03..2026-07-01 | +239.8% | +17.5% | -12.7% | 47% | 170 |

_His realized book cannot be shown on this row: it only exists for 2023H2→2026 (120 trades). Comparison (a) to his book is in the per-year table below over the shared window._

## Per-year realized P&L (bucketed by EXIT year — regime behavior)

| Portfolio | 2019 | 2020 | 2021 | 2022 | 2023 | 2024 | 2025 | 2026H1 | total |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Machine+timing | $-10k | $+114k | $+148k | $+46k | $+0k | $+241k | $-168k | $+187k | $+557k |
| Machine no-timing | $+60k | $+200k | $+596k | $-114k | $+43k | $+646k | $-44k | $+723k | $+2109k |
| Naive buy-all | $+121k | $+80k | $+530k | $+9k | $+0k | $+418k | $-24k | $+425k | $+1558k |
| **His actual book** | — | — | — | — | $-37k | $+71k | $+148k | $-108k | $+74k |

_'—' = no data (his journal starts 2023H2). Machine/naive P&L per year is on the same $650,000 book._

## Per-year win rate + trade count (machine + timing)

| Year | #trades | Win% | P&L |
|---|---:|---:|---:|
| 2019 | 38 | 34% | $-10k |
| 2020 | 32 | 56% | $+114k |
| 2021 | 38 | 55% | $+148k |
| 2022 | 4 | 100% | $+46k |
| 2023 | 0 | — | $0k |
| 2024 | 38 | 50% | $+241k |
| 2025 | 48 | 27% | $-168k |
| 2026H1 | 33 | 42% | $+187k |

## Overlap with his ACTUAL buys (did the machine pick the same names?)
- Machine took **354 entries** across **275 names**.
- He actually bought **98 names** (role 'bought').
- Name overlap: the machine independently bought **33** of the names he also bought (33/98 of his buys caught by name). Timing overlap (machine entry within ±30 days of one of his buys of that name): **11** entries.
- Pure divergence: **242** names the machine bought that he NEVER bought — i.e. the machine's own selection, not a copy of his book.

## Verdict

- **Does mechanically picking from his watch pool + the winning exit make money?** **Yes, on the full span.** Over 2018-12-03..2026-07-01 (which INCLUDES the 2022 bear), the machine returned +85.7% total / +8.5% CAGR at -19.5% max DD (with the exposure dial ON), or +324.5% total / +21.0% CAGR at -16.0% DD with the dial OFF. Win rate 44%. Positive both ways — but the WHY matters more than the headline (below).
- **2022 bear — the key stress test, read HONESTLY on the dial-OFF book:** with exposure UNMANAGED the machine kept buying breakouts into the downtrend and got chopped: 73 trades, 37% win, **P&L $-114k** — a real, material LOSS. Mechanical SELECTION alone does NOT survive the bear; the −7% stops fire but breakouts keep failing. What rescues 2022 is the EXPOSURE DIAL: turning it on (prior-week info only) cut 2022 to just 4 trades / $+46k by pulling gross exposure toward zero through the downtrend — and it kept the machine essentially FLAT through 2023 (0 exits) rather than fighting the chop. So the bear verdict is: selection is a bull-tape edge; the timing overlay is what makes it survivable, not the stock-picking.
- **2026 H1 (recent hard tape):** 33 trades, P&L $+187k (dial on) / $+723k (dial off) — a POSITIVE non-bull half, unlike his own 2026H1 book (see per-year table).
- **2025 was the machine's worst modern year** (dial-on $-168k, 27% win): a whipsaw tape where the let-winners-run rule gave back gains before the 50-day break confirmed. The dial did not help here (its signal lagged the intra-year chop). Disclosed, not hidden — a genuine weakness of the mechanical stack in choppy sideways years.
- **vs the naive buy-everything baseline (dial-OFF, apples-to-apples on selection):** the base-detector machine returned +324.5% vs naive +239.8% total. The detector ADDS value over taking every flagged name at its stated pivot — but note the naive baseline only takes 170 trades vs the machine's 354 (naive requires a RECORDED pivot, which the survivorship-thinned early years mostly lack), so the two are not cleanly comparable in the pre-2023 window. Both clear his book on the shared years.
- **vs his actual book (shared 2023H2→2026 window):** his realized total P&L was $74k (+1.2% on invested, 33% win, -13.8% realized DD). See the per-year table for the head-to-head on the overlapping years.

### Hard limits (curve-fit + honesty guards, rule #1)
- **Pre-filtered pool.** The candidate universe is ALREADY his discretionary watch list, so this tests mechanical ENTRY-TIMING/SELECTION WITHIN his pool — NOT a full-market scan. A real deployable edge would need the same detector run over the whole market.
- **Partial survivorship.** 71 watch-list names (mostly 2018-2022 delistings/acquisitions) have no price data and are DROPPED. Their absence biases the surviving-name result upward to an unknown degree — a real, disclosed limit.
- **Small per-year samples** in the thin years (2019-2021); read those cells as directional.
- **No parameter tuning.** Detector bounds are the O'Neil spec; the exit is the already-proven E3; sizing/exposure/stop are his revealed behavior / O'Neil's playbook. The 8-week eligibility window and 5% buy zone are structural (O'Neil buy-point mechanics), not fit to the result.
- **No lookahead**, enforced: base from bars ≤ decision week; breakout only from bars strictly after base confirmation; exit judged on bars ≤ decision day; exposure dial reads prior-week only.
