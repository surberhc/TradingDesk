# Handoff — IBKR sleeve architecture: FA group block test (2026-07-23)

**Resume:** read `docs/CRM_DESIGN_groups_brain.md` (esp. **§13** — the verified test results — then
**§12**) and `docs/HANDOFF_2026-07-21_CRM_groups.md`, then this file. Architecture **Option A** is
LOCKED (FA Account Groups + CRM-as-brain; Model Portfolios dead for automation).

> **STATUS AS OF END OF 2026-07-23: the load-bearing test is DONE and PASSED.** The earlier
> "NEXT ACTION" list in this file (add the group member, then run Test 0) has been **executed** —
> it is replaced by the "What genuinely remains" list at the bottom. Do not re-run it.

---

## 1. HEADLINE — the FA block execution architecture is VALIDATED

Both halves of the §10.4 load-bearing test ran **live on the PAPER account** (FA master
`DF8922141`, port 4002) on 2026-07-23 with **real orders and real fills** — not what-ifs.

**Full evidence lives in `docs/CRM_DESIGN_groups_brain.md` §13.** Summary:

| Test | Result | One-line evidence |
|---|---|---|
| **Test 0 — single-leg FA block** (§13.1) | **PASSED** | `BUY 2 PDBC` on `faGroup="Balanced"`, `faMethod=""` → ONE block fill (execId `00025b49.6a697500.01.01`, 2 @ 18.14). `DU8922143` and `DU8922144` each got **1 share**, both `avgCost` 18.3244; master flat. Zero rejections, **no error 10226**. |
| **Multi-leg COMBO on an FA block** (§13.2) — *the genuine open unknown* | **PASSED** | SPY 20260814 739C/740C vertical, BUY 2 spreads, LMT 0.68 → filled 2 @ 0.66 in ~11 s, zero errors. Both accounts each got **+1 739C / −1 740C at identical avgCosts**; master netted flat. |

**Consequence: spread-based sleeves (S8) do NOT need per-account routing.** A combo rides an FA
block and both legs allocate coherently. `docs/CRM_DESIGN_groups_brain.md` §10 item 4 is CLOSED and
§11 gap 8 is RESOLVED.

*Scope of the claim (stated honestly):* proven for a **plain LMT** 1:1 two-leg **equity**-option
vertical on a **2-account** group. **Not** proven for MIDPRICE/Adaptive (`config.LADDER_FA_BLOCKS`
is still `False`), **not** proven for SPX/SPXW index options specifically, **not** proven at larger
group sizes.

## 2. Two limitations the same session exposed (both change the design)

These do **not** invalidate the architecture; they change *how we reconcile* and *how we pre-check*.
Both are already written into the CRM design doc.

### 2.1 IBKR returns allocation-order executions ONLY at the FA master (§13.3)

`reqExecutions(ExecutionFilter(acctCode=…))` was run **individually for all six managed accounts**
and returned records for **`DF8922141` only** — nothing for any `DU…` subaccount. IBKR's docs say
per-subaccount `reqExecutions` *should* work; on this paper FA it returned nothing. **Whether that
is paper-FA-specific or general is UNCERTAIN — untested on a live FA.**

**Therefore:** treat the API as a **dead end for per-account execution records.** Per-account proof
comes from **`reqPositions` + `avgCost`** (which also carries each account's pro-rata commission
share), or from a **Flex / activity statement**. Design reconciliation around positions + Flex.
Applied to §6 step 8, §7.1, §7.2, §11 gap 2 of the design doc.

### 2.2 There is NO what-if / margin-preview gate for FA block orders (§13.4)

- With `transmit=False`: `ib_async`'s `whatIfOrder` sets `whatIf=True` but **not** `transmit`; IBKR
  replies **error 321** (*"What-If order should have transmit flag set to TRUE"*) and then **never
  resolves the future** → `ib.whatIfOrder()` **hangs forever**. This is a **real bug affecting
  `order_router.what_if()` for ANY `transmit=False` order**, FA or not. Needs a hard timeout.
- With `transmit=True`: the FA master returns **nothing at all** for a group order (tested at 30 s
  and 90 s deadlines, zero error events). No IBKR doc claims what-if support for allocation orders.

**Therefore: placing is the only way to validate a block.** Any design assuming a what-if pre-trade
gate in front of a block is revised out; the margin/buying-power pre-check must be **entirely
self-computed** from `reqAccountSummary` tags. Applied to §6 step 6 and §11 gap 6.

**Knock-on:** `paperbot\fa_block_test.py`'s recorded result (*"the FA master ACCEPTS a group order,
what-if only"*) is **SUSPECT** — it used exactly this broken path on a `transmit=False` order, which
would have hung rather than returned an acceptance. Flagged for re-verification or retirement
(§13.5). Its question is now answered far more strongly by an actual fill.

## 3. Documented-language confirmations (worth citing, not re-deriving)

- **`faMethod=""` is the DOCUMENTED path, not a workaround:** *"If specifying actual group name and
  the faMethod is blank/omitted the default method of that group will be used."*
  — <https://interactivebrokers.github.io/tws-api/financial_advisor_methods_and_orders.html>
- **`ContractsOrShares` is a PROFILE-style method**, not one of the documented *group* methods
  (EqualQuantity / NetLiq / AvailableEquity / PctChange). Under TWS build 983+ *"Use Account Groups
  with Allocation Methods"*, groups and profiles are **unified**: `requestFA`/`replaceFA` accept
  **Group only** (Profile errors — which matches what `fa_probe.py` observed), and `placeOrder`
  accepts a profile name in `faGroup`.
  — <https://interactivebrokers.github.io/tws-api/financial_advisor.html>
- *"Unlike in TWS, there is not a default account allocation for the API — it must be specified with
  every order placed."*

