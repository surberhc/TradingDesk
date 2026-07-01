# canslim — APS / CAN SLIM strategy evaluation

## Purpose
Evaluate an outside advisor's ("APS") **real-money IBD / CAN SLIM (William O'Neil)
growth-stock strategy** as a candidate to add to the TradingDesk portfolio. This is a
research/diligence project: understand the strategy, measure whether its edge is real and
robust (not curve-fit), and determine what it would cost and take to run it ourselves.

## Data inventory
Precise coverage of the source spreadsheets in `source/` (copies — originals remain at the
Drive root).

### Weekly plan / review files
Timing, allocation, current positions, watch lists, and market commentary. **These do NOT
contain per-trade P&L.**
- `APS - Weekly review and trading plan (29).xlsx` — coverage **Nov 2018 – Dec 2022**,
  210 weekly sheets.
- `APS - Weekly review and trading plan.xlsx` — coverage **Dec 2023 – Jun 2026**,
  ~130 sheets.
- **Gap:** ~all of **2023** is missing from the plan files (advisor reportedly did not
  trade during that period).

### Trade journal files
Systematic closed-trade ledger **with** entry/exit prices, %, hold time, and P&L.
- `APS Trading Journal - 2023.xlsx` — **2023 H2**, from 06-30-23 onward.
- `APS Trading Journal - 2024.xlsx` — 2024.
- `APS Trading Journal - 2025 (2).xlsx` — 2025.
- `APS Trading Journal - 2026.xlsx` — **2026 H1**.
- **Trade-level P&L exists only from mid-2023 onward.**

## Key findings so far
- **Textbook IBD / CAN SLIM.** Base breakouts at pivots; RS / EPS / IBD-50 / group ranks;
  10-week, 21-day, and 50-day moving averages; the 7–8% hard stop rule; a 20–25% profit-
  taking ladder; and IBD Market-Pulse allocation bands (0–100% invested).
- **The edge is real in trending years and breaks in choppy ones.**
  - 2024: 42% win rate, +33% avg win vs -9% avg loss.
  - 2025: 36% win rate, +38% vs -10%.
  - 2023 H2 and 2026 H1 (choppy): ~15–18% win rate; avg loss blows past the -7% rule to
    -13% / -15%, with tail losses to -41% / -72%.
  - **The leak is stop discipline** — discretion overriding the hard stop in choppy tapes.
- **Market-timing / cash-raising discipline is long-standing.** 100% cash in Q4 2018
  (+0.64% vs Nasdaq -8.26%) and at the end of 2022 (Nasdaq -33% on the year).

## Data-sourcing boundary
- **IBKR** (already paid for, API live) can supply **price history + scanner/ranking +
  execution** cheaply (~$0–10/mo, overnight batch).
- **IBKR CANNOT supply fundamentals.** `reqFundamentalData` was removed in TWS v10.47
  (May 2026), had shallow/patchy small-cap coverage, and never offered point-in-time
  history.
- Therefore a **fundamentals vendor with point-in-time + survivorship-free small/mid-cap
  coverage is required** for both the live screen and an honest backtest.
- See `TradingDesk\connections\IBKR_CAPABILITIES.md` and memory rule
  `ibkr-first-data-sourcing`.

## Open threads
- **Stop-loss counterfactual** (2023–2026) — DONE; see `research/stop_analysis_report.md`.
  Headline: a −7% hard stop would have made **less** money (−$36.8k vs discretion), driven
  entirely by 2025 (stop cut huge winners); −8% is ~break-even (+$8.7k). Trailing/vol-aware
  variants tested too (V1 breakeven+trail, V2 50-day-SMA trail, V3 2×/2.5×ATR): **no policy
  helps in all four periods** — every one is sunk by the 2025 melt-up. V2 (50-day SMA, most
  O'Neil-faithful) softens 2025 most but gives back crisis-year protection → different risk
  profile, not a strict win. Stop CHOICE is regime-dependent noise on this sample. Price paths
  RAW Tiingo; pre-entry SMA50/ATR20 pulled from the IBKR paper gateway (116/120). ERJ, PSTG
  uncoverable on both sources (excluded); ERJ-2024 flip is probable-but-unconfirmable.
- Potential **rule-based base/pivot detector**, validated against the advisor's own labeled
  watch-list picks.
- **Fundamentals-vendor cost estimates** — scope strictly to fundamentals +
  point-in-time only.
- **Extend the market-timing / allocation time series** back through 2018–2022 from the
  plan files.

## Layout & data conventions
- `source/` — small static reference spreadsheets (fine to keep in Drive / synced).
- `research/` — specs and analysis artifacts (see below).
- **Bulk downloaded price/fundamentals universe data lives off-Drive** at
  `C:\TradingDesk-Local\canslim\`, per desk convention (Drive sync corrupts bulk data).

### research/ contents
- `canslim_oneil_spec.md` — the O'Neil / CAN SLIM rule spec.
- `ibkr_api_capabilities.md` — IBKR API capabilities/limits for this project.
- `stop_analysis_report.md` — stop-loss counterfactual across all 120 closed trades,
  2023 H2–2026 H1. JOB 1: fixed −7%/−8% hard stop (per-year + overall dollar effect, rescue
  vs. bleeder lists). JOB 2: trailing/vol-aware variants (V1 breakeven+trail, V2 50-day-SMA
  trail, V3 2×/2.5×ATR) with a robustness/curve-fit verdict.
- `stop_analysis_trades.csv` — per-trade detail behind JOB 1 (entry, breach, hard-stop
  return, delta_$, classification) for all 120 trades.
- `trailing_trades.csv` — per-trade detail behind JOB 2: each policy's return, whether/when
  it stopped, and the price/indicator data source, for all 120 trades.
