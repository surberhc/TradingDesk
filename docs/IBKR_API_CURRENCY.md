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

### 3.1 What-if hang on FA allocation orders — **REVISED 2026-07-23** (client bug, NOT an IBKR limit)

**Original observation (live, 2026-07-23, paper account):** `ib_async`'s `whatIfOrder` sets
`whatIf=True` but does **not** set `transmit`. IBKR responds with **error 321 — "What-If order should
have transmit flag set to TRUE"** and then never resolves the future, so the call **hangs
indefinitely**. Setting `transmit=True` alongside `whatIf=True` was observed to make the FA master
return **nothing at all**. The original conclusion recorded here was: *"there is no pre-trade
margin/commission check available for an FA block order."*

**Corrected understanding (research 2026-07-23):** that conclusion was a **test artifact, not an IBKR
limitation.** The hang is an **`ib_async` 2.1.0 CLIENT bug**, not a missing IBKR capability:
`whatIfOrder` sets `whatIf=True` but not `transmit`, which trips IBKR error 321 and leaves the future
unresolved. On the wire, IBKR's `whatIf`/preview decode **does** carry an `accountsCount`-length
`OrderAllocation` array — a genuine **per-account preview** — gated by
`MIN_SERVER_VER_FULL_ORDER_PREVIEW_FIELDS`, **which our Gateway 10.45.1g satisfies**. So a pre-trade
margin/allocation gate for FA blocks **does exist**; it is unreachable only through `ib_async`'s
`whatIfOrder` helper.

- **Confidence — the wire protocol carries a per-account preview array on our build: HIGH** (documented;
  the preview-fields server-version gate is met by 10.45.1g).
