# CAN SLIM replica — disciplined-execution DECISION ENGINE (compute-only)

`canslim/execution_engine.py` turns the PROVEN winning ruleset into a deployable **daily
decision engine**. It is the forward-running sibling of `execution_backtest.py`: the backtest
showed that the `E3 + timing` config (cut losers at −7%, then let winners run behind a rising
50-day line, no profit cap, ~12% sizing, exposure gated by the weekly `invested_pct` dial)
returned ~2.7x his realized book at LOWER drawdown, on his own picks and sizing. This engine
applies that same frozen ruleset to TODAY's portfolio and prices.

## What it does
Given the current book, today's prices + a trailing close series per name, the day's candidate
picks, the exposure dial, and cash, `decide_day(...)` returns a ranked `DayPlan`:

- **EXITS** (first): a name closes at/below its −7% initial stop *before* the 50-line rule has
  engaged, **or** a decisive close below its 50-day SMA (`close < 0.98 × SMA50`) once the name
  is a confirmed winner. Each carries a `trigger` (`stop_7pct` / `decisive_50sma_break`).
- **HOLDS**: everything still onside — including winners with no profit cap (the cap was the
  single most destructive rule tested; it is deliberately absent).
- **ENTRIES**: confirmed breakouts within the +5% buy zone above their pivot, ranked by
  tightness to the pivot (least-extended first), sized ~12% of equity (18% hard cap), subject
  to the 7-name concurrent cap and the exposure dial. Each carries `target_dollars`,
  `entry_ref` (pivot) and `initial_stop` (−7% level). Excess is held as cash.

The **per-name state machine** (`evaluate_position`) is the SAME latch `simulate_exit()` proved
out: −7% stop active until the first close above a *rising* 50-SMA, after which the stop retires
(`sma_active` latches True and is carried across days on the `Position`) and only a decisive
50-line break exits. All thresholds (`HARD_STOP`, `SMA_BUFFER`, `EW_TARGET`, `EW_CAP`) and the
`sma()` function are IMPORTED from `execution_backtest.py`, so the engine and the validated
backtest **cannot drift**.

## Pluggable pick source
`decide_day` takes a `list[Pick]`. `picks_from_list(rows)` adapts a passed-in list (Doug's
watch list, or the detector's output — `base_detector.py` / `selection_backtest.py`) into
`Pick(symbol, pivot, last_px, breakout_confirmed)`. Swap the adapter without touching the
engine.

## No lookahead
A decision for day D uses only bars up to D. Per name the caller supplies closes ending on/before
D; the 50-SMA is built from that slice and no future bar is referenced. If `MarketData.dates`
is provided, a causality guard raises `NoLookaheadError` on any bar dated after the decision day.
The exposure dial must be the prior reading (caller's responsibility, same as the backtest).

## The seam to paperbot (the remaining GATED step)
The engine is **COMPUTE-ONLY**. It never submits, arms, or transmits an order; it does not
import paperbot or touch any order path (enforced by a test). The single hand-off point is
marked `# === PAPERBOT SEAM ===` on `DayPlan`:

- `plan.exits`   → close orders for the named positions.
- `plan.entries` → open orders sized to `target_dollars`, with `initial_stop` as the resting
  server-side protective stop.

Taking it live on IBKR **paper** is a separate, deliberate step behind the desk's sacred
**review → arm → transmit** gate, and requires (per memory `live-trading-order-resilience`)
that protective stops rest **server-side** first (the kill-the-gateway probe). Concretely the
gated step is: (1) feed a real `DayPlan` into the paperbot dynamic order router as a *proposal*;
(2) human review of the action list; (3) arm; (4) transmit on the PAPER account (DU…141, port
4002). None of that is wired here. No paperbot version bump — this is a compute-only module,
no order-affecting change.

## How to run
- Tests: `cd canslim` → `"C:\TradingDesk-Local\venv\Scripts\python.exe" -m pytest tests/test_execution_engine.py -q` (18 passing).
- Dry run over the last ~4 months of cached data:
  `cd canslim` → `"C:\TradingDesk-Local\venv\Scripts\python.exe" execution_engine.py`
  Seeds a small book from real cached names, feeds each trading day's real closes, and prints
  the daily action list (entries/exits/holds + a cash/exposure line). It uses the same
  scratchpad `fwd_cache` the backtest uses; it is a sanity demonstration, not a backtest.
