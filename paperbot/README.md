# Trading Engine — PAPER account only

This engine targets the **IBKR paper account only** (paper login, paper port **4002**,
account **DU…141**). There is no real-money configuration in this project. The word
"live" is not used here — real-money accounts are explicitly out of scope.

Spec / build plan: `Market_Data\trading_engine\HANDOFF.md` (in Drive). This folder
(`C:\Users\andre\trading_engine`, outside Drive) holds the runnable engine and, later,
its order ledger — kept off Drive so sync can never corrupt the running state.

## Environment
Runs on the trading venv that has `ib_async` **and** the shared strategy brain:
`C:\TradingDesk-Local\venv` (has `ib_async 2.1.0`, pandas, and `strategy_core`
installed editable). The decision logic is imported from `strategy_core` so the paper
engine runs the EXACT code the backtester validated.

## Safety posture (current)
- Connection is **read-only** (`config.READONLY = True`) — physically cannot transmit orders.
- **Dry-run** (`config.DRY_RUN = True`) — even on a non-read-only session later, orders are
  only logged until a human arms the session. No auto-arm.
- Engine refuses to act on any account not ending in `config.ACCOUNT_SUFFIX` ("141").

## Status log

### 2026-06-26 (cont.) — Gateway launch fixed, first contact, dry-run ExecutionEngine
- ✅ **Paper Gateway now auto-launches.** Fixed an IBC 3.24.0 / Gateway-1045 JRE-probe bug by
  pre-seeding `java_version=17` in the launch env (baked into `connections/ibkr_paper.py`; same one-line
  fix applied to `dailyreport/daily_run.py`). Port 4002 comes up in ~15s. No IBKR script was edited.
- ✅ **First contact (read-only):** connected to **DF8922141** — an **FA paper *master***, which is
  our MAIN account — NetLiq $31,808.96, flat. Corrected the account model: 141 is a `DF` master, and
  `DU…142-146` are paper sub-accounts reserved for later. Fixed `check_account.py` (accept DU **and**
  DF; ASCII-only output so the cp1252 console can't mangle it).
- ✅ `strategy_target.py` — gets the strategy's CURRENT target book by running the validated
  backtester through today and taking its latest rebalance (preserves the prev_weights chain, so
  paper == backtest). Current target (Balanced, as of 2026-06-01): SPY/VTI/RSP 26.67% each, PDBC 15%,
  TFLO 5%.
- ✅ `execution_engine.py` — **DRY-RUN skeleton**: compute target → connect read-only → read NAV +
  positions → diff vs target → LOG intended limit orders. Verified: 5 BUYs (~$31.9k notional) against
  the flat $31.8k account. Transmits nothing. Enforces READONLY+DRY_RUN and the DU/DF paper guard.
- ✅ `risk_manager.py` — **RiskManager + kill switch**. Guards: daily-loss **kill switch** (−2%,
  trips + **persists** to `C:\TradingDesk-Local\state\paperbot\killswitch.json`, survives restart,
  manual-clear only — verified firing); **cash-reserve / no-leverage**; **per-position cap** (35% on
  risk assets, cash-equivalents exempt — the old 5% would have vetoed the strategy itself); **max
  legs**; order sanity. Positions are now sized vs **investable = NAV×(1−1.5%)** so the reserve holds by
  construction and the book never levers.
- ✅ `order_router.py` — **OrderRouter**: builds the exact IBKR LIMIT orders, **qualifies contracts**
  (real conIds returned), deterministic `orderRef` per (account, as_of, side, symbol) for
  **idempotency**. `transmit=False` always; a `transmit_guard` fails CLOSED (DRY_RUN / READONLY /
  not-armed all block). Verified: 5 orders built, **0 transmitted**.
- ✅ Full pipeline runs clean end-to-end against DF8922141: target → connect → diff → risk (5/5
  approved) → route (blocked, nothing sent).
- ✅ `live_quotes.py` — **live IBKR quotes** (read-only) for sizing + limit prices. Validated during
  market hours: all 5 universe symbols returned **live data (mdType=1)**; orders now price off real
  bid/ask/last (per `ORDER_STYLE`), with a per-symbol fallback to the strategy-data close.
- ✅ `ledger.py` — **audit trail**: every engine run appended to `…\state\paperbot\runs.jsonl`
  (full detail) + `paperbot.log` (one human line). Wired into the engine.
- ✅ `reconcile.py` — read-only **reconciliation** (MATCHED / DRIFTED / MISSING / UNTRACKED), importable
  + runnable standalone. Verified: flat account → all 5 MISSING, "book not aligned."
- ⏭️ Next (needs the market open AND a human arm): place an actual paper order to validate **fills +
  partial-fill handling + post-fill reconciliation**. Arming = turn the gateway `ReadOnlyApi` off
  (restart) + set `READONLY=False` + `DRY_RUN=False` + pass `armed=True`. Until all of that, the
  OrderRouter's `transmit_guard` fails closed and nothing sends.
- ⏭️ Housekeeping: refresh Tiingo price data (currently through 2026-06-24) before any real paper run.

### 2026-06-26 — Engine home created + read-only first-contact script
- ✅ `config.py` — connection (paper 4002, clientId 30), account suffix guard ("141"),
  read-only + dry-run flags, starting risk-limit defaults (not yet enforced), strategy selection.
- ✅ `check_account.py` — READ-ONLY first contact: connect, confirm account ends in 141 and is a
  DU paper account, print balances + positions, disconnect. Transmits nothing.
- ⏭️ Next: run `check_account.py` once the paper Gateway is up (confirms plumbing + account).
- ⏭️ Then: ExecutionEngine skeleton — compute target weights from `strategy_core`, diff vs paper
  positions, **log** intended orders (dry-run). No transmission until RiskManager + kill switch exist.

## How to run the account check
Start IB Gateway, log into the PAPER account, enable the API on port 4002, then:

    C:\TradingDesk-Local\venv\Scripts\python.exe C:\Users\andre\trading_engine\check_account.py

Success looks like: it prints the account number ending in 141, marks it `PAPER ✓`, and lists
your paper cash and positions — then says nothing was transmitted.
