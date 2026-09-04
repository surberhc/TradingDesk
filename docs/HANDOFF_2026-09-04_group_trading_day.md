# HANDOFF — 2026-09-04 — First full live group-trading day

**Read this first if you are a new session picking up the trading desk.**
Written at the close of 2026-09-04. Everything below is measured, not assumed.

---

## 1. WHAT WAS ACCOMPLISHED

Five models were rebalanced live through the FA group-block rail — the first time the desk
has traded a whole book this way.

| Model | Accounts | Result at close |
|---|---|---|
| Balanced (Custom) | 7 | ON TARGET, every line |
| Balanced (Small, Custom) | 2 | ON TARGET |
| Growth (Small, Custom) | 41 | 38 on target, 3 off by one share |
| Starter (Custom) | 27 | 6 on target, 21 short by 1-2 shares (cash buffer) |
| Growth (Custom) | 106 | BUCK tail fully cleared on the final run |

Notable executions:
- **BUCK sold 397,412 shares (~$9.2M) across the day** — more than 200% of its 20-day
  average volume (184,694). Fills ranged 23.22-23.31. The final 47,356 filled at 23.24.
  BUCK is now at its 2% model weight, not zero — it remains a model holding.
- **BIL sold 48,580 (~$4.44M)**. JAAA was cleared book-wide (Andrew did that by hand in TWS
  because the API cannot sell a fractional position).
- Growth (Custom)'s big run filled **63 of 63 blocks**, zero cancels, zero reprices.
- Final run: 16 of 16 blocks, "Uninvested-proceeds check: CLEAN".

## 2. WHAT IS STILL OPEN — START HERE ON TUESDAY

**Monday 2026-09-07 is Labor Day. Next session is Tuesday 2026-09-08.**

### 2a. Re-run the sweep first
The last full sweep was taken BEFORE the final BUCK run. Re-run
`group_execute.verify_in_sync` across all five models and confirm Growth (Custom) is clean.
Expect the 94 previously-overweight accounts to now be on target.

### 2b. Starter (Custom) — 20 accounts 1-2 shares short of SCHB
Cause: the 1% cash safety buffer plus whole-share rounding in accounts averaging ~$1,400.
Not yet investigated properly. See to-do #6.

### 2c. PDT — resolved, but re-check
19 accounts were pattern-day-trader restricted at 12:10; all 19 cleared by 12:46.
Re-scan before trading Growth (Small) or Starter.

## 3. THE OUTAGE, AND ITS ROOT CAUSE (read before touching FA groups)

**Group creation died at ~13:57 and stayed dead for 45 minutes.** Cause:

> A maintenance purge rewrote the master's FA groups document using Python's
> `ET.tostring()`, which omits the `<?xml version="1.0" encoding="UTF-8"?>` declaration
> that IBKR's own document always carries. From that write onward IBKR accepted
> DELETIONS from the document and SILENTLY REJECTED every ADDITION.

Creation had worked 190 times earlier the same day. A gateway restart did not fix it.
Three theories were chased and all three were wrong (wedged gateway, bad clone template,
invalid member accounts). The answer only appeared once we listened to the broker.

Fixed: `fa_membership.serialize_groups()` always emits the declaration; every serializer
routes through it. Two further errors surfaced on the way and are also fixed:
- `[10260] unsupported method (NetLiq)` — new groups are created `ContractsOrShares`
- `[10229] FA data saving error: Invalid Group` — new members get a placeholder amount of
  **1**, not 0; an all-zero ContractsOrShares group is invalid

## 4. STATE OF THE MACHINE

- **Desk** runs on `C:\TradingDesk-Local\venv\Scripts\python.exe -u -m streamlit run
  desk_app.py --server.port 8502 --server.headless true --server.address 127.0.0.1
  --server.runOnSave false`, with stdout/stderr redirected to
  `C:\TradingDesk-Local\state\desk_stdout.log` and `desk_stderr.log`.
  **It used to run under `pythonw` with its output discarded — never do that again.**
  A code change requires a FULL process restart; Streamlit does not reload imported modules.
