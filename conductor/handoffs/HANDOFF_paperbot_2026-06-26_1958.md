# THREAD HANDOFF → CONDUCTOR  (2026-06-26)

This is the live continuation of the **Paperbot / desktop** thread. If you are the Conductor
(likely on Andrew's phone), read `MISSION.md`, then this file, then `STATUS.md` + `DECISIONS.md`.
Then drive. Speak to Andrew in plain English, one decision at a time. **PAPER ONLY.**

## IMMEDIATE NEXT ACTION (already decided by Andrew — execute, don't re-ask)
**FLATTEN EVERY DU SUB-ACCOUNT TO ZERO.** (From `DECISIONS.md`, id `paperbot-flatten`.)
- Sweep **DU8922142, DU8922143, DU8922144, DU8922145, DU8922146** — Andrew believes several hold
  leftover shares (e.g. 143 and 146 each showed 1 position earlier; 142 holds the 1 PDBC test share).
- For each account, sell every open position back so the account ends at **zero positions**.
- **Confirm zero across all five DU accounts when done**, and log each flatten to the ledger.

### How to execute it safely (lessons already paid for — do NOT repeat them)
- **Trade the DU sub-accounts, never the FA master DF8922141** (it rejects direct orders and hangs reads).
- **Connect NON-readonly with the account pinned:** `ib.connect(host, 4002, clientId=30, readonly=False,
  account="DU89221xx")` — pinning the account avoids ib_async hanging on the master's update stream.
- **Do NOT call `whatIfOrder`** — it hangs with no timeout (cost us ~40 min today). Place directly.
- The paper gateway's hard read-only lock is **OFF** already, so **no gateway restart is needed** — just
  connect non-readonly and place. (If a restart is ever needed: fully kill the gateway, wait until the
  process is gone AND port 4002 closed, settle ~4s, then start with env `java_version=17`.)
- **Serialize:** one account, one order at a time. Use marketable-limit SELLs (limit = bid) on liquid ETFs.
- Reusable pieces live in `paperbot/`: `live_quotes.py`, `order_router.py` (`place(..., armed=True)`),
  `reconcile.py`, `ledger.py`. The proven minimal place-and-fill pattern is in `live_fill_test.py`.

## What this thread accomplished today
- Fixed the gateway auto-launch bug (`java_version=17`; baked into `connections/ibkr.py` and
  `dailyreport/daily_run.py`). See memory `gateway-launch-fix`.
- Built the full paperbot dry-run pipeline: `strategy_target` → `execution_engine` (diff vs positions)
  → `risk_manager` (kill switch + caps, verified firing) → `order_router` → `reconcile` + `ledger`.
- Wired **live quotes**; corrected the account model (141 = FA master, trade the DU subs).
- **Validated the first real PAPER fill** (BUY 1 PDBC @15.85 in DU8922142).
- Turned the paper gateway hard read-only lock **OFF** (software arming is the control now).

## Current system state
- Gateway: UP, serving, single instance, `ReadOnlyApi=no` (paper hard-lock off by design).
- Default engine (`paperbot/execution_engine.py`) still READONLY+DRY_RUN — transmits nothing on its own.
- Open positions to clear: the flatten task above.

## Still unbuilt / open
- **Desktop auto-watcher NOT set up yet.** For the phone→desktop loop to run hands-off, a desktop session
  must watch `conductor/DECISIONS.md` and act (a `/loop` session or a scheduled poll). Until then, the
  desktop only acts when a session is open and prompted. Decide with Andrew if he wants this stood up.
- Other lanes' open items are in `STATUS.md` (A: byte-parity re-prove; B: options_eod view fix; D: EOD digest).

## How to proceed right now
1. Tell Andrew in one short message: the flatten task is ready to run, and ask **"want me to run the
   flatten now?"** (it needs a desktop worker active to execute).
2. On his go, have the desktop worker flatten all five DU accounts per the steps above, then report zero.
3. Keep `STATUS.md` current; append any new decisions to `DECISIONS.md`.
