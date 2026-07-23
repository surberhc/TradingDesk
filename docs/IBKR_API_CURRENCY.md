# IBKR API CURRENCY — the living record

**Owner:** the desk. **Governing rule:** `CLAUDE.md` § *IBKR currency — build against what IBKR does now*.
**Last reviewed:** 2026-07-23.

---

## 1. Purpose and cadence

IBKR ships builds continuously, removes API surfaces on its own schedule, and raises the
minimum supported TWS/Gateway floor without asking us. A desk built once against whatever
the API did on the day it was written rots silently — the failure mode is a rejected or
mis-allocated order, discovered live. This file is the standing answer to "what do we
actually depend on, is it still current, and what is on a clock."

**Cadence: quarterly, plus event-triggered.** Event triggers: any TWS/Gateway build change,
any IBC upgrade, any `ib_async` bump, and any new IBKR surface the desk starts using.

Every review must actually check:

1. **Release notes since the last review** — TWS API production *and* beta — for removals,
   deprecations, and behavior changes.
2. **Our versions vs IBKR's supported floor** — Gateway build, IBC, `ib_async`. IBKR keeps
   pushing the minimum up; a pinned old build is a shrinking window, not a plan.
3. **Every row of §2** — still supported, still behaving the way §3 records.
4. **Whether anything new obsoletes a workaround in §3** — the workarounds here exist
   because of a limitation on a specific build; limitations get fixed.

Findings land in §3 with dated evidence and a source, and the review gets a row in §5.
**A version or behavior change that touches order routing is an order-affecting change** —
bump `paperbot\version.py` VERSION + add a CHANGELOG line, per `CLAUDE.md`.

**Evidence labels used throughout:**

| Label | Means |
|---|---|
| **VERIFIED** | Confirmed on the date shown by reading the file/venv/log named, or by a live observation named. |
| **RECORDED** | Asserted in this repo by a prior session, with its source cited here; not re-derived on the review date. |
| **UNCONFIRMED** | Believed but not checked. Treat as a to-do, not a fact. |

---

## 2. WHAT WE CURRENTLY DEPEND ON

### 2a. Versions

