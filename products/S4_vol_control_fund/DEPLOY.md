# S4 Vol-Control Fund — deploy runbook (PAPER)

Honest status: **SHELF-READY.** The strategy is built, validated, produces a correct target
book today, AND the single-account paper-deployment wiring is now built and tested — a
review-only path that connects read-only, sizes the levered {SPY, BIL} book, runs a margin
preflight + risk guard, and builds orders it transmits nothing. What remains before it
trades is entirely operational: pick the account + profile, confirm the account is a margin
paper account, schedule the daily driver, and build/arm the future executor. None of that is
code you write — it is the deliberate deploy checklist below.

This runbook states what is ready, the feed and account model, and the exact deploy steps.

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

## What is now built (the paper wiring)

All of this lives in `paperbot/` as **S4-specific siblings** — the frozen S0 execution
paths (`strategy_target.py`, `investable.py`, `reconcile.py`, `risk_manager.py`,
`rebalance_engine.py`) are **untouched**:

- `paperbot/s4_strategy_target.py` — adapter that runs the shared brain
  (`strategies.spx_vol_control.SpxVolControl`) with the S4 product config and packs the
  `{SPY, BIL}` weights into the existing `Target` dataclass. **Zero exposure math of its
  own** (proven bit-for-bit by the parity test). Profile is a runtime dial (balanced /
  conservative / explicit `target_vol`+`leverage_cap` overrides; defaults to conservative,
  the un-levered cell). Includes a **stale-data guard** (fails closed if prices are older
  than the last completed trading session).
- `paperbot/s4_sizing.py` — **real-margin leverage sizing**: the SPY leg is sized to
  `NAV * exposure` (notional may exceed NAV, funded by broker margin); the BIL borrow leg
  (negative weight) is **carried through, never silently dropped**.
- `paperbot/s4_risk.py` — S4 risk guard (permits exposure up to the active profile's
  `leverage_cap`, vetoes beyond) + a fail-closed **margin preflight** that refuses the
  leveraged (>1.0) path unless the account is a confirmed margin account with sufficient
  buying power. The un-levered path is allowed on any account type.
- `paperbot/s4_rebalance_run.py` — single-account **review-only** runner (gateway-locked,
  read-only connect, `place(armed=False)` — transmits nothing). Account id is a parameter.
- `paperbot/s4_daily_run.py` — the **calendar-gated daily entry point** to schedule.

Tests: `paperbot/test_s4_strategy_target.py` (parity + causality + stale guard),
`test_s4_sizing_risk.py` (leverage sizing + guard + preflight), `test_s4_runner.py`
(preview + calendar gate). Run `cd paperbot && python -m pytest -q`.

## Deploy checklist (the remaining operational steps)

1. **Set the S4 account id.** It is a required `--account` parameter — never hardcoded.
   Pick the paper account S4 will run in and pass it to `s4_daily_run.py`.

2. **Confirm that account is a MARGIN paper account with buying power.** The margin
   preflight enforces this at deploy: for the `balanced` (1.5x) profile the fund runs SPY
   exposure above 1.0x on real margin, so a **cash** account or thin buying power will be
   **refused** by the preflight (no orders built). ⚠️ If the existing `DU8922142–146` subs
   are cash accounts, the balanced/1.5x profile **cannot run** on them until a margin paper
   account exists. The `conservative` (5%) profile **never borrows** (avg exposure ~0.35x,
   the cap never binds), so it runs on **any** account type regardless.

3. **Pick a profile.** `--profile conservative` (5%/1.5x, un-levered, bond-alternative
   role — safe on any account) or `--profile balanced` (10%/1.5x, needs a margin account).
   Or pass explicit `--target-vol` + `--leverage-cap` for a custom cell.

4. **Schedule `s4_daily_run.py`** (Windows Task Scheduler, run-whether-logged-on, matching
   the desk convention for the other tasks). Suggested: once daily after the SPY close +
   forward-data ingest (so `price_date` is fresh; the stale-data guard fails closed
   otherwise). Example action:

   ```
   Program:   C:\TradingDesk-Local\venv\Scripts\python.exe
   Arguments: "C:\Users\andre\My Drive (andrew@surberhc.com)\TradingDesk\paperbot\s4_daily_run.py" --account DU89221XX --profile conservative
   Start in:  C:\Users\andre\My Drive (andrew@surberhc.com)\TradingDesk\paperbot
   Settings:  Run whether user is logged on or not; Run with highest privileges
   ```

   On a weekend/holiday the driver is a clean NO-OP (exit 0, no connection). **Registering
   this task is a deliberate human step — this build does NOT register any scheduled task.**

5. **Arm via the future executor.** The daily runner is review-only (`place(armed=False)`,
   read-only connect — transmits nothing). Actually placing orders is the job of the future
   S4 executor (clientId 45 `paperbot_s4_exec`, reserved, **not yet built**): a
   transmit-capable sibling that connects `readonly=False`, pinned to the S4 sub, and places
   armed. Building + arming it is a separate, deliberate step (see memory: paper-arming-and-
   fills, paper-not-a-ming-vase). Nothing in the current build can transmit.

## Data feed (unchanged)

The daily deploy still needs the latest SPY adjusted close appended to `bt_data` before each
run (the same forward daily-price pipeline the rest of TradingDesk uses); the stale-data
guard in `s4_strategy_target.py` fails closed if that ingest hasn't happened.
