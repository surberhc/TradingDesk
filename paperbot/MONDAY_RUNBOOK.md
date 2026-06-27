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
is set to the computed split **by the executor** (`rebalance_execute.py` via `replaceFA`)
in lockstep with each block — no manual GUI editing of the splits.

> KNOW THIS BEFORE YOU START: the Monday transmit is driven by **`rebalance_execute.py`**
> (clientId **38**), the transmit-CAPABLE sibling of `rebalance_run.py`. **Default run is a
> read-only DRY review that transmits NOTHING** — identical to `rebalance_run.py`. To send,
> you must line up ALL FOUR gate conditions: `READONLY=False` AND `DRY_RUN=False` AND
> `armed=True` AND the exact CLI token `--arm-i-understand` present. The token (and nothing
> else) flips READONLY/DRY_RUN in-process; there is NO auto-arm. The armed flow is fully in
> code: discover → build_plan → resolve_tier_groups (fail-closed) → risk_manager → **BACK UP
> FA config** → set each group's ContractsOrShares via `replaceFA` → place blocks ONE at a
> time (never `whatIfOrder` a group order) → reconcile. `order_router` now rejects any
> NaN/<=0 limit price. 33 tests pass. `rebalance_run.py` is still the read-only source of
> truth for the NUMBERS; `rebalance_execute.py` is the thing that sends them.

Python venv: `C:\TradingDesk-Local\venv\Scripts\python.exe`. Run every paperbot script
**from the paperbot dir** (flat imports). ClientIds: accounts=31, recon=32, fa-probe=33,
rebalance-runner=37, **rebalance-executor=38**. Both runner and executor pin their
connection to DU8922142 (master DF…141 hangs the account stream — never connect un-pinned
to the master).

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
Read-only, build-only. The executor's **default run** (no token) is identical to
`rebalance_run.py`: computes tier models off live quotes (falls back to strategy close),
resolves each tier→FA group by **live membership** (fail-closed), prints the full plan, and
logs the order objects with **nothing transmitted, no FA config written**.

