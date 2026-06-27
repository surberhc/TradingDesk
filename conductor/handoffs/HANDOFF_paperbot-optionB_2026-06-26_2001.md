# HANDOFF — Lane C — Paperbot Option B (multi-account FA rebalancing)

**For:** the desktop worker who picks up this lane. **From:** the session that built
Increment 1–3 of Option B (2026-06-26). **Posture:** everything below is READ-ONLY /
what-if — **no client order has been transmitted, no FA config has been written.**

> PAPER ONLY. The accounts are paper (DF/DU…). Real money is out of scope.

---

## 1. Mission in one paragraph
Andrew is an RIA. He wants his validated All-Weather strategy to rebalance multiple
client accounts on IBKR automatically. We proved IBKR has **no API to create/edit a
Model Portfolio** (GUI-only), so he chose **Option B: trade the client accounts directly
via the API using FA allocation**. The engine targets each account to its tier model,
executes changes as **block orders (one average price, allocated per account)**, holds a
**cash reserve for distribution clients**, and keeps a **human review→arm→transmit gate**.

## 2. Decisions already made (don't re-litigate)
- **Option B chosen** (direct multi-account), not Option A (manual model edit).
- **Per-account risk version:** each account is tagged Conservative/Balanced/Growth
  (`paperbot/config.py → ENROLLMENT`) and gets that tier's weights.
- **Execution = block + NetLiq:** model changes and same-tier same-direction true-ups
  are aggregated into one FA group order, executed as a block at one average price,
  split across accounts proportional to net liq. (Per-account *direct* orders are the
  fallback for drift true-ups on a single account.)
