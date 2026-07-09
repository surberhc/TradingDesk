# S8 — British IC with mechanical long-leg exit ("B2" correction)

Status: **research/paper-analysis finding only — not live, not yet running anywhere.**

**Superseded as the canonical spec by `docs/S8_SPEC.md` (2026-07-09) — S8 is now developed/evaluated as
its own standalone strategy, not scored against this account's actual trade log going forward. This
file remains as the original research derivation.**

## What S8 is

S8 = the actual, externally-traded British IC strategy (SPX 0DTE credit spreads,
IBKR account U***9156) with **one mechanical correction** to the long (protective)
leg: **close the long leg the instant its paired short leg stops out — no profit
target, no timer, just close now.** Entries, templates, and the short-side stop
formula are unchanged from the strategy as actually traded (see
`STRATEGY_MECHANICS.md` for the entry/stop mechanics and `RECONSTRUCTION_NOTES.md`
for the execution-level reconstruction methodology). Only the long leg's *exit*
rule is different — today it's closed manually/discretionarily whenever someone
gets to it; under S8 it's closed automatically the moment the short stops.

## Validated results (data: 2025-07-09 to 2026-07-07, 236 trading dates, reconstructed from real IBKR execution-level fills, validated against the real account balance to ~$32/day average mismatch)

| | Actual (as-traded) | S8 (B2 rule) |
|---|---|---|
| Total P&L | +$42,765 | **+$138,982** (minor reconciliation variance $138,960–$138,982 across aggregation passes) |
| Return on $127,710 starting balance | +33.5% | **+108.8%** |

S8 beats the actual outcome on **~83–87% of individual long legs**, consistently
across a chronological train/test split (train: Jul 2025–Mar 2026, test: Mar–Jul
2026), and stays positive in every segment even after removing the single largest
contributing day in each segment — **not an artifact of one lucky day.**

Two individual days are unusually large single-day contributors on top of a
broader, real day-to-day tilt: 2025-10-10 (a market crash) and 2026-05-18 (a large
one-directional move). Excluding both, S8 still beats actual on 83% of the
remaining 58 days and adds +$87,583 over that period alone.

### Monthly return comparison (actual / S8, % of $127,710 starting balance)

| Month | Actual | S8 |
|---|---|---|
| Jul 2025 | +5.2% | +5.9% |
| Aug 2025 | +0.3% | +2.7% |
| Sep 2025 | +10.2% | +11.9% |
| Oct 2025 | +26.5% | +8.7% |
| Nov 2025 | -6.1% | -1.9% |
| Dec 2025 | +32.2% | +35.4% |
| Jan 2026 | -4.2% | -7.0% |
| Feb 2026 | +7.8% | +14.1% |
| Mar 2026 | +1.9% | +7.1% |
| Apr 2026 | -4.7% | +5.5% |
| May 2026 | -12.3% | +47.3% |
| Jun 2026 | -10.0% | -7.7% |
| Jul 2026 (partial) | -13.2% | -13.1% |

S8 wins 9 of 13 months. Oct 2025 and Jan 2026 are the clearest months where actual
beats S8, both traceable to the trade-off below.

## The honest trade-off

S8 gives up **100% of the long leg's upside past the moment the short stops** —
including most of whatever a rare huge move contributes after that point. The
2025-10-10 crash day is actually *lower* under S8 than what actually happened,
because the human's discretionary judgment that specific day beat the mechanical
rule. S8 converts the long leg from "occasional huge win" into "modest,
high-hit-rate risk reducer." **This is a deliberate variance-reduction trade, not
a free lunch** — it wins on hit-rate and total return across this window, but it
structurally caps the tail scenario the long leg exists to catch.

## Known limitations (carried forward, not to be dropped)

- Only ~1 year of data, with exactly one true crash-magnitude event (2025-10-10)
  — not enough regime coverage to fully bless this yet.
- Exit fills in this backtest are marked at 1-minute OHLC close, not real
  bid/ask — actual slippage on a fast automated exit is unmeasured and could
  erode some of the edge. Per this project's own measured finding, SPXW 0DTE
  exit-side slippage on the existing, non-automated exits already averages
  ~13x the quoted half-spread.
- No live implementation exists. This is a research/paper-analysis finding
  against reconstructed historical fills, not a running system.

## Cross-references

Full methodology and detail live in this folder:
- `RECONSTRUCTION_NOTES.md` — execution-level reconstruction, lifecycle/combo
  pairing, balance validation, exit-rule characterization.
- `STRATEGY_MECHANICS.md` — entry timing, template naming, stop formula
  derivation, scale-in/re-entry pattern.
- `STRATEGY_RECONSTRUCTION.md`, `EXIT_RULE_ANALYSIS.md` — full S8/B2 backtest
  detail and train/test split results.
- `longleg_rule_backtest_results.csv`, `longleg_rule_summary_by_split.csv`,
  `longleg_rule_summary_dollars.csv` — underlying per-leg and per-split numbers
  behind the totals above.

This document formalizes S8 as a named candidate strategy per the TradingDesk
naming convention (S0 = dailyreport regime, S4 = SPX vol-control fund, S5 =
financed convexity overlay, S6 = SPX cashflow 0DTE, S7 = income condor). It does
not repeat the full analysis in the files above — see those for derivation detail.
