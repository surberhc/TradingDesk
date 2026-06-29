# S4 Vol-Control Fund — deploy runbook (PAPER)

Honest status: **the strategy is built, validated, and produces a correct target book
today. It is NOT yet wired to place paper orders.** This runbook states what is ready,
what feed and account model it would use, and the exact gaps that remain before it could
trade the paper account.

PAPER / research scope only. There is no real-money path anywhere in this project. Paper
login, paper account, paper port 4002. Never call any of this "live."

## What runs today (ready)

```
C:/TradingDesk-Local/venv/Scripts/python.exe products/S4_vol_control_fund/run_s4.py
C:/TradingDesk-Local/venv/Scripts/python.exe products/S4_vol_control_fund/run_s4.py --profile conservative
C:/TradingDesk-Local/venv/Scripts/python.exe products/S4_vol_control_fund/run_s4.py --backtest
```

- The first two print **today's target book** — the `{SPY: exposure, BIL: 1-exposure}`
  weights the fund wants to hold right now (a negative BIL weight = borrow). This is the
  deploy-shaped output an execution layer would diff against the account.
- `--backtest` delegates a full TR/ER sweep to the validated runner so paper == backtest.

## Data feed

- **Backtest / research:** local read-only parquet at `C:\TradingDesk-Local\bt_data`
  (`SPY.parquet`, `BIL.parquet`). See VALIDATION.md.
- **Daily deploy:** the fund rebalances **daily** off the SPY close + the realized-vol
  estimator. To run it forward each day you need the latest SPY adjusted close appended
  to the price store — the same Tiingo daily-price pipeline the rest of TradingDesk uses
  (`datacollector` / the existing forward-data jobs feed `bt_data`). No options, no
  intraday, no new subscription. The fund decides at the close and would execute the next
  session (T+1), matching the backtest's causal convention.

## Account model (PAPER)

- IBKR **paper** gateway, port **4002**, via the shared `connections` package (host/port/
  clientId are not duplicated in product code).
- The paperbot's existing multi-account (Option B) engine targets the paper CLIENT
  sub-accounts (`DU89221xx`), each funded ~$1.1M paper, under the FA paper master. S4
  would slot into that same execution layer — it is a single-account vol dial, so a
  per-account share computation against NAV is all it needs.
- Safety posture inherited from `paperbot/config.py`: `READONLY=True` and `DRY_RUN=True`
  until a human deliberately arms a session. No auto-arm.

## What's still required before it could trade paper (the honest gaps)

1. **Execution wiring.** `run_s4.py` computes the target book but touches no broker. To
   trade it, the target must be handed to the paperbot's `rebalance_engine` /
   `execution_engine` (the same path S0 uses). S0's `strategy_target.current_target()` is
   the template; S4 needs an equivalent adapter that returns its `{SPY, BIL}` weights in
   the paperbot's `Target` shape. **Not built.**

2. **Leverage / borrow handling.** At 10%/1.5x the fund can ask for exposure > 1.0x
   (a negative BIL weight = margin borrow). The current paperbot `RISK_LIMITS` assume no
   leverage (`cash_reserve_pct = 0.05`, positions sized against NAV*(1-reserve)). Deploying
   S4 as-is at cap > 1.0 requires either capping at 1.0x for the first paper cut or
   extending the risk manager to model the borrow leg. **Decision + work needed.**

3. **Daily-cadence rebalance loop.** The paperbot was built around S0's month-end
   cadence. S4 rebalances daily; confirm the execution scheduler runs the diff each
   trading day (and that daily turnover at ~1bp is acceptable on the paper book). The
   backtest already validated the daily cadence in the strategy; the paper *scheduler* is
   the open piece.

4. **Forward data freshness check.** A guard that refuses to trade on stale prices (the
   paperbot already has a `price_date` freshness check in `strategy_target.py` to reuse).

5. **Profile / dial decision.** Pick the deploy cell: `balanced` (10%/1.5x) as the
   default product, or `conservative` (5%/1.5x) for a bond-alternative role. Pinned in
   `config.py`; flipping it is a one-line change.

Until items 1–4 are done, S4 is a **research-validated, target-book-producing** product —
ready to *show* and ready to *wire*, not yet armed to trade. That is by design: arming is
a separate, deliberate step (see memory: paper-arming-and-fills, paper-not-a-ming-vase).