- **Confidence — the hang is specifically the `ib_async` `whatIf`-without-`transmit` client bug: HIGH**
  (matches IBKR error 321's own message text and the observed non-resolution).

**Consequence (revised):** don't design around "no FA margin gate exists." A per-account margin/
allocation preview is reachable via the **official `ibapi` (or a port), or a patched transmit flag** on
the what-if copy — see the new conductor follow-up. `order_router.py` already avoids `whatIfOrder` on
every live path (four docstrings note the known hang), so nothing live regresses; this only reopens the
*possibility* of a pre-trade gate that the original note had written off. Cross-reference the what-if-hang
conductor items **#26** and **#48** (the latter is the client-side timeout fix for the same hang).

> Still re-test at every review: a `ib_async` bump could fix the helper directly and make the preview
> reachable without leaving `ib_async`.

### 3.2 Per-subaccount executions — **REVISED 2026-07-23** (likely a master-client-id artifact, NOT an IBKR limit)

**Original observation (live, 2026-07-23, paper account):** for an allocation (group/block) order,
`reqExecutions` filtered per subaccount returned **nothing** for all six managed accounts; only the FA
master (`DF8922141`) records came back. The original conclusion recorded here was: *"per-account fill
proof cannot come from `reqExecutions` — it must come from positions/`avgCost` or Flex statements; any
reconciliation design assuming per-subaccount execution records is wrong on this build."*

**Corrected understanding (research 2026-07-23):** per-subaccount executions **ARE** obtainable via the
API. `reqExecutions` with an `acctCode`/`execFilter` returns per-subaccount `Execution` records
(`Execution.acctNumber`) — **BUT receiving *all clients'* execution/commission records requires
connecting as the MASTER CLIENT ID.** Today's test connected as **clientId 35** (`paperbot_fa_block`),
**not** the master client id — which is the most likely reason we saw master-only records and (wrongly)
concluded the API was a dead end for per-account fills. Source:
<https://interactivebrokers.github.io/tws-api/executions_commissions.html>.

- **Confidence — the API documentation (per-subaccount `Execution.acctNumber`; all-client records
  require the master client id): HIGH** (documented on the tws-api reference).
- **Confidence — that clientId-35-vs-master is *why* we saw master-only records here: STRONG HYPOTHESIS,
  NOT YET RE-TESTED.** This has **not** been re-run on our Gateway connecting as the master client id.
  It is well-supported by the documentation but remains unverified on our build.

**Consequence (revised):** do **NOT** design the reconciliation around Flex being *required* on the
strength of the original observation. **Re-test `reqExecutions` connecting as the master client id first**
(new conductor follow-up). Flex may still be *chosen*, but the API is not established as a dead end. The
positions/`avgCost` path remains a valid independent cross-check regardless.

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

### 3.5 `ib_async` 2.1.0 exposes only the legacy FA fields — **and that is fine (`OrderAllocation` is inbound-only)**
**VERIFIED 2026-07-23** by introspecting the installed class and grepping the installed
package.
`ib_async.Order`'s allocation-related dataclass fields are exactly:
`faGroup`, `faProfile`, `faMethod`, `faPercentage`. There is **no `OrderAllocation` symbol
anywhere in `venv\Lib\site-packages\ib_async\`.**

**Corrected framing (research 2026-07-23, HIGH confidence):** the earlier read — that `OrderAllocation`
is a "newer allocation *mechanism*" `ib_async` lacks — was misleading. `OrderAllocation` is an
**INBOUND-only** class on `OrderState`: the server *returns* it to describe an allocation (in
`whatIf`/order-preview and order state). It is **not** an outbound submission path. Fields: `Account`,
`Position`, `PositionDesired`, `PositionAfter`, `DesiredAllocQty`, `AllowedAllocQty`, `IsMonetary`.
Outbound allocation is still driven by `Order.faGroup`/`faMethod`/`faPercentage` — the exact fields
`ib_async` exposes. So `ib_async` 2.1.0 is **fully sufficient for the submission path we use.** The only
thing it cannot do is *read* the `OrderState.orderAllocations` preview array; a system that needs that
inbound read would use the official `ibapi` or a port. See §3.9 for the full research write-up. Added in
TWS API 10.33 (IBKR Campus changelog 2024-12-17); confirmed field-for-field by two independent
wire-protocol ports (scmhub/ibapi Go, wboayue/rust-ibapi).

Consequence: the desk is on the **`faGroup`/`replaceFA` submission path** — which is current, documented,
and un-removed, **not** a legacy path IBKR has superseded for submission. A future `ib_async` bump could
still change FA-field behavior, and it sits directly on the order path — so a bump here remains an
**order-affecting change** — but the earlier framing of this as "IBKR has moved on and we haven't" was
wrong.

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
| `faGroup`/`faMethod` order fields (§3.5) | **Not a deprecation exposure — REVISED 2026-07-23.** The `OrderAllocation` class is **inbound-only** (a description on `OrderState`), not a replacement submission path; `faGroup`/`faMethod`/`faPercentage` remain the current, documented outbound mechanism. **No FA-allocation breaking changes in the 2025 (10.35–10.42) or 2026 (10.43–10.48) production release notes**; last FA-relevant change was the 2022 v981/10.22 groups/profiles unification we are already on. | Entire block-order path depends on these fields — but they are current, not superseded | §3.5 / §3.9 (**research, HIGH confidence**) |
| Minimum supported Gateway floor | IBKR raised the minimum to **10.30 in March 2025** and keeps raising it — a pinned build is a shrinking window, not a strategy | Pinned at `TWS_MAJOR_VRSN=1045` across all three instances; one upgrade moves all three | `canslim\research\ibkr_api_capabilities.md` §2 (**RECORDED**) |

---

### 3.9 FA-allocation research findings — 2026-07-23 (deep, adversarially verified)

A dedicated research pass on 2026-07-23 (documentation + wire-protocol cross-checks; **no gateway
connection, no orders**) settled several questions the same-day live FA test had left ambiguous or
recorded wrongly. Confidence is labeled per claim. The two "known constraints" this pass corrected are
folded back into §3.1 and §3.2, marked **REVISED**; this subsection is the consolidated record with
citations.

1. **`OrderAllocation` is INBOUND-only — the §13.6 decision was largely a false choice. (HIGH confidence,
   documented.)** `OrderAllocation` is **not** an outbound order-submission mechanism. It is an inbound
   class on `OrderState` that the server *returns* to *describe* an allocation (in `whatIf`/order-preview
   and order state). Added in **TWS API 10.33** (IBKR Campus changelog dated **2024-12-17**). Fields:
   `Account`, `Position`, `PositionDesired`, `PositionAfter`, `DesiredAllocQty`, `AllowedAllocQty`,
   `IsMonetary`. There is **no "attach `OrderAllocation` to the order instead of `replaceFA`" path** —
   outbound allocation is still `Order.faGroup`/`faMethod`/`faPercentage`. Confirmed field-for-field by
   **two independent wire-protocol ports** (scmhub/ibapi in Go, wboayue/rust-ibapi in Rust) plus the
   Campus changelog. **Recorded decision: keep `faGroup`/`replaceFA` for submission** — it is current,
   documented, un-removed, and the path `ib_async` 2.1.0 supports. Do not switch libraries for a
   submission mechanism that does not exist. (See `docs\CRM_DESIGN_groups_brain.md` §13.6; conductor #50.)

2. **`ib_async` 2.1.0 has no `orderAllocations`. (HIGH confidence, verified in the venv.)** It exposes only
   `faGroup`/`faProfile`(obsolete)/`faMethod`/`faPercentage`. A Python system that ever needs to *read*
   the inbound `OrderState.orderAllocations` preview array would need the official `ibapi` (or a port);
   `ib_async` is fully sufficient for the `faGroup`/`replaceFA` submission path we use.

3. **Two of the same-day recorded limitations were TEST ARTIFACTS, not IBKR limits** — corrected in §3.1
   and §3.2:
   - **Per-subaccount executions ARE obtainable via the API** (`reqExecutions` with `acctCode`/`execFilter`
     → per-subaccount `Execution.acctNumber`), **but receiving all clients' records requires connecting
     as the MASTER CLIENT ID.** Today's test used **clientId 35** (`paperbot_fa_block`), not the master —
     the most likely reason we saw master-only (`DF8922141`) records. *API doc = HIGH confidence;
     "clientId-35-is-why" = STRONG HYPOTHESIS, NOT YET RE-TESTED on our gateway.* Source:
     <https://interactivebrokers.github.io/tws-api/executions_commissions.html>.
   - **`whatIf` per-account preview IS supported on our gateway. (HIGH confidence.)** The wire protocol
     carries an `accountsCount`-length `OrderAllocation` array inside the `whatIf`/preview decode, gated by
     `MIN_SERVER_VER_FULL_ORDER_PREVIEW_FIELDS`, which **10.45.1g satisfies**. Today's hang was the
     `ib_async` 2.1.0 client bug (`whatIfOrder` sets `whatIf=True` but not `transmit` → IBKR error 321 →
     future never resolves), **not** an IBKR limitation. A pre-trade margin/allocation gate for FA blocks
     **does exist**; it is unreachable only through `ib_async`'s `whatIfOrder` helper. Cross-ref conductor
     **#26** and **#48**.

4. **The `replaceFA` hot-path risk is addressed architecturally, not by a library swap.** Only rewrite a
   group's XML when its **MEMBERSHIP** changes, never per-order; serialize the writes (already the design
   intent). The per-order mutation exists only because `ContractsOrShares` encodes share counts as group
   config. **Open follow-up:** evaluate whether a stable allocation method (or `faPercentage`/order-size)
   removes the per-order `replaceFA` entirely without losing the CRM brain's explicit per-account control.

5. **No FA-allocation breaking changes in the 2025 (10.35–10.42) or 2026 (10.43–10.48) production release
   notes. (HIGH confidence.)** Last FA-relevant change was the **2022 v981/10.22** groups/profiles
   unification + `FADataType.PROFILES` / `faProfile` de-support — which we are already on. A **third config
   path** exists — Client Portal Web API `/iserver/account/allocation/*` (added Oct 2023) — but it is group
   **SETUP**, analogous to `replaceFA`, **not** per-order submission; note it as an alternative config
   surface, not a routing option (CRM design already rejects the CP Web API `/fa` path for now, §10 item 3).

---

## 4. MONITORING CHANNELS

> **POPULATED 2026-07-23.** The 2026-07-23 FA research pass validated the authoritative source
> set below (it is the same set that sourced §3.9). Kept as a living list — add a channel as we
> come to depend on it — but this is no longer a placeholder.

**Authoritative — validated by the 2026-07-23 research pass:**

- **IBKR Campus — TWS API changelog** — <https://www.interactivebrokers.com/campus/ibkr-api-page/tws-api-changelog-2/>
  (the dated, per-version change feed; this is where the `OrderAllocation`-added-in-10.33 entry
  dated 2024-12-17 was confirmed — §3.9). The **primary** channel for "what changed and when."
- **TWS API reference (GitHub docs)** — <https://interactivebrokers.github.io/tws-api/> (the
  canonical behavior reference; citation source for §3.2 executions, §3.3 / §3.4, and §3.9).
- **Annual production release-notes pages** — the per-year TWS API production release notes
  (2025 = 10.35–10.42, 2026 = 10.43–10.48 were both read for §3.9; also
  <https://ibkrguides.com/releasenotes/api/tws/>, where the `reqFundamentalData` removal appeared).
  Read the year's page each review for removals/deprecations/behavior changes.
- **IBKR Campus — API section** — <https://www.interactivebrokers.com/campus/ibkr-api-page/>
  (order types, market-data subscriptions, lessons. Note: several Campus pages return **HTTP 403
  to automated fetchers** — see `docs\IBKR_ORDER_TYPES_RESEARCH.md`; plan for manual reading or
  search snippets.)
- **TWS API release notes — beta** — same host, beta channel (early warning: what lands in
  production next).

**Still open (not blocking — the set above is sufficient for the quarterly review):**
- Whether IBKR publishes a *machine-readable* changelog/feed we can poll, so the review is a diff
  rather than a re-read. (The Campus changelog is the best human-readable diff today.)
- The IBC (IBController) release channel — IBC is a third-party project on its own cadence and a
  hard dependency of every Gateway launch here. **UNCONFIRMED** where its release notes live.
- The `ib_async` release channel and its changelog — the library, not IBKR, is what actually gates
  which fields we can set (and, per §3.1, whether the `whatIfOrder` hang gets fixed upstream).
- IBKR's deprecation-announcement channel, if distinct from the release notes / Campus changelog.

---

## 5. REVIEW LOG

| Date | Trigger | What was checked | Outcome |
|---|---|---|---|
| **2026-07-23** (research pass) | FA-allocation deep research (documentation + wire-protocol cross-checks; no gateway, no orders) | IBKR Campus TWS API changelog, tws-api GitHub docs, 2025/2026 production release notes, and two independent wire-protocol ports (scmhub/ibapi Go, wboayue/rust-ibapi) reviewed for FA allocation + `OrderAllocation` semantics. | New **§3.9** research-findings subsection added. Established `OrderAllocation` is **inbound-only** (added 10.33, 2024-12-17) → §13.6 decision resolved to keep `faGroup`/`replaceFA` (conductor #50 closed). **Corrected two same-day "known constraints" as test artifacts:** §3.1 (what-if hang is an `ib_async` client bug, not a missing IBKR margin gate — per-account preview exists on 10.45.1g; HIGH) and §3.2 (per-subaccount executions *are* API-obtainable, likely master-client-id artifact — API doc HIGH, "clientId-35-is-why" a STRONG HYPOTHESIS not yet re-tested). §3.5/§3.8 reframed (`OrderAllocation` not a superseding submission path). §4 monitoring channels **populated** (Campus changelog + tws-api docs + annual release notes). No FA breaking changes in 10.35–10.48. Two conductor follow-ups opened (master-client-id reqExecutions re-test; reach whatIf preview via `ibapi`/patched transmit). |
| **2026-07-23** | Initial — standing practice established (`CLAUDE.md` § *IBKR currency*) | Venv introspected (`ib_async` 2.1.0, aeventkit 2.1.0, nest-asyncio 1.6.0, Python 3.12.9, wire protocol 157–178). Gateway build read from `C:\Jts\ibgateway\1045\launcher.log` (**10.45.1g**, May 27 2026) and the `TWS_MAJOR_VRSN=1045` pin confirmed in all three IBC launchers. IBC **3.24.0** confirmed from `C:\IBC\version`. Repo grepped for the IBKR surfaces actually used (§2b). `Order` dataclass introspected: **no `OrderAllocation`**, legacy FA fields only. | Doc created. §3 seeded with two **live-verified** paper-account findings from the same-day FA block test: **what-if is unusable for FA allocation orders (error 321 + indefinite hang) — no pre-trade margin gate exists for FA blocks**; and **allocation executions return only at the FA master, never per subaccount**. Deprecation table opened; `reqFundamentalData` removal at **10.47** is the live clock (we are on 10.45.1g, and no repo code calls it — forward exposure only). §4 left explicitly **incomplete**. |

<!-- Next review due: 2026-10-23 (quarterly), or immediately on a Gateway/IBC/ib_async change. -->
