# MONDAY RUNBOOK — First live PAPER rebalance (5 client sub-accounts)

PAPER ONLY. Port 4002, paper master DF8922141, paper subs DU8922142–146. Nothing in this
runbook touches real money. Read it top to bottom once before you start.

## What we are doing
First live paper rebalance of 5 FLAT client sub-accounts, each ~$1.1M:

| Account | Tier | FA group | Routing |
|---|---|---|---|
| DU8922142 | Conservative | tier_conservative | **DIRECT** (lone account in tier) |
| DU8922143 | Balanced | tier_balanced | FA block |
| DU8922144 | Balanced | tier_balanced | FA block |
| DU8922145 | Growth | tier_growth | FA block |
| DU8922146 | Growth | tier_growth | FA block |

Allocation = **explicit per-account integer shares** the engine computes. Order-level
`faMethod=""` (NetLiq is rejected, Err 10226). Each FA group's stored **ContractsOrShares**
is set to the computed split by hand in the gateway GUI before the block is sent.

> KNOW THIS BEFORE YOU START: `rebalance_run.py` is **build-only**. It connects
> `readonly=True` and hardcodes `armed=False`, so it can NEVER transmit — it only prints the
> plan and logs the order objects. The actual order placement on Monday is a **manual GUI
> step** in IB Gateway/TWS (set ContractsOrShares, then transmit the group/direct orders),
> OR an explicitly-armed `order_router.place(armed=True)` over a `readonly=False` connection
> (the pattern in `live_fill_test.py` / `fa_block_test.py`). `rebalance_run.py` is the
> source of truth for the NUMBERS, not the thing that sends them.

Python venv: `C:\TradingDesk-Local\venv\Scripts\python.exe`. Run every paperbot script
**from the paperbot dir** (flat imports). ClientIds: accounts=31, recon=32, fa-probe=33,
rebalance-runner=37. The runner pins its connection to DU8922142 (master DF…141 hangs the
account stream — never connect un-pinned to the master).

---

## STEP A — Confirm the gateway account feed is back (quick read, must NOT hang)
Market open, gateway logged into PAPER, API on 4002. Prove the account stream answers fast.

```
cd "C:\Users\andre\My Drive (andrew@surberhc.com)\TradingDesk\paperbot"
C:\TradingDesk-Local\venv\Scripts\python.exe accounts.py
```
PASS when within a few seconds you see the table with all of DU8922142–146 funded
(~$1.1M each), the master DF8922141 flagged advisor, and **"Enrollment reconciliation:
clean"**. If it HANGS (master account-stream lock) or any enrolled account is missing/not
funded → **STOP**. Do not proceed. (accounts.py uses its own clientId 31 and is pinned away
from the master, so a hang here means the feed itself is not healthy.)

---

## STEP B — Dry-run review: eyeball per-account shares + the splits
Read-only, build-only. Computes tier models off live quotes (falls back to strategy close),
resolves each tier→FA group by **live membership** (fail-closed), prints the full plan, and
logs the order objects with **nothing transmitted**.

```
C:\TradingDesk-Local\venv\Scripts\python.exe rebalance_run.py
```
Confirm ALL of the following before continuing — if any is off, **STOP**:
- Safety banner reads `READONLY=True  DRY_RUN=True  armed=False` and `transmission: BLOCKED`.
- `[4]` resolves: `Conservative -> tier_conservative`, `Balanced -> tier_balanced`,
  `Growth -> tier_growth`. If it raises "FAILING CLOSED" the live group membership doesn't
  match enrollment — fix the groups in the GUI, re-run. Do NOT hand-pick a group name.
- PER-ACCOUNT TARGET BOOK: each account shows the expected tier holdings, sane TGT_SH, and
  TGT_$ ≈ investable (≈ $1,045,000 = 1.1M × 0.95 cash-reserve; reserve=0 unless a cashflow
  is scheduled). From flat, DELTA_SH == TGT_SH and all BUYs.
- BLOCK ORDERS: Balanced and Growth show one block per symbol with a 2-way split; each
  split line's two numbers sum to the block `x<qty>`. **Conservative shows as DIRECT**
  (lone account) — that is correct, not a bug.
- **No NaN / no $0 / no negative** TGT_$ or limit anywhere. A blank/NaN price means a quote
  was missing and the close fallback also failed → STOP and fix data, do not trade that name.
- Routes: every `fa_block` split sums to its `total_qty`; the one Conservative line is
  `direct account=DU8922142`.

**Write down**, per FA group, the exact per-account share split (you will type these into the
GUI in Step C). Example shape:
`tier_balanced: SPY DU…143=870 DU…144=870 (block 1740); BND DU…143=7256 DU…144=7256 (14512)`.

---

