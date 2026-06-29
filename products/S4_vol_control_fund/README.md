# S4 — SPX Volatility-Control Fund (standalone product)

A self-contained, deploy-ready packaging of the **S4 SPX Volatility-Control Fund**:
a single-risk-asset vol-targeting fund (an in-house FIA/RILA / S&P 500 Daily Risk
Control replica). PAPER / research scope only — never "live."

> **Shared-brain rule:** the strategy LOGIC is not in this folder. It lives once in the
> `strategies` package (`strategies/spx_vol_control.py`, class `SpxVolControl`) — the same
> code the backtester and paperbot import. This product folder **imports** that engine and
> pins the validated deploy dials around it. No logic is duplicated or forked.

## What S4 is

One risk asset (the S&P 500 via SPY total-return prices) plus a cash / T-bill leg
(BIL), scaled **daily** so that:

```
exposure_t = min( leverage_cap , target_vol / realized_vol_t )
```

- The residual `(1 - exposure_t)` sits in cash earning the risk-free rate; if exposure
  exceeds 1.0 the excess is borrowed at RF (a negative cash weight).
- `realized_vol_t` is the asymmetric **max(fast 20d, slow 60d)** estimator — de-risk
  fast (the short window spikes first in a selloff), re-risk slow (the long window is
  sticky). This is the FIA "headline steal."
- **No bonds, no regime engine, no diversification.** Vol-targeting IS the whole
  mechanism. Two dials: `target_vol` (the risk dial) and `leverage_cap` (the upside dial).

## Validated default parameters

The product ships with the live-retail **FIA/RILA standard cell** as the default, plus a
conservative bond-alternative profile. Every number traces to a validated report — see
`VALIDATION.md`.

| Profile | target_vol | leverage_cap | CAGR (TR, gross) | Realized vol | Max DD | 2008 | vs SPY B&H |
|---|---|---|---|---|---|---|---|
| **balanced** (default) | **10%** | **1.50x** | **7.51%** | 9.86% (on target) | **-20.94%** | -12.74% | SPY: 10.70% CAGR, 19.79% vol, **-55.20% DD** |
| conservative | 5% | 1.50x | 4.56% | 4.94% | -9.51% | -5.71% | (cap never binds at 5%) |

Net of realistic costs (1bp/turnover + 50bp/yr borrow), the balanced cell gives up only
~5 bp/yr (7.51% → 7.45%). Source rows: `backtester/output/s4_vol_control_20260628.md`
(`10% / 1.50x` and `5% / 1.50x`) and the net-of-costs companion report.

**Validation anchor:** against the published S&P 500 5% Daily Risk Control SEC supplement
(5yr → 2024-04), our build hits **14.93 / 5.70 / 3.75%** vs SEC's **14.74 / 5.68 / 3.55%**
(SPX-TR / DRC-5%-TR / DRC-5%-ER) — a near-bullseye that proves the engine is correct.

## Its role (and what it honestly cannot do)

S4 is a **conservative vol dial / bond-alternative** — an equity-sourced way to hold a
constant target volatility with a far smoother equity curve and a fraction of SPY's
drawdown (-21% vs -55%; -13% vs -37% in 2008). Sharpe and Sortino beat buy-and-hold SPY
across the entire parameter surface.

It is **not** an alpha engine. Two structural limits, stated plainly:

- **It cannot beat SPX on raw CAGR.** Holding vol below SPX's ~16–19% caps bull-market
  upside by construction. That give-up IS the smoothness — they are the same coin.
- **It cannot catch the V-bottom or dodge gaps.** Daily rebalancing de-risks after vol
  spikes and re-risks slowly, so it sells into weakness and rebuilds late — the
  industry-unsolved re-entry lag (~79pp summed across four crashes, COVID dominant; see
  `s4_reentry_lag_20260628.md`). This is a known, accepted, structural toll — not a bug
  and not worth a bespoke faster-re-entry fix (that's an overfit trap). The separate **S5**
  strategy is the one designed to close this gap.

## How to run

From the repo root, using the project venv (`C:\TradingDesk-Local\venv`):

```
# Today's target book (the {SPY, BIL} weights to hold now) — the default deploy cell:
C:/TradingDesk-Local/venv/Scripts/python.exe products/S4_vol_control_fund/run_s4.py

# The conservative bond-alternative profile:
C:/TradingDesk-Local/venv/Scripts/python.exe products/S4_vol_control_fund/run_s4.py --profile conservative

# A full historical TR/ER sweep (delegated to the validated backtester runner):
C:/TradingDesk-Local/venv/Scripts/python.exe products/S4_vol_control_fund/run_s4.py --backtest
```

`run_s4.py` computes target weights only — it touches no broker and places no order.

## Folder contents

| File | What it is |
|---|---|
| `README.md` | this file — what S4 is, the validated defaults, role, how to run |
| `config.py` | the pinned deploy dials + a `build_strategy()` factory that imports `SpxVolControl` from the shared package (no logic here) |
| `run_s4.py` | thin runnable entry point: prints today's target book; `--backtest` delegates to the validated runner |
| `VALIDATION.md` | manifest pointing to the three validated reports + the data the product needs |
| `DEPLOY.md` | PAPER runbook: data feed, account model, and the honest gaps before it could trade paper |

## Where the real artifacts live (not copied here, by design)

- **Engine (shared brain):** `strategies/spx_vol_control.py` — `SpxVolControl(StrategyBase)`
- **Validated runner + 2-D sweep:** `backtester/s4_vol_control.py`
- **Validation reports:** `backtester/output/s4_vol_control_20260628.md`,
  `s4_vol_control_net_of_costs_20260628.md`, `s4_reentry_lag_20260628.md`
- **Roster entry:** `datacollector/STRATEGIES.md` (S4 section)
