# Trading Engine — Handoff / Build Spec

**Purpose of this file.** A self-contained brief so a fresh Claude Code session can build the
live/paper execution engine without re-deriving context. Read top to bottom before writing code.

---

## 0. ONE-LINE GOAL

Build a LIVE/PAPER execution engine that runs the **same strategy logic we backtest**, against
live IBKR data, placing orders in the IBKR **paper account first**, with a guarded, human-gated
path to a live account later. The backtester and the live engine must share ONE strategy codebase —
**what we test is exactly what we trade.**

## 1. HARD SAFETY CONSTRAINTS (read first — non-negotiable)

- **Paper account only** until explicitly promoted. Connect to IB Gateway **paper port 4002**.
- **Dry-run by default.** The engine LOGS intended orders and does NOT transmit until a human
  explicitly "arms" the session. Arming is per-session and manual.
- **NEVER place live-account orders without explicit, in-session human confirmation.** The assistant
  must not auto-arm live trading. Live = port **4001** + live account + stricter guards + a human
  flipping the switch each session. There is no automatic paper→live promotion.
- **Risk guards required before ANY order transmission:** max position size, max # contracts/legs,
  max daily loss, per-trade max loss, buying-power/cash-reserve check, a **kill switch**, and
  **position reconciliation** (intended vs actual broker positions).
- **Idempotency:** no duplicate orders on reconnect or restart. Every order carries a client order id.
- The execution engine is the highest-stakes code in the whole program. Build it slow, paper-first,
  dry-run-first, guarded. Bias to halting on any ambiguity rather than transmitting.

## 2. WHAT ALREADY EXISTS TO REUSE (don't rebuild)

- **IBKR connection pattern:** `ib_async`, IB Gateway `127.0.0.1:4002`. For DATA we connect
  `readonly=True`; for TRADING use a SEPARATE non-readonly connection with its own `clientId`
  (RRG uses 1; data tests use 21–24; give the trader e.g. 30). See
  `Market_Data\options_warehouse\ibkr_*.py` and `Market_Data\rrg_poller.py`.
- **Gateway setup / gotchas:** `Market_Data\options_warehouse\IBKR_SETUP.md` (auto-restart, API
  settings, live-data entitlement is confirmed working on the paper connection).
- **Market data + features:** `Market_Data\options_warehouse\` — GEX engine (`features/gex.py`),
  parquet storage, the live collector (being built), warehouse at `C:\MarketData`.
- **Backtester + strategy logic:** `backtester\` (Adaptive All-Weather Core, validated). Strategy
  specs: `Downloads\STRATEGY_2_IRON_CONDOR_INCOME.md`, `Downloads\STRATEGY_3_*` (Swiss condor),
  the MSR gamma/vol handoff, and `options_warehouse\STRATEGIES.md` (the roster S0–S3).
- **venv:** `C:\TradingDesk-Local\venv` (has `ib_async`, pandas, pyarrow, duckdb).
- **Secrets:** `C:\Users\andre\backtester\.env` (outside Drive; never printed). IBKR auth is the
  Gateway login, not a key in `.env`.

## 3. THE CORE DESIGN PRINCIPLE — one strategy codebase, two runners

A **Strategy** is a pure decision function: given (market state/features, current positions, clock)
→ target orders / target positions. The same function is driven by:
- the **backtester** (historical bars, simulated fills), and
- the **live engine** (live IBKR data, real paper/▲live order routing).

Same code, both places. This is what prevents backtest↔live divergence — the classic killer. The
first deliverable is a clean `Strategy` interface plus refactoring ONE already-backtested strategy
onto it, proving the shared-codebase principle.

## 4. ARCHITECTURE (components)

- **StrategyBase** (interface): `warmup()`, `on_data(state) -> intents`, `on_fill(fill)`, `params`.
- **MarketState provider:** live (IBKR stream / the collector) or historical (backtester) — same shape.
- **ExecutionEngine:** connect (paper), pull account + positions, call the strategy, translate intents
  → IBKR orders (incl. 4-leg combos/BAG for condors), manage order lifecycle (submit/fill/cancel/
  retry), reconcile against the broker.
- **RiskManager:** pre-trade checks (size, count, daily loss, buying power, the cash-settlement
  reserve rule from Strategy 3 §5), plus the kill switch. Vetoes before transmission.
- **OrderRouter:** IBKR order construction — combos for condors, limit vs market, TIF, etc.
- **Ledger/Logger:** every intent, order, fill, reconciliation → a DB + log. The audit trail.
- **Runner/Scheduler:** start session, manual arm, run through market hours, flatten/park at close
  per the strategy's rules.

## 5. BUILD ORDER

1. `StrategyBase` interface + refactor ONE simple backtested strategy onto it (no IBKR needed).
2. ExecutionEngine skeleton: connect to paper, fetch account/positions, generate intents in
   **DRY-RUN (log only)**. Validate during market hours.
3. RiskManager + guards + kill switch (BEFORE any real order can transmit).
4. OrderRouter: paper order submission (armed), simplest strategy/instrument first; validate fills
   + reconciliation live in paper.
5. Wire the condor / covered-call (S2/S3) execution once their backtests exist.
6. Monitoring, logging, daily report, reconnect handling.
7. Paper→live checklist — human-gated, never automatic.

## 6. OPEN QUESTIONS FOR THE USER (answer at session start)

- **Which strategy goes to live-paper first?** Simplest/safest = the Adaptive All-Weather monthly
  rebalance (low frequency, low risk, already validated). A condor is higher-touch.
- **Paper account number** to target (there are several DU* sub-accounts on this Gateway).
- **Starting risk limits** for paper: max position size, max daily loss, max contracts/legs.
- **Order style:** limit vs market default; fill aggressiveness.
- **Eventually a dedicated machine/VM** for 24/5 running, or keep it on this PC?

## 7. HONEST CONSTRAINTS

- This is an **interactive** build — it needs the user for design + safety decisions, not a
  fire-and-forget job.
- **Live validation needs market hours** (paper order routing, fills, reconciliation).
- Paper-first, dry-run-first, guarded, human-gated to live. Slow is correct here.

---

*Start by confirming §6, then build §5 step 1 (the shared Strategy interface). Keep §1 sacred.*
