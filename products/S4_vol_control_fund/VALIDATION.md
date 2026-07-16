# S4 Vol-Control Fund — validation manifest

A pointer to the evidence behind this product's pinned defaults, and the data it needs.
Nothing here was re-derived for the product folder — the engine is the shared brain
(`strategies/spx_vol_control.py`) driven by the validated runner
(`backtester/s4_vol_control.py`), and these are the reports that runner produced.

## Evidence (the three validated reports)

All paths are relative to the repo root (`C:\TradingDesk`).

| Report | What it proves | Path |
|---|---|---|
| **Clean-mechanics results** | The 2-D TARGET_VOL x LEVERAGE_CAP sweep, the vol-targeting proof (realized vol lands on target), and the **SEC 5%-DRC sanity** (our 14.93/5.70/3.75% vs SEC published 14.74/5.68/3.55% — a near-bullseye, proves no bug). | `backtester/output/s4_vol_control_20260628.md` |
| **Net-of-costs** | Same sweep minus two real frictions (1bp/turnover txn + 50bp/yr borrow spread). Total drag 2–15 bp/yr; the 10%/1.5x cell loses ~5bp (7.51% → 7.45%). Gross conclusions survive net of costs. | `backtester/output/s4_vol_control_net_of_costs_20260628.md` |
| **Re-entry-lag analysis** | Quantifies the V-bottom miss per crash: ~79pp summed over GFC/2018/COVID/2022 at 10%/1.5x, **dominated by COVID (~46pp)**. Verdict: a modest, structural, episode-specific toll — NOT worth a bespoke faster-re-entry fix (overfit trap). | `backtester/output/s4_reentry_lag_20260628.md` |

Regenerate any of them from the repo root with the validated runner:

```
C:/TradingDesk-Local/venv/Scripts/python.exe backtester/s4_vol_control.py --sweep --report
```

(The `--report` run writes the clean-mechanics + net-of-costs markdown to
`backtester/output/`. The re-entry-lag report comes from `backtester/s4_reentry_analysis.py`.)

## The pinned defaults and the exact report lines behind them

| Param | Value | Report line it traces to |
|---|---|---|
| target_vol | **0.10** | `s4_vol_control_20260628.md`, sweep row `10% / 1.50x`: CAGR 7.51% TR, realized vol 9.86%, max DD -20.94%, 2008 -12.74%, Sharpe 0.65. |
| leverage_cap | **1.50** | Same row. 1.5x = the live-retail FIA/RILA standard ("10% target at 150% cap"). |
| estimator | **simple** (max 20d/60d) | Report header: `estimator: simple max(20d, 60d)`. The required S4 default. |
| cash_ticker | **BIL** | Report header: `cash/RF series: BIL`. Covers 2007-05-30+. |
| net-of-cost defaults | 1bp txn / 50bp borrow | `s4_vol_control_net_of_costs_20260628.md`: 10%/1.5x → 5.3 bp/yr total drag → 7.45% net CAGR. |
| conservative profile | target_vol **0.05** | `s4_vol_control_20260628.md`, sweep row `5% / 1.50x`: CAGR 4.56% TR, realized vol 4.94%, max DD -9.51%, 2008 -5.71%. Also the SEC-anchor cell. |

## Data this product needs (read-only)

Local, off-Drive, already on disk: `C:\TradingDesk-Local\bt_data`.

| File | Role | Coverage |
|---|---|---|
| `SPY.parquet` | the single risk asset (SPX total-return proxy; adjusted close = dividends in) | 2007-01-03+ |
| `BIL.parquet` | cash / risk-free leg (1-3mo T-bill ETF, total-return proxy) | 2007-05-30+ |
| `SGOV.parquet` | alternative cash leg (only if you switch `cash_ticker`) | 2020+ |

No options data, no new pull — the whole product runs off these daily-price parquets.
The sim window begins where SPY and BIL overlap (2007-06-28 after the 60d warm-up).

## Known caveats (carried from the reports — keep honest)

- **It cannot beat SPX on raw CAGR.** Holding vol below SPX's ~16–19% structurally caps
  bull-market upside (the SEC anchor's 14.74% → 5.68% give-up is exactly this). Higher
  target_vol/cap recover CAGR at the cost of the smoothness that is the point.
- **It cannot catch the V-bottom / dodge gaps.** Daily rebalance de-risks after vol
  spikes and re-risks slowly — the industry-unsolved re-entry lag (~79pp summed, COVID
  dominant). This is the price of the crash protection, not a free fix. S5 is the
  separate strategy that structurally closes this gap.
- **Clean-mechanics assumptions:** SPY adj-close as an SPX-TR proxy; BIL carries a minor
  "stale identical prices" QC flag (immaterial to annual figures); the net-of-cost layer
  is a simple flat per-turnover bp + linear borrow spread, no tiered margin/slippage/tax.
- **Strict causality** is preserved in the runner: exposure decided from vol through day
  T's close earns day T+1's return (one-day shift). No look-ahead.