```
C:\TradingDesk-Local\venv\Scripts\python.exe rebalance_execute.py
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

**Eyeball**, per FA group, the exact per-account share split — these are what the executor
will write to ContractsOrShares via `replaceFA` when armed (you do NOT type them by hand).
Example shape:
`tier_balanced: SPY DU…143=870 DU…144=870 (block 1740); BND DU…143=7256 DU…144=7256 (14512)`.

---

## STEP C — Pre-arm: eyeball the FA-groups XML casing (MONDAY FLAG)
The executor's `set_group_contracts_or_shares` matches/writes XML tags
(`defaultMethod`/`ListOfAccts`/`Account`/`acct`/`amount`) in the live GROUPS XML. **The exact
tag casing could not be confirmed offline.** Before arming, dump the live config and eyeball it:

```
C:\TradingDesk-Local\venv\Scripts\python.exe fa_probe.py
```
Confirm the group names match enrollment (`tier_balanced`, `tier_growth`) and that the
member/method tag casing in the dump matches what the executor expects. If casing differs,
**STOP** and fix the executor's tag handling before any armed run — a casing mismatch means
ContractsOrShares may not be written correctly. (fa_probe uses its own clientId 33.)

---

## STEP D — Arm the gateway (only after A + B + C are clean)
The 4-condition gate: `READONLY=False` AND `DRY_RUN=False` AND `armed=True` AND the
`--arm-i-understand` token. The token (Step E) flips READONLY/DRY_RUN in-process; this step
satisfies the gateway side.

```
C:\TradingDesk-Local\venv\Scripts\python.exe -c "import arming; arming.arm()"
```
This flips `ReadOnlyApi=no` and restarts the gateway. **Re-run `accounts.py` after the restart
to confirm the feed came back** before you send anything. Keep the Step B review handy — the
numbers you arm against MUST be from a recent run (quotes drift — re-run Step B if >few min).

---

## STEP E — Transmit via the executor (armed; one block at a time, in code)
With the gateway armed (Step D), run the executor WITH the exact token. The executor itself
backs up the FA config, sets each group's ContractsOrShares via `replaceFA`, and places blocks
one at a time — you do not touch the GUI:

```
C:\TradingDesk-Local\venv\Scripts\python.exe rebalance_execute.py --arm-i-understand
```
The safety banner must read `transmission: PERMITTED`. The armed run will, per the ledger:
1. Re-discover live state, rebuild the plan, resolve groups fail-closed, run risk guards
   (any HALT/VETO = NOTHING sent, no FA config written).
2. **Back up** the live FA groups XML to a timestamped file under `state\paperbot\fa_backups\`.
3. For each FA block: write THAT group's ContractsOrShares to the split via `replaceFA`, then
   place the block with `place(armed=True)` — `faMethod=""`, LIMIT/DAY, deterministic orderRef.
4. Conservative (DU8922142) routes **DIRECT** (lone account, no group).
- Confirm each order ACCEPTS (no Err 10226 — would mean a stray order-level `faMethod`).
- The executor **never `whatIfOrder`s a group order** (it HANGS) — what-if is skipped for blocks.
- The HARD PRICE GUARD rejects any NaN/<=0 limit before an order is built; a rejected route is
  logged and skipped, never sent blank.

---

## STEP F — Watch fills
- From the executor's `place()` output (and the GUI if open), confirm each block fills and the
  master **allocates** to the member accounts per the written split (filled = sum of the split).
- Partial/no fill on a limit: leave it working or adjust the limit; do NOT switch to market.
- Confirm each member account's filled shares == its split number. A mismatch means the
  group's ContractsOrShares didn't match the block qty — stop, re-check Step C's casing.

---

## STEP G — Reconcile each account to model
The armed executor reconciles in-process at the end (recon_report read-only readout, step `[8]`)
— every account should print `in-band`. To re-confirm independently after the run:

```
C:\TradingDesk-Local\venv\Scripts\python.exe recon_report.py
```
PASS when every account reads **in-band** (no REBALANCE), DRIFTED count ~0, and no UNTRACKED
holdings. If an account still shows REBALANCE, compare its per-holding TGT_SH vs actual — a
1–2 share gap is integer-floor rounding (acceptable); a large gap means a block under/over
filled → investigate that symbol before close.

---

## DISARM (MANDATORY — always, when done or aborting)
Restore the safe lock. **This is not optional — never leave the gateway armed:**
```
C:\TradingDesk-Local\venv\Scripts\python.exe -c "import arming; arming.disarm()"
```
Confirms `ReadOnlyApi=yes` restored + gateway restarted (transmission re-locked). Then
re-run `accounts.py` to confirm the feed is healthy in the locked state.

---

## ABORT / ROLLBACK — if anything looks wrong at ANY step
1. **Stop sending.** `Ctrl-C` the executor (it prints a disarm reminder) / do not place the next block.
2. Cancel any working (unfilled) orders in the GUI.
3. **DISARM immediately** (`arming.disarm()`) — restores ReadOnlyApi=yes + restart.
4. If a block already filled and is wrong, flatten the affected accounts back to flat with
   `flatten_accounts.py` (own clientId 34; review it first — it places real paper sells),
   then re-run `recon_report.py` to confirm flat, and re-diagnose with the dry-run
   (`rebalance_execute.py` with no token, or `rebalance_run.py`) before any retry. The FA
   config backup lives in `state\paperbot\fa_backups\` if a group's split needs restoring.
5. Re-run `accounts.py` to confirm the feed is healthy before walking away.

## Gotchas (ranked) to watch for live
1. **Default `rebalance_execute.py` transmits NOTHING** — the armed run needs ALL FOUR:
   `READONLY=False` + `DRY_RUN=False` + `armed=True` + the exact `--arm-i-understand` token.
2. The executor writes ContractsOrShares via `replaceFA` — **eyeball the FA-groups XML casing
   with `fa_probe.py` first (Step C)**; the tag casing was not confirmable offline.
3. Conservative (DU8922142) is a LONE account → DIRECT order, no FA group. Don't wait for a
   block that won't come.
4. ContractsOrShares is per-symbol — the executor sets it per block, in lockstep with sending.
5. A missing quote → NaN/<=0 limit. The HARD PRICE GUARD rejects it before build; Step B's
   "no NaN" check is your early warning. Never transmit a NaN/blank-priced line.
6. Live group membership must equal enrollment exactly or it fails closed — fix the GUI
   group, never override the name.
7. Re-run Step B if quotes are stale (>few min) before arming; arm against fresh numbers.
8. **DISARM is mandatory** when done or aborting — never leave the gateway armed.