- **Gateway** live-trade lane, port 4003, login `asurber219`, master `F6795549`,
  355 managed accounts. Andrew restarted it manually at 14:07 — it needs his 2FA, so only
  he can do it. Never ask him to use a browser.
- **Permanent FA groups that must NEVER be deleted:**
  `Dougs Group, Income, Main, MainSmall, No Trade, Rebalance, Rob, Ted`
- **Gateway precautionary limits** (set by Andrew): order size **100,000 shares**,
  order value **$5,000,000**. Mirrored in `config.GATEWAY_ORDER_SIZE_LIMIT` and
  `GATEWAY_ORDER_VALUE_LIMIT` — keep them in step with the gateway.

## 5. TO-DO LIST (everything raised during the day, highest value first)

1. **Batch the group writes.** A 63-block run does 63 separate read-modify-write cycles of
   the WHOLE FA document, plus another per block at placement. That is both the ~25-minute
   run time AND the cause of the group bloat that triggered the outage. One `replaceFA` for
   all groups, then fire all sells at once and all buys at once — `order_router.place()`
   ALREADY batches (it places every order, then watches them all). ~25 min becomes ~2 min.
2. **Capture broker errors on every remaining API call.** Only `fa_group_sync` listens to
   `ib.errorEvent`, and only since today. Still blind: `rebalance_execute.py:290`
   (`set_group_contracts_or_shares` writes the per-account split before EVERY block — a
   silent failure there means trading against a stale allocation), plus `placeOrder` and
   `cancelOrder` in `order_router.py`, `safe_execute.py`, `live_fa_block_execute.py`.
   Build ONE shared helper so there is never a third version of this bug.
3. **Live run log under the Send button** on the Group trade page, streaming as it happens.
   Andrew should not need Claude to read a log file to know what his trade is doing.
4. **Automatic group cleanup after a run.** Every run leaves one throwaway group per block.
   Four runs took the document from 8 groups to 198 and broke the gateway.
5. **Fix the Reset button properly.** It still leaves the strategy tickboxes checked on a
   fresh window. Clearing `st.session_state["gt_*"]` was not enough.
6. **Revisit `CASH_SAFETY_BUFFER_PCT` (1%).** It systematically under-buys by about a share
   per line per account, forcing a second pass and leaving 20 Starter accounts short.
7. **Desk reporting should separate tradeable gaps from untradeable stubs.** Reporting "41
   accounts off target" when most cannot be fixed via the API sends Andrew chasing ghosts.
8. **Monthly distribution reserve.** Accounts with regular monthly income (the Kinsey IRA
   was named) must hold back cash BEFORE a rebalance. Andrew believes checks were built but
   is not sure they work. ALSO WANTED: a daily checklist confirming everyone owed a
   distribution has the cash available. NOT INVESTIGATED YET — status unknown.
9. **A Stop button on a running trade.** Today the only way to halt a stuck run was killing
   the desk process from a shell.
10. **BUCK via the ETF block desk.** BUCK trades ~184k shares/day and we were over 200% of
    that in one session. If a large BUCK trade is ever needed again, call IBKR's ETF block
    desk or the issuer's capital markets desk for a NAV trade instead of working the book.

## 6. HARD-WON FACTS (do not re-derive these)

- **IBKR refuses ANY fractional order via the API** — `[10243] Fractional-sized order cannot
  be placed via API. Please use desktop version.` This applies to FA block orders too. A
  full exit computed as 865.3444 places 865 and leaves 0.3444, and only the desktop platform
  can clear that stub. This was ALREADY in memory `fractional-impossible-via-tws-api` and was
  rediscovered the hard way. CHECK THAT NOTE BEFORE TOUCHING ORDER QUANTITIES.