## STEP C — Set each FA group's ContractsOrShares to the computed split
GUI step in IB Gateway/TWS FA configuration (Account → FA config → Groups). For EACH tier
group that has an FA block (`tier_balanced`, `tier_growth` — NOT Conservative, it's direct):
1. Open the group, method = **ContractsOrShares**.
2. Enter, per member account, the **share count for the symbol you are about to send**.
   ContractsOrShares is per-order-quantity allocation: you set it to the split for the block
   you are placing next, then place that block, then update it for the next symbol's block.
3. Save. Re-read the group and confirm the numbers match what you wrote down in Step B.

Because the split is set per symbol, place ONE block at a time: set group split → send that
block → confirm fills → set next split → next block. Do not batch different symbols against
one stored split. The Conservative account skips this entirely (direct order).

---

## STEP D — Flip the arm gate (only after B + C are clean)
Arming = `ReadOnlyApi=no` + gateway restart (`arming.py`), THEN a `readonly=False` connection
and `order_router.place(..., armed=True)`. All three must hold to transmit: `READONLY=False`,
`DRY_RUN=False`, `armed=True`.

- If transmitting from the GUI: arm with `python -c "import arming; arming.arm()"` (flips
  ReadOnlyApi and restarts the gateway), then place orders in the GUI. Re-run `accounts.py`
  after the restart to confirm the feed came back before you send anything.
- If transmitting from code: use the `live_fill_test.py` pattern (connect `readonly=False`,
  build, `place(armed=True)`). `rebalance_run.py` will NOT send even if you pass armed — it
  connects read-only by construction; do not rely on it to transmit.

Keep the dry-run review window open. The numbers you arm against MUST be the numbers from
Step B's run on the SAME session (quotes drift — re-run B if more than a few minutes pass).

---

## STEP E — Transmit, one block at a time
Order: do the direct Conservative orders and the FA blocks symbol-by-symbol. After each:
- Confirm the order ACCEPTS (no Err 10226 — that means a stray `faMethod` slipped in; the
  block must carry `faMethod=""` and rely on the stored ContractsOrShares).
- **Never `whatIfOrder` a group order — it HANGS.** Skip what-if for FA blocks entirely.
- Use LIMIT orders (config ORDER_STYLE=limit), DAY tif. orderRef is deterministic
  (`paperbot:<group|account>:<as_of>:<side>:<symbol>`) so a re-send is detectable, not blind.

---

## STEP F — Watch fills
- In the GUI or from `place()` output, confirm each block fills and the master **allocates**
  to the member accounts per the stored split (filled = sum of the split).
- Partial/no fill on a limit: leave it working or adjust the limit; do NOT switch to market.
- Confirm each member account's filled shares == its split number. A mismatch means the
  group's ContractsOrShares didn't match the block qty — cancel remainder, re-check Step C.

---

## STEP G — Reconcile each account to model
After fills, prove the book matches the model with the read-only readout:

```
C:\TradingDesk-Local\venv\Scripts\python.exe recon_report.py
```
PASS when every account reads **in-band** (no REBALANCE), DRIFTED count ~0, and no UNTRACKED
holdings. If an account still shows REBALANCE, compare its per-holding TGT_SH vs actual — a
1–2 share gap is integer-floor rounding (acceptable); a large gap means a block under/over
filled → investigate that symbol before close.

---

## DISARM (always, when done or aborting)
Restore the safe lock:
```
C:\TradingDesk-Local\venv\Scripts\python.exe -c "import arming; arming.disarm()"
```
Confirms `ReadOnlyApi=yes` restored + gateway restarted (transmission re-locked). Then
re-run `accounts.py` to confirm the feed is healthy in the locked state.

---

## ABORT / ROLLBACK — if anything looks wrong at ANY step
1. **Stop sending.** Do not place the next block.
2. Cancel any working (unfilled) orders in the GUI.
3. **DISARM immediately** (`arming.disarm()`) — restores ReadOnlyApi=yes + restart.
4. If a block already filled and is wrong, flatten the affected accounts back to flat with
   `flatten_accounts.py` (own clientId 34; review it first — it places real paper sells),
   then re-run `recon_report.py` to confirm flat, and re-diagnose with `rebalance_run.py`
   before any retry.
5. Re-run `accounts.py` to confirm the feed is healthy before walking away.

## Gotchas (ranked) to watch for live
1. The runner cannot transmit — placement is a separate manual/armed step (above).
2. Conservative (DU8922142) is a LONE account → DIRECT order, no FA group. Don't wait for a
   block that won't come.
3. Stored ContractsOrShares is per-symbol — set it per block, in lockstep with sending.
4. A missing quote → NaN limit price builds a $0/NaN order. Step B's "no NaN" check catches
   it; never transmit a NaN/blank-priced line.
5. Live group membership must equal enrollment exactly or Step B fails closed — fix the GUI
   group, never override the name.
6. Re-run Step B if quotes are stale (>few min) before arming; arm against fresh numbers.