| Surface | Version in use | Evidence | Status |
|---|---|---|---|
| Client library | **ib_async 2.1.0** | `pip list` + `ib_async.__version__` in `C:\TradingDesk-Local\venv` (`...\Lib\site-packages\ib_async\`), 2026-07-23 | **VERIFIED** 2026-07-23 |
| Async event lib | aeventkit 2.1.0 | same venv listing | **VERIFIED** 2026-07-23 |
| Loop patch | nest-asyncio 1.6.0 | same venv listing | **VERIFIED** 2026-07-23 |
| Python | 3.12.9 (venv) | `python -V` from the venv interpreter | **VERIFIED** 2026-07-23 |
| TWS API wire protocol | ib_async negotiates server versions **157–178** (`MinClientVersion` / `MaxClientVersion`) | `venv\Lib\site-packages\ib_async\client.py` lines 88–89 | **VERIFIED** 2026-07-23 |
| IB Gateway build | **10.45.1g, dated May 27 2026** (installer 4.128, standalone) | `C:\Jts\ibgateway\1045\launcher.log`: `Build 10.45.1g, May 27, 2026`; `C:\Jts\ibgateway\1045` is the only build dir present | **VERIFIED** 2026-07-23 |
| Gateway major pinned by launchers | `TWS_MAJOR_VRSN=1045` | `C:\IBC\StartGateway.bat`, `C:\IBC-Live-Data\StartGatewayLiveData.bat`, `C:\IBC-Live-Trade\StartGatewayLiveTrade.bat` — all three | **VERIFIED** 2026-07-23 |
| IBC (IBController) | **3.24.0** | `C:\IBC\version` contains `3.24.0`. Matches the version asserted in `connections\connections\ibkr_paper.py` `_gateway_env()` docstring | **VERIFIED** 2026-07-23 (the docstring claim is now corroborated, not just repeated) |
| Java runtime under the Gateway | 17.0.16.0.101 | same `launcher.log` | **VERIFIED** 2026-07-23 |

**Note on the Gateway number:** `1045` is the install/major directory; `10.45.1g` is the
actual point build. Both matter — IBKR's release notes are keyed on `10.4x`, and IBC's
`TWS_MAJOR_VRSN` is keyed on `1045`. All three Gateway instances (paper 4002, live-data
4001, live-trade 4003) share one IBC install (`C:\IBC\IBC.jar`) and one `C:\Jts` Java
install, differing only in settings dir and port — **VERIFIED** by reading the three `.bat`
launchers 2026-07-23. So a Gateway or IBC upgrade moves **all three at once**; there is no
per-instance pin. That is an event trigger for this review, and a risk in its own right.

### 2b. IBKR API surfaces this repo actually uses

Established by grepping the repo 2026-07-23, not by listing what IBKR offers.

| Surface | Calls / fields used | Where |
|---|---|---|
| **FA allocation config** | `requestFA(1)` (GROUPS read), `requestFA(2)` (PROFILES — errors on 983+, caught), `requestFA(3)` (ALIASES), `replaceFA(1, xml)` (destructive full-XML overwrite) | `paperbot\fa_probe.py`, `paperbot\rebalance_execute.py`, `paperbot\nightly_monitor_run.py` |
| **FA order allocation** | Order fields `faGroup`, `faMethod` (always `""` on block orders); group-stored `ContractsOrShares` governs the split | `paperbot\order_router.py` (`_base_fields`, `build_fa_block`), `paperbot\rebalance_engine.py` |
| **Order types** | `LMT` (DAY and GTC), `MIDPRICE`, `LMT` + `algoStrategy="Adaptive"` with `TagValue("adaptivePriority", Patient\|Normal\|Urgent)`, `REL` with `auxPrice` cap | `paperbot\order_router.py` |
| **Order plumbing** | `placeOrder`, `cancelOrder`, `orderRef` (deterministic idempotency key), `tif`, `outsideRth` (left False deliberately), `transmit` | `paperbot\order_router.py` |
| **Server-side risk seam (built, NOT wired)** | `PriceCondition`, `TimeCondition`, `conditions` / `conditionsCancelOrder`, `ocaGroup` / `ocaType` | `paperbot\order_router.py` §S5 seam |
| **Market data** | `reqMktData` (snapshot + streaming), `reqTickers`, `reqMarketDataType` (1 live / 2 frozen / 3 delayed), `reqHistoricalData`, `reqSecDefOptParams`, `reqContractDetails`, `qualifyContracts` | `paperbot\live_quotes.py`, `datacollector\*`, `livebot\s8_*`, `connections\*` |
| **Executions / positions / account** | `reqExecutions(ExecutionFilter())`, `reqAllOpenOrders`, `positions` / `reqPositions`, `accountSummary`, `accountValues`, `reqAccountUpdatesMulti`, `reqPnL`, `managedAccounts` | `paperbot\order_router.py` (dedup gate), `paperbot\*`, `dashboard\app.py` |
| **Scanner** | `reqScannerParameters` | `canslim\` research only |
| **`whatIfOrder`** | **Present in `order_router.what_if()` but called on NO live path** — see §3.1 | `paperbot\order_router.py` |

---

## 3. KNOWN CONSTRAINTS AND GOTCHAS

Each entry carries how it was established. Entries marked *verified live 2026-07-23* were
observed on the **paper** account (FA master `DF8922141`, port 4002) during the FA group
block test — see `docs\HANDOFF_2026-07-23_FA_gateway_test.md` and
`docs\CRM_DESIGN_groups_brain.md` §12. Those live observations were made by that session;
this file records them, it did not re-run them.

### 3.1 What-if is unusable for FA allocation orders — there is NO pre-trade margin gate for FA blocks
**VERIFIED by live observation, 2026-07-23, paper account.**
`ib_async`'s `whatIfOrder` sets `whatIf=True` but does **not** set `transmit`. IBKR responds
with **error 321 — "What-If order should have transmit flag set to TRUE"** and then never
resolves the future, so the call **hangs indefinitely**. Setting `transmit=True` alongside
`whatIf=True` makes the FA master return **nothing at all**.

Consequence: **there is no pre-trade margin/commission check available for an FA block
order.** Any margin gate must come from elsewhere (account balances / margin pulled
pre-tranche, per the CRM design's two-tier cadence). Corroborating: `order_router.py`
already states "whatIfOrder is NEVER used (known hang)" in four separate docstrings and
never calls it on a live path — the hang predates today; today's test established *why*.

> Watch for a fix: this is exactly the class of thing that a `ib_async` bump or a Gateway
> build could silently resolve. Re-test `whatIfOrder` on an FA block at every review.

### 3.2 Allocation-order executions come back ONLY at the FA master level
**VERIFIED by live observation, 2026-07-23, paper account.**
IBKR returns executions for an allocation (group/block) order **only at the FA master
account**. `reqExecutions` filtered per subaccount returned **nothing** for all six managed
accounts.

Consequence: **per-account fill proof cannot come from `reqExecutions`.** It must come from
positions / `avgCost`, or from Flex statements. Any reconciliation design that assumes
per-subaccount execution records is wrong on this build.

### 3.3 `faMethod=""` is the documented path, not a workaround
**RECORDED** (IBKR documentation, cited by a prior session in `docs\CRM_DESIGN_groups_brain.md`
§ GroupDef; not re-fetched 2026-07-23).
When the group carries a `defaultMethod`, leaving `faMethod` blank makes IBKR use it:
*"If specifying actual group name and the faMethod is blank/omitted the default method of
that group will be used."*
— <https://interactivebrokers.github.io/tws-api/financial_advisor_methods_and_orders.html>

Related, same source area: an **order-level `faMethod="NetLiq"` is rejected (Error 10226)** —
**RECORDED** from `order_router._base_fields` and the `rebalance_engine` module docstring.
Also documented: there is no default account allocation for the API; it must be specified
on every order.

### 3.4 `ContractsOrShares` is a profile-style method; groups and profiles are unified on 983+
**RECORDED** (IBKR documentation, cited in `docs\CRM_DESIGN_groups_brain.md`; not re-fetched
2026-07-23).
`ContractsOrShares` is **not** one of the documented *group* methods (EqualQuantity, NetLiq,
AvailableEquity, PctChange) — it is a *profile* method. Under TWS build **983+**, *"Use
Account Groups with Allocation Methods"*, groups and profiles are **unified**:
`requestFA`/`replaceFA` accept **Group only** (Profile errors — which is what `fa_probe.py`
observes and catches), while `placeOrder` accepts a profile name in `faGroup`.
— <https://interactivebrokers.github.io/tws-api/financial_advisor.html>

The live paper groups already carry `defaultMethod=ContractsOrShares` (**VERIFIED**
2026-07-23 read-only recon, per the handoff), so no method change is needed — the executor
writes the per-account split into the group config via `replaceFA` and sends the block with
`faMethod=""`.

### 3.5 `ib_async` 2.1.0 does NOT implement the newer `OrderAllocation` mechanism
**VERIFIED 2026-07-23** by introspecting the installed class and grepping the installed
package.
`ib_async.Order`'s allocation-related dataclass fields are exactly:
`faGroup`, `faProfile`, `faMethod`, `faPercentage`. There is **no `OrderAllocation` symbol
anywhere in `venv\Lib\site-packages\ib_async\`.**

Consequence: the desk is on the **legacy FA field path** whether or not IBKR has moved on.
This is the single most likely place for a future `ib_async` bump to change behavior, and
it sits directly on the order path — so a bump here is an **order-affecting change**.

### 3.6 `replaceFA` is a destructive full-XML overwrite
**RECORDED** from `paperbot\rebalance_execute.py` (`requestFA(1)` returns the full groups
XML; `replaceFA(1, xml)` replaces the full set). The code already backs up to
`fa_groups_backup.xml` and merges rather than rewriting. Never call it without the backup.

### 3.7 Order-type constraints the router encodes
**RECORDED** from `paperbot\order_router.py` (docstrings cite a live DU142 run) and
`docs\IBKR_ORDER_TYPES_RESEARCH.md`:
- **MIDPRICE** is US stock/ETF only (never options) and **DAY-only**; IBKR rejects it
  outside RTH with **Warning 321** *"Midprice orders are not supported outside of regular
  trading hours"*. An `outsideRth=True` flag makes an order eligible outside RTH even when
  placed *during* RTH — which is why every builder now leaves `outsideRth` at False.
- **Adaptive forbids GTC** — TIF is forced to DAY.
- The **resting remainder must be a plain LMT** (only a plain LMT can carry GTC).
- **REL** is the options-safe dynamic peg (used for SPXW) since MIDPRICE is unsupported there.

**Note on error 321:** IBKR uses 321 as a generic request-validation code — it carries both
the what-if `transmit` message (§3.1) and the MIDPRICE-outside-RTH message. Never key logic
on the numeric code alone; read the message text.

### 3.8 DEPRECATION EXPOSURE — tracked, with clocks

| Surface | Exposure | Our position | Evidence |
|---|---|---|---|
| `reqFundamentalData`, `cancelFundamentalData`, `fundamentalData` callback, `FUNDAMENTAL_RATIOS` tick 47 | **REMOVED in TWS API v10.47** (2026 production release note dated 2026-05-29). API fundamentals doc marked deprecated, **no migration path offered.** | Our Gateway is **10.45.1g** — pre-removal, so it still works *today*. **No repo code calls `reqFundamentalData`** (grep, 2026-07-23), so nothing breaks on upgrade. The exposure is forward: IBKR fundamentals are **not a durable foundation** for the CANSLIM screen. | `canslim\research\ibkr_api_capabilities.md` §2 (**RECORDED**, release note not re-fetched 2026-07-23); Gateway build **VERIFIED** |
| FA `requestFA` PROFILES (type 2) | De-supported on build **983+** | Already handled — `fa_probe.py` catches the error and moves on | `paperbot\fa_probe.py` (**RECORDED**) |
| Legacy `faGroup`/`faMethod` order fields (§3.5) | Newer `OrderAllocation` mechanism exists upstream in IBKR's API; `ib_async` 2.1.0 does not expose it. Whether IBKR has deprecated the legacy fields is **UNCONFIRMED** — check at the next review. | Entire block-order path depends on the legacy fields | §3.5 (**VERIFIED**) / deprecation status **UNCONFIRMED** |
| Minimum supported Gateway floor | IBKR raised the minimum to **10.30 in March 2025** and keeps raising it — a pinned build is a shrinking window, not a strategy | Pinned at `TWS_MAJOR_VRSN=1045` across all three instances; one upgrade moves all three | `canslim\research\ibkr_api_capabilities.md` §2 (**RECORDED**) |

---

## 4. MONITORING CHANNELS

> **TODO — INCOMPLETE.** This list is being populated from an in-flight research pass. It is
> **not** final; treat it as the known-authoritative subset, not the whole answer. Do not
> mark this section done until the research pass lands and the gaps below are filled.

Known authoritative today:

- **TWS API reference** — <https://interactivebrokers.github.io/tws-api/> (the canonical
  behavior reference; already the citation source for §3.3 / §3.4)
- **IBKR Campus — API section** — <https://www.interactivebrokers.com/campus/ibkr-api-page/>
  (order types, market-data subscriptions, lessons. Note: several Campus pages return
  **HTTP 403 to automated fetchers** — see `docs\IBKR_ORDER_TYPES_RESEARCH.md`; plan for
  manual reading or search snippets.)
- **TWS API release notes — production** — <https://ibkrguides.com/releasenotes/api/tws/>
  (this is where the `reqFundamentalData` removal appeared)
- **TWS API release notes — beta** — same host, beta channel (early warning: what lands in
  production next)

Known gaps to fill in the research pass:
- Whether IBKR publishes a machine-readable changelog/feed we can poll, so the quarterly
  review is a diff rather than a re-read.
- The IBC (IBController) release channel — IBC is a third-party project on its own cadence
  and is a hard dependency of every Gateway launch here. **UNCONFIRMED** where its release
  notes live.
- The `ib_async` release channel and its changelog — the library, not IBKR, is what actually
  gates which fields we can set.
- IBKR's deprecation-announcement channel (if distinct from the release notes).

---

## 5. REVIEW LOG

| Date | Trigger | What was checked | Outcome |
|---|---|---|---|
| **2026-07-23** | Initial — standing practice established (`CLAUDE.md` § *IBKR currency*) | Venv introspected (`ib_async` 2.1.0, aeventkit 2.1.0, nest-asyncio 1.6.0, Python 3.12.9, wire protocol 157–178). Gateway build read from `C:\Jts\ibgateway\1045\launcher.log` (**10.45.1g**, May 27 2026) and the `TWS_MAJOR_VRSN=1045` pin confirmed in all three IBC launchers. IBC **3.24.0** confirmed from `C:\IBC\version`. Repo grepped for the IBKR surfaces actually used (§2b). `Order` dataclass introspected: **no `OrderAllocation`**, legacy FA fields only. | Doc created. §3 seeded with two **live-verified** paper-account findings from the same-day FA block test: **what-if is unusable for FA allocation orders (error 321 + indefinite hang) — no pre-trade margin gate exists for FA blocks**; and **allocation executions return only at the FA master, never per subaccount**. Deprecation table opened; `reqFundamentalData` removal at **10.47** is the live clock (we are on 10.45.1g, and no repo code calls it — forward exposure only). §4 left explicitly **incomplete**. |

<!-- Next review due: 2026-10-23 (quarterly), or immediately on a Gateway/IBC/ib_async change. -->