## 4. OPEN DECISION for Andrew — `OrderAllocation` vs `replaceFA`-per-block (§13.6)

**Surfaced, deliberately not decided.** Newer TWS API exposes an **`OrderAllocation`** class —
explicit per-account allocations attached to a single order, no config mutation. **Our installed
`ib_async` 2.1.0 does NOT implement it** (verified: `Order` exposes only `faGroup`, `faProfile`,
`faMethod`, `faPercentage`).

So today's pattern — **`replaceFA` the GROUPS XML before every block** — puts a **shared-config
write in the hot path of every order**. The alternative requires patching or replacing the client
library. Trade-off table in design §13.6. **Andrew's call.**

---

## 5. Prior context that still stands

### 5.1 FA group inventory (read-only recon, 2026-07-23)

| Group | Member(s) | defaultMethod |
|---|---|---|
| Balanced | DU8922143 **+ DU8922144 (added this session)** | ContractsOrShares |
| Conservative | DU8922142 | ContractsOrShares |
| Growth | DU8922145 | ContractsOrShares |

**XML tag casing CONFIRMED** (`Group / name / defaultMethod / ListOfAccts varName="list" / Account /
acct / amount`) — resolves the MONDAY_RUNBOOK "casing unconfirmed offline" flag.
`set_group_contracts_or_shares` tag handling matches.

### 5.2 BIG operational discovery (load-bearing, unchanged)

**The IBKR paper account/login is SHARED by multiple people.** Daytime lockouts = multi-user
contention on the single paper login (one-login-per-username), NOT merely data-entitlement
contention. HARD RULE: during market hours (before ~15:00 CT) do NOT disrupt other users — no
killing paper-gateway processes, no login takeover, no relaunch (a relaunch competes for the one
login and kicks whoever is on it). Read-only investigation is OK. Own-the-gateway work
(relaunch / FA writes / arming) only AFTER 3 PM CT. **Architecture implication (→ conductor log
entry #80):** the desk can't reliably own the paper gateway during market hours; real unattended
automation likely needs its OWN dedicated IBKR login. See memory `paper-account-after-3pm-ct`.

### 5.3 Gateway ops notes

- IBC config (`C:\IBC\config.ini`): `ReadOnlyApi` must be `no` for the `replaceFA` write (it was
  flipped this session); `ExistingSessionDetectedAction=manual`, TradingMode=paper, port 4002.
- A clean IBC launch (`connections.ibkr_paper.ensure_gateway()`) DID auto-accept the paper-trading
  API disclaimer (the recurring Error 10141 blocker) — the automated path works when IBC owns the
  launch (manual logins are what caused the dueling-session mess).
- clientIds seen this session: 33 = paperbot_fa (read), 35 (block fill test), 36 = paperbot_fa_admin
  (write).
- **Live gateway = port 4003 (S8 zero-transmit pilot; daily 08:15 CT restart) — NEVER touch.**

### 5.4 Incident (this session)

**`paperbot\arming.py arm` killed the S8 LIVE-pilot Gateway on port 4003** (2m51s outage,
09:55:17→09:58:08 CDT) — the paper kill routine's `dir_substring=r"C:\IBC"` is a string **prefix**
of `C:\IBC-Live-Trade`. Zero-transmit wall never breached. Full writeup:
`docs/INCIDENT_2026-07-23_arm_restart_killed_live_gateway.md`. **The fix LANDED** (conductor **#46**,
commit `de7a999` — instance-exact discriminator + never-kill guard). Conductor **#47** (orphan `java`
pid 29236 cleanup + the unresolved 4002 disappearance) is still open.

---

## 6. What genuinely remains

Ordered roughly by dependency, not by size.

1. **Fix `order_router.what_if()`'s infinite hang** — no timeout on `ib.whatIfOrder`; hangs on any
   `transmit=False` order (§2.2). **Conductor #48.**
2. **Re-verify or retire `paperbot\fa_block_test.py`** — its recorded acceptance is suspect (§2.2).
   Retiring is the likely right call now that a real fill supersedes it. **Conductor #49.**
3. **Andrew's decision: `OrderAllocation` vs `replaceFA`-per-block** (§4). Blocks nothing today
   (the current path is proven) but shapes the executor's design. **Conductor #50.**
4. **Build the position-delta attribution path** — §13.3 means the sleeve ledger needs a
   before/after `reqPositions` snapshot per block, plus a Flex EOD cross-check. This replaces the
   passive `execDetails` listener the design originally assumed. Still the biggest to-build piece
   (design §9 to-build #1, §11 gaps 2/3).
5. **Self-computed margin/buying-power pre-check** — no broker preview exists (§2.2); §6 step 6 of
   the design must be built from `reqAccountSummary` tags alone (design §11 gap 6).
6. **Not-yet-proven extensions of the block path** (nice-to-have, not blockers): FA-block ×
   MIDPRICE/Adaptive (`config.LADDER_FA_BLOCKS=False`); SPX/SPXW index-option combos specifically;
   groups larger than 2 accounts.
7. **Group-membership sync** — `set_group_contracts_or_shares` edits amounts, not durable membership
   (design §11 gap 4). This session added `DU8922144` by hand via that path; the CRM needs a
   deliberate membership write.
8. **Conductor #80 (log entry): gateway unattended-resilience hardening** — re-examine the disabled
   `GatewayWatchdog`, root-cause the shared-login contention, fail-closed + off-machine alerts.
9. **Still-pending Andrew decisions carried forward:** transport JSON-vs-DB (SQLite recommended);
   overlay-tier weights (frozen, needs out-of-sample/per-regime validation per rule #1).

Conductor items **#42** (CRM) and **#43** (gateway test) advanced.