- **Distribution reserve:** investable = (NetLiq − reserve) × (1 − cash_reserve); the
  reserve (next month's distribution) is carved out *before* any buy, so we never
  buy-today/sell-tomorrow. Lives in the model's own SGOV/T-bill sleeve. Built into the
  rebalancer, NOT a separate program. Schedule = `paperbot/cashflows.py → SCHEDULE`
  (currently EMPTY; Andrew fills it in over time).
- **No-trade band** `config.REBALANCE_BAND_PCT = 0.03` applied identically to all
  accounts (compliance: no per-client discretion).
- **Human gate:** review → arm → transmit. Keep `READONLY` + `DRY_RUN` + `armed` as
  three independent blocks.
- **Compliance:** keep it in mind as we build; formal written policies are DEFERRED
  until further along (Andrew's call). Version-stamp + git history are the traceability.

## 3. What was built this thread (all verified read-only unless noted)
| File | What it does | Verified? |
|---|---|---|
| `paperbot/config.py` | `ENROLLMENT` (acct→tier), `VALID_VERSIONS`, `REBALANCE_MASTER=False`, `REBALANCE_BAND_PCT` | ✅ |
| `paperbot/accounts.py` | read-only multi-account discovery + enrollment reconciliation | ✅ ran clean |
| `paperbot/cashflows.py` | distribution/contribution schedule + reserve math | ✅ (schedule empty) |
| `paperbot/recon_report.py` | **daily multi-account reconciliation + drift + block-aggregation report** | ✅ ran; output below |
| `paperbot/reconcile.py` | added optional `investable` override so reserve flows into drift math | ✅ |
| `paperbot/version.py` | version label + CHANGELOG (now **v0.4.0**) + `stamp()` for ledger | ✅ |
| `paperbot/fa_probe.py` | read-only dump of gateway FA groups/profiles/aliases (`requestFA`) | ✅ ran; output below |
| `paperbot/order_router.py` | added `build_fa_block()` — constructs an FA group (block) order | built, NOT yet exercised end-to-end |
| `paperbot/fa_block_test.py` | what-if-only validator: does the master accept a block order? | ⚠️ **result UNKNOWN — see §5** |
| `connections/clientids.py` | added `paperbot_accounts`=31, `paperbot_recon`=32, `paperbot_fa`=33 | ✅ |

Git: the whole desk is now a **local git repo** (first commit `3b29616`; this thread's
work committed on top). Commit after each change-set.

## 4. Current verified reality (from accounts.py + recon_report.py)
- **Master DF8922141** — advisor account ~$31.8k; NOT rebalanced.
- **5 client subs, each funded ~$1.07–1.11M paper, all enrolled & visible:**
  DU8922142 Conservative · DU8922143 Balanced · DU8922144 Balanced · DU8922145 Growth ·
  DU8922146 Growth.
- All 5 are essentially **all-cash** right now → recon shows a full initial rebalance for
  each. recon correctly **block-aggregated** the two Balanced and two Growth accounts with
  per-account share splits, and flagged a stray **GOOG** in DU8922143 as UNTRACKED→SELL.
- FA config on the gateway (`fa_probe.py`): one leftover **`test_group`** (DU142/143/144,
  method `ContractsOrShares`); PROFILES empty (build-983+ unification); aliases = all 6.

## 5. THE ONE OPEN VALIDATION (do this first)
`fa_block_test.py` was launched in the background and **stalled with no output** — almost
certainly the **FA-master account-stream hang** (see gotchas). Its result (does the master
accept an API block order?) is **not known**.
**Action:** run it **in the foreground** on the desktop and read the result:
```
C:\TradingDesk-Local\venv\Scripts\python.exe "…\TradingDesk\paperbot\fa_block_test.py"
```
If clientId 33 is "already in use," kill any stale `python.exe` running `fa_block_test`
first. If it hangs again, the connection pattern (non-readonly + `account=DU8922142` to
dodge the master hang) may need adjusting, or validate by targeting a single DU account.
**This what-if must pass before block execution is wired in.**

## 6. Next steps (the remaining build ladder)
1. **Confirm the block what-if** (§5).
2. **Create the 3 real per-tier FA groups** (Conservative/Balanced/Growth) on the paper
   gateway via `replaceFA`. NOTE: `replaceFA` takes the FULL groups XML and REPLACES all
   groups — include or intentionally drop the leftover `test_group`. This is a **shared-
   plumbing config write → serialize it; needs Andrew's OK (see DECISIONS).**
3. **Wire block routing into a multi-account engine:** take recon_report's per-account
   orders → aggregate to blocks → `build_fa_block(faGroup=<tier>, faMethod="NetLiq")` →
   `what_if` each → (later) arm + place. Per-account direct orders for single-account
   true-ups.
4. **Scale risk_manager** to per-account caps + a master-level aggregate kill switch.
5. **Stamp version + per-account reasons into every ledger record** (`version.stamp()`).
6. **Arming gate / staged rollout:** dry-run → what-if → ONE DU account → all five.

## 7. Dependency with the FLATTEN task (coordinate!)
DECISIONS.md has an ANSWERED item: **flatten ALL DU accounts to a blank slate** (sell
every leftover position, incl. the GOOG in DU143 and the 1 test PDBC share in DU142).
That flatten should complete **before** the first Option B block rebalance (so we invest
from zero). Same files/gateway → **serialize order placement** between the two.

## 8. Gotchas (hard-won — respect these)
- **FA master DF8922141 rejects direct/unallocated orders AND hangs on its account-update
  stream.** Always connect with an explicit `account=DU…` to dodge the hang. Block orders
  go through the master via `faGroup` (no single account on the order).
- **clientId registry** (`connections/clientids.py`): engine=30, accounts=31, recon=32,
  fa=33. Never collide; running engine can hold 30.
- **Background runs buffer stdout** — run paperbot scripts in the foreground so you see
  output (that's why §5's result was lost). Memory lesson: never leave a long/risky op
  running blind.
- **Paper gateway hard read-only lock is OFF**; the *software* `READONLY`/`DRY_RUN` flags
  + `armed` are the real control. `paperbot/arming.py` flips the gateway lock if needed.
- **Gateway isn't always up between runs**; `connections/ibkr.py` auto-launches it
  (needs `java_version=17`, already baked in). Expect a couple of ConnectionRefused
  retries on cold start.
- **replaceFA is destructive** (full-XML overwrite); read current config first.
- **cashflows.SCHEDULE is empty** by design — reserves read 0 until Andrew supplies data.

## 9. How to run things (desktop, foreground)
```
PY="C:\TradingDesk-Local\venv\Scripts\python.exe"
$PY "…\TradingDesk\paperbot\accounts.py"        # who's there + enrollment check
$PY "…\TradingDesk\paperbot\recon_report.py"     # the daily drift/rebalance readout
$PY "…\TradingDesk\paperbot\fa_probe.py"         # read FA group config
$PY "…\TradingDesk\paperbot\fa_block_test.py"    # validate block order (DO §5)
```
Memory (shared brain) for project facts/lessons:
`C:\Users\andre\.claude\projects\C--Users-andre-My-Drive--andrew-surberhc-com--TradingDesk\memory\MEMORY.md`
(esp. `ibkr-model-portfolio-api-limit.md`, `git-version-tracking.md`).