- **A stub must NOT force an account to trade.** Making FRACTIONAL always-breach created a
  permanent loop: 41 of 43 accounts re-trading, ALL driven by a stub alone, ZERO on trade
  size. Stubs are reported by `verify_in_sync` and never traded on.
- **`DayTradesRemaining` is a Reg T MARGIN tag.** It is ABSENT by design on a cash account.
  Failing closed on absence silently excluded 38 of 185 enrolled accounts. Only refuse when
  IBKR affirmatively answers `0`.
- **A read straight after an FA write is served STALE.** An early purge reported deleting 82
  groups while deleting none. Settle, then re-read on a fresh call.
- **Price each phase immediately before it runs.** Limits computed at plan time went stale
  during 20 minutes of group creation, leaving buy limits BELOW the bid — every buy block
  timed out unfilled. That is what made the 11:07 run sell $6.77M and buy nothing.
- **Adaptive/Patient will not cross the spread.** Plain marketable limits for small blocks,
  Adaptive **Urgent** for large ones (>$50k). Patient is why nothing filled.
- **A block over $50k gets a 10-minute working window**, not 90 seconds. BUCK at 99,952
  shares filled in about a minute on Urgent; on a 90-second plain limit it filled zero.
- **A failed sell must stop the buys it funds.** The sell-phase gate was "reached a TERMINAL
  state", and Cancelled is terminal — so a $354k BUCK sell that filled ZERO counted as
  "sells done" and the run walked into the buy phase it was supposed to fund. Now it halts
  on a zero-fill sell; a PARTIAL still funds a proportionally smaller buy.
- **"Sent" is not an outcome.** The desk reported "25 block(s) sent" in a green box for a run
  in which 12 of 25 blocks traded nothing. Green only when every block filled in full.

## 7. KEY FILES

| Path | What it owns |
|---|---|
| `paperbot/group_execute.py` | run orchestration, `verify_in_sync`, `purge_run_groups`, ledger |
| `paperbot/group_rebalance.py` | per-ticker block planning, `block_order_qty`, gateway-limit cap |
| `paperbot/live_fa_block_execute.py` | two-phase execution, PDT/margin gates, outcome grading |
| `paperbot/fa_group_sync.py` | the ONLY module that writes FA config; captures broker errors |
| `paperbot/fa_membership.py` | pure XML; `serialize_groups()` emits the declaration |
| `paperbot/order_router.py` | order construction and `place()`; records `broker_message` |
| `dashboard/desk/page_group_trade.py` | the Group trade page |
| `C:\TradingDesk-Local\state\paperbot\runs.jsonl` | audit record of every run |
| `C:\TradingDesk-Local\state\paperbot\fa_backups\` | FA XML backup taken before every write |
| `C:\TradingDesk-Local\state\desk_stdout.log` | the desk's live run narrative |

## 8. COMMITS FROM TODAY

- `a7409f6` full exit sells the whole position
- `d0543a8` a stub of a dropped holding clears itself
- `80331f0` the fraction survives to the order
- `d93d9f7` price each phase when it runs; stop inventing PDT blocks
- `32e84b8` report what blocks ACHIEVED; halt on a dead sell; work large blocks
- `75e196d` size decides the order type — large blocks get Adaptive Urgent
- `1e5cc0a` record WHY the broker refused a block; log the desk
- `6e39bef` block orders are whole shares — IBKR error 10243, verbatim
- `2e1aca7` the XML declaration — why group creation died, and four fixes

Tests at close: **paperbot 1480 passing, backtester 489 passing.**

## 9. GROWTH (CUSTOM) TARGET ALLOCATION as of 2026-09-03

```
XLI 13% · XLV 13% · XLE 13% · XLF 13% · XLB 12%
GLDM 7% · USFR 7% · FLOT 4%
XLP 3% · GDX 3% · XLU 3%
BUCK 2% · SIVR 2% · TLT 2% · GDXJ 2% · SILJ 1%
```
