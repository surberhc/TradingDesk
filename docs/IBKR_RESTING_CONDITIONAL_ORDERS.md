# IBKR Server-Side RESTING + CONDITIONAL Orders — Disconnect-Survival Deep Dive

**Scope:** RESEARCH + DESIGN ONLY. No code changed, no orders placed, no gateway connected. **PAPER account only** (port 4002, FA master `DF8922141`, DU subs `DU8922142..146`). Companion to `docs/IBKR_ORDER_TYPES_RESEARCH.md` (that doc = the dynamic-pricing / ladder layer; THIS doc = the **resting + conditional + disconnect-survival** layer). Triggered by two needs: (a) the 2026-06-29 rebalance where **TFLO/VGSH DAY legs died at session disconnect** — a GTC resting order would have survived; (b) **S5** will pre-stage auto-cover / tail-protection triggers off the SPX level that must fire **without our program in the loop**.

**The central question, stated precisely:** for each construct, *is it held on IB's SERVERS and does it keep working / trigger / execute even if our TWS / IB-Gateway / API client DISCONNECTS?* Three tiers matter and the whole point is to keep them distinct:
- **(i) Native / exchange-held** — the order lives at the exchange. Survives anything short of the exchange itself going down.
- **(ii) IB-server-SIMULATED** — IB holds and monitors it on **IB's own servers** and releases it to the exchange when marketable/triggered. Stop, stop-limit, trailing, conditional, MIT/LIT, and the simulated-peg family are here. *Survives client disconnect in IBKR's stated model* — but IBKR is **conspicuously silent** on the fully-offline trigger guarantee (see §3 + §8).
- **(iii) Client-held** — the trigger/logic lives in OUR process (or an untransmitted TWS-session-local order). Dies or goes inert the moment we disconnect. **This is the category Andrew wants to escape.** Our current ladder loop (`place_laddered`) and any in-process "if-then" is tier (iii).

---

## 1. EXECUTIVE SUMMARY (decision-grade)

1. **Yes — IB rests orders on its servers and they survive our client disconnecting.** IBKR's own language: *"Active orders will remain active upon exiting Trader Workstation … it is possible to receive an execution without being logged in."* [1][2] A **GTC** (or GTD) resting limit is the simplest disconnect-proof construct and is exactly what the dead TFLO/VGSH **DAY** legs needed.
2. **Conditional Orders are IB's native if-then and are evaluated on IB's servers, including cross-instrument triggers** — "if SPX ≤ X then act on the SPXW option" is a first-class, supported pattern [1][7][13]. They are accessible from ib_async via `order.conditions=[PriceCondition(...)]` with the **conId of a DIFFERENT contract** [7][12]. This is the construct that lets S5's cover/roll/tail logic sit at IB instead of round-tripping our process.
3. **OCA groups, brackets, attached/child (parentId), OTO/OCO are enforced by IB**, not by our client — once transmitted, "one fills → the rest cancel" is IB's job and survives our disconnect [3][6][9]. Brackets in the TWS API are linked via `parentId`; the parent+children are submitted as a unit (children `transmit=False` until the last is sent `transmit=True`) [9].
4. **Stops / stop-limits / trailing stops are IB-SIMULATED and IB-server-held** (the exchange usually has no native stop) [4][5]. They trigger from IB's servers, NOT our machine — but with two honest caveats: IBKR explicitly warns simulated orders "may be subject to performance issues of third parties outside our control" [5], and IBKR does **not** publish a clean "triggers even if you are fully offline" guarantee for the simulated family. **Treat as server-held; verify with a paper probe before betting S5 risk on it.**
5. **THE DISCONNECT-SURVIVAL SETTING (headline, §2):** the relevant control is **API → Settings → "Maintain and resubmit orders when connection is restored"** (default **ON** in TWS/Gateway ≥ 10.28; reworked in ≥ 10.40 to cover network drop + auto-restart) [10]. The **danger** setting is the per-client **auto-cancel / "cancel open orders on disconnect"** behavior tied to clientId — if active, our resting orders die on disconnect, which is the precise failure to avoid. **Our IB Gateway does NOT auto-cancel by default**, but this must be confirmed on our config and **the orders must be GTC, not DAY**, or they expire at session end regardless.
6. **The two opposed levers (resting GTC vs the active escalating ladder) reconcile cleanly (§5):** run the **active price-improving ladder while we're connected during the session**, then convert the **unfilled remainder to a resting GTC** (the terminal rung) so the leg cannot die at disconnect. Active when present, resting when gone.
7. **Honest limits (§8):** (a) IBKR's silence on offline-trigger for simulated/conditional orders is real — flag for a paper probe; (b) **GTC orders self-cancel after ~90 days with no login** and at quarter-boundary corporate-action rules [11]; (c) untransmitted / TWS-session-local orders are **cleared on restart** [8] — never rely on a tier-(iii) order surviving; (d) `whatIfOrder` HANGS on our setup — the staging path must place-and-watch, never what-if (carried from memory).

---

## 2. THE DISCONNECT-SURVIVAL CONFIG — read this first

**There is no single "auto-cancel on disconnect" master switch that is ON by default for the API.** What governs survival is a combination; here is the exact picture from primary sources, with the one we must set:

| Setting | Where it lives | Default | What it does | Our recommended value |
|---|---|---|---|---|
| **"Maintain and resubmit orders when connection is restored"** | TWS/Gateway **Global Configuration → API → Settings** | **ON (checked)** in TWS/Gateway **≥ 10.28**; reworked ≥ 10.40 to also cover auto-restart [10] | When checked, orders received while connectivity is lost are saved and **auto-resubmitted on reconnect**; from ≥10.40 it maintains/submits orders after a network disconnect **or the auto-restart** [10] | **Keep ON.** This is the friend, not the enemy. |
| Per-API-client behavior on disconnect (the implicit "cancel open orders when the API disconnects") | Governed by clientId semantics + `reqGlobalCancel`; there is **no documented default that auto-cancels resting orders** when an API client drops [a] | **Does NOT auto-cancel** (per IBKR KB: active orders remain active after exiting TWS) [1][2] | If a client-side or third-party auto-cancel were enabled, resting orders would die on disconnect — the failure Andrew wants gone | **Confirm none is enabled.** Our code must not call `reqGlobalCancel` on shutdown. |
| **Time in force on the order itself** | The order (`order.tif`) | We currently send **`DAY`** | **DAY expires at session end** — this is *why* TFLO/VGSH died, not a gateway setting | **Use `GTC`/`GTD`** for any leg that must survive a disconnect. |
| Auto-Restart (nightly) / weekly cold restart | Gateway config (IBC `autoRestart` / scheduled restart) | Auto-restart nightly; full **server reset** weekly | Resting **transmitted** orders persist across the auto-restart; the **API connection drops and must reconnect** [10][b] | Keep IBC auto-restart; ensure our client **reconnects** and re-reads open orders after the reset. |

**Critical nuances (do not miss):**
- **DAY vs GTC is the actual lever for the rebalance bug.** No gateway setting would have saved a `tif="DAY"` order past session end. The fix is to change the order, not (only) the config.
- **Untransmitted / session-local orders are deleted on restart.** IBKR: *"Untransmitted orders will only be available within that TWS session (not for other usernames) and will be cleared on restart."* [8] Anything we stage but don't transmit, and anything held only in our process, is tier (iii) and dies. To survive, an order must be **transmitted and accepted by IB's servers**.
- **Nightly auto-restart:** transmitted resting orders survive it, but **the API socket drops** — our client must reconnect and reconcile open orders (we already have a reconcile path). During the **weekly server reset**, IBKR documents that *existing native orders operate normally while execution reports and simulated orders are delayed until the reset completes* [5] — i.e. a simulated stop/conditional could be briefly blind during the reset window. Flag for S5.
- **GTC longevity caps:** GTC auto-cancels after **~90 days with no account login**, at quarter-boundary, and on certain corporate actions / dividends > 3% [11]. For an always-on S5 tail this means we must **log in periodically and re-stage rolled GTCs** — they are not truly "set and forget for a year."

> [a] IBKR publishes no setting whose default cancels resting orders purely because an API client disconnected; the documented model is the opposite (orders remain active). The only client-driven mass-cancel is the explicit `reqGlobalCancel` call, which WE control. **This is the single most important thing to confirm live on our gateway** (§8 probe list).
> [b] The auto-restart drops and re-establishes the platform; the "Maintain and resubmit" setting (≥10.40) is specifically what carries orders across it [10].

---

## 3. DISCONNECT-SURVIVAL MATRIX — the headline artifact

Legend — **Held where:** EXCH = native exchange-held · IB-SIM = simulated & held on IB servers · CLIENT = lives in our process / TWS-session-local. **Survives API disconnect?** = does it keep working if our TWS/Gateway/API client drops (order already transmitted & accepted). **Survives nightly reset?** = does the resting/transmitted order persist across the nightly auto-restart (socket drops; order persists). **ib_async?** = constructible in ib_async 2.1.0. **Paper caveat** noted.

| Construct | Held where | Survives API disconnect? | Survives nightly reset? | ib_async API | Paper caveat |
|---|---|---|---|---|---|
| **LMT/MKT, tif=DAY** | EXCH | **NO — expires at session end** regardless of disconnect | n/a (gone by close) | `LimitOrder(...)`, `.tif="DAY"` | This is what killed TFLO/VGSH |
| **LMT, tif=GTC** | EXCH (or IB-SIM if exch lacks support) | **YES** [1][2] | **YES** (transmitted) [10] | `.tif="GTC"` | 90-day-no-login + quarter + corp-action cancels [11] |
| **LMT, tif=GTD** | EXCH | **YES until the date/time** | YES until expiry | `.tif="GTD"`, `goodTillDate` | Same family as GTC |
| **Good-after-time (GAT)** | IB-SIM (activation deferred) | **YES** (activates at the time even if we're away) | YES | `Order.goodAfterTime` | Activation is server-side |
| **Conditional Order (Price/Time/Margin/Exec/Volume/%Chg)** | IB-SIM (server-evaluated) | **YES in IBKR's stated model** [1][7]; offline-trigger not explicitly guaranteed → PROBE | YES (transmitted) | `order.conditions=[PriceCondition(...)]`, `conditionsCancelOrder` [7][12] | **Cross-instrument trigger supported** (conId of another contract) [7] |
| **Stop / Stop-Limit** | IB-SIM (exchange usually no native stop) [4][5] | **YES (server-held)** — but IBKR warns of 3rd-party performance caveats [5] | YES | `StopOrder`, `StopLimitOrder` | Trigger method matters per secType [14] |
| **Trailing Stop / Trailing-Stop-Limit** | IB-SIM | **YES (server-held)** | YES | base `Order("TRAIL", trailingPercent=…/trailStopPrice)` | Trail recalculated server-side |
| **MIT / LIT** | IB-SIM | **YES (server-held)** | YES | base `Order("MIT"/"LIT", auxPrice=trigger)` | Simulated trigger |
| **OCA / One-Cancels-All group** | IB-SIM (group enforced by IB) [3][6] | **YES** — one fills → IB cancels the rest, no client needed | YES | per-order `ocaGroup`, `ocaType`(1/2/3); `ib.oneCancelsAll(...)` [12] | ocaType 1 = cancel-with-block (overfill protection) [3] |
| **Bracket (entry + TP limit + SL stop)** | entry EXCH; children IB-SIM; linked by parentId | **YES** — children auto-submit on parent fill, OCA-linked, IB-managed [9] | YES | `ib.bracketOrder(action, qty, lmt, tp, sl)` [12] | Children carry `parentId`; transmit last child =True [9] |
| **OTO (one-triggers-other) / attached child** | IB-SIM (parentId link) | **YES** — child fires on parent fill, server-side [9] | YES | child `.parentId=parent.orderId`, staggered transmit | — |
| **OCO (one-cancels-other)** | IB-SIM (OCA of 2) | **YES** | YES | two orders, same `ocaGroup` | OCO = the 2-leg case of OCA |
| **Adaptive algo / MIDPRICE / scheduled algos** | CLIENT-adjacent / DAY-bound | Adaptive is **DAY-only (no GTC)** → not a resting construct; MIDPRICE pegs continuously (DAY) | NO (DAY) | per companion doc | **Do NOT use for the resting layer** — they're the *active-session* layer |
| **Our in-process ladder / any Python if-then** | **CLIENT (tier iii)** | **NO — dies on disconnect** | NO | `place_laddered(...)` in `order_router.py` | This is the thing to *replace* with server-side staging where survival matters |

**Reading of the matrix:** everything we want for "decision-made, now leave it at IB" lives in the **GTC + Conditional + OCA/bracket** rows (EXCH or IB-SIM, survives disconnect, survives nightly reset). Everything in the **DAY / Adaptive / in-process-ladder** rows is session-bound and is the *opposite* of what Andrew asked for — useful while connected, useless once we're gone.

---

## 4. The constructs in detail (with ib_async shapes)

All examples are **design illustrations** — code-shaped, not to be run. ib_async 2.1.0 confirmed locally (companion doc): base `Order` carries `conditions`, `conditionsCancelOrder`, `ocaGroup`, `ocaType`, `parentId`, `tif`, `goodAfterTime`, `goodTillDate`; condition classes and `ib.bracketOrder()`/`ib.oneCancelsAll()` exist [12].

**4.1 Resting GTC (the simplest disconnect-proof order).**
```python
o = LimitOrder("BUY", qty, cap); o.tif = "GTC"; o.outsideRth = True
o.orderRef = "paperbot:<acct>:<as_of>:BUY:TFLO"   # keep our deterministic ref
```
Survives disconnect and nightly reset [1][2][10]. Re-stage before the 90-day/quarter cancel window [11].

**4.2 Conditional Order — native if-then, cross-instrument.** Condition references a DIFFERENT contract (e.g. SPX index) than the contract being traded (e.g. an SPXW put):
```python
from ib_async import PriceCondition, Order
cond = PriceCondition(price=Xlevel, conId=SPX_conId, exch="CBOE",
                      isMore=False, triggerMethod=0)   # "SPX <= X"
o = Order(orderType="LMT", action="SELL", totalQuantity=n, lmtPrice=cap)
o.conditions = [cond]; o.conditionsCancelOrder = False   # False = ACTIVATE on condition
o.tif = "GTC"; o.outsideRth = True
```
Condition types available: **Price, Time, Margin, Execution, Volume, PercentChange** [7]; combinable with `.And()`/`.Or()` [12]. `conditionsCancelOrder=True` flips the semantics to "cancel this working order when the condition is met." Server-evaluated [1][7].

**4.3 OCA group (cover/roll basket; one fills → rest cancel, server-side).**
```python
for o in (cover_a, cover_b, roll_c):
    o.ocaGroup = "s5_cover_2026q3"; o.ocaType = 1   # 1 = cancel-remaining WITH block (overfill-safe)
```
IB cancels the survivors when one fills — no client required [3][6].

**4.4 Bracket (entry + profit-target + protective stop), IB-linked.**
```python
bo = ib.bracketOrder("BUY", qty, limitPrice=entry, takeProfitPrice=tp, stopLossPrice=sl)
for o in bo: o.tif = "GTC"      # make the children rest, not DAY
# transmit parent last / children staggered per parentId rules [9]
```
Children carry `parentId`, auto-submit on parent fill, and are OCA-linked so the TP and SL cancel each other [9].

**4.5 Stop / trailing stop (IB-simulated protective exit).**
```python
StopOrder("SELL", qty, stopPrice); o.tif="GTC"            # simulated, server-held [4][5]
Order(orderType="TRAIL", action="SELL", totalQuantity=qty, trailingPercent=p); o.tif="GTC"
```

---

## 5. Staging if-then RISK/COVER orders at IB for S5 options

**Goal:** once we've *decided* the cover/tail logic, it sits at IB and fires off an SPX level with our program out of the loop.

**The pattern that works (all server-side once transmitted):**
1. **Always-on tail = resting GTC long puts** (the Tier-1 uncapped deep-OTM tail from the S5 design). A plain GTC buy/hold needs no trigger — it just exists at IB. Re-stage on roll and before the 90-day cancel window [11].
2. **Auto-cover / de-risk trigger = a Conditional Order keyed to the SPX level**, not to the option's own price: `PriceCondition(conId=SPX, isMore=False, price=trigger)` attached to a `SELL`/cover order on the SPXW leg [7]. "If SPX ≤ trigger, send the cover" — IB evaluates it [1][7][13].
3. **Mutually-exclusive cover ladder = OCA group** (`ocaGroup`, `ocaType=1`): stage several cover/roll alternatives; the first to fill cancels the rest, IB-enforced, overfill-protected [3][6].
4. **Profit-take + protective-stop on a written premium leg = bracket / OTO** with `parentId`, children `tif="GTC"` so they rest [9].
5. **"Stand-down vs harvest" regime flip:** the *binary* flip (sell premium vs don't) can be partly encoded as conditional activation, but the **sizing / which-strike / how-much** decision is genuinely ours and should stay in our process during the session, then leave the **resulting protective orders resting** at IB.

**Honest limits — what still requires us connected:**
- **Conditions on a derived/synthetic signal** (realized-vol, gamma sign, VIX term-structure, the S5 ledger state) are **NOT** expressible as IB conditions — IB conditions are Price/Time/Margin/Exec/Volume/%Change on *tradable contracts* only [7]. Any vol/gamma-gated decision must be computed by us; we can then *stage the resulting order* as a resting conditional/GTC. So: **our brain decides, IB holds the trigger.**
- **Continuous re-pricing / laddering** of a cover (the active price-improvement) is a connected-session behavior; the disconnect-proof fallback is a **resting GTC at a marketable-enough cap**, accepting worse price for survival.
- **The offline-trigger guarantee for simulated/conditional orders is not documented by IBKR** — until probed, treat the SPX-triggered cover as "very likely server-side, verify before trusting it as the *sole* protection." Pair a deep always-on GTC tail (un-triggered, can't fail to fire) under any triggered cover, so catastrophe protection never depends on a trigger firing.

---

## 6. Immediate application to the rebalance

**Should the illiquid-leg ladder use GTC resting rungs?** Yes — but *as the terminal rung*, not the whole ladder. The active escalating ladder (price-improvement) and a resting GTC are somewhat opposed: the ladder wants to be present and re-pricing; GTC wants to be left alone. **Reconcile by phase:**

- **During the session (connected):** run the existing `place_laddered` active ladder (MIDPRICE → Adaptive(Patient→Urgent) → marketable cap) for price improvement. Unchanged.
- **Terminal rung becomes a resting GTC remainder**, not a cancelled residual. Today the ladder *cancels* the residual at the terminal rung; instead, **leave the unfilled remainder as a `tif="GTC"` marketable-capped limit** so it (a) survives session disconnect — the exact thing that killed TFLO/VGSH — and (b) completes whenever the thin Treasury book crosses, even after we're gone.
- **Net rule:** *ladder while present, rest when gone.* The leg never both "dies at disconnect" AND "gives up price by crossing immediately" — it improves price while we watch, then converts to a resting GTC that can't expire at session end.
- **Idempotency:** keep the deterministic `orderRef` across the active→GTC handoff so a reconnect detects the resting order rather than double-sending. (Worker note: a separate worker is building the laddered router; this GTC-remainder behavior is a design input for it, not something to code here.)
- **One caution:** a GTC remainder left at a *marketable* cap can fill at any later moment the book moves — fine for "get this leg done," but the cap must stay inside the PRICE GUARD and be a sane bound, not a blank check. For a price-sensitive leg, rest the GTC at a *limit* (non-marketable) and accept it may sit.

---

## 7. Paper-account & ib_async specifics

- All constructs above are submittable on the **paper** account via ib_async 2.1.0 [12]. Condition objects, `ocaGroup`/`ocaType`, `bracketOrder()`, `parentId`, `tif="GTC"`, `goodAfterTime`/`goodTillDate` are all present.
- **FA-block interaction is UNVERIFIED** (carried from the companion doc): conditional/OCA/bracket semantics on an **FA group** order (`faGroup` + group `ContractsOrShares`) are not documented for the group case. Keep server-side staging on the **direct (lone-account) legs** first — which is where the Conservative-tier TFLO/VGSH legs already run (6 direct orders to DU142) — and probe FA-block before trusting it.
- **Paper market-data fidelity:** conditions that trigger off a price need a live/delayed quote on the trigger contract; a delayed SPX feed makes an SPX-triggered cover fire on stale data. Treat paper triggers as directional evidence, not exact timing.
- **`whatIfOrder` HANGS** on our setup — the staging path must place-and-watch, never what-if (memory: paper-arming-and-fills).

---

## 8. CAVEATS + LIVE-PAPER-PROBE list

**Honest caveats:**
1. **IBKR is silent on the fully-offline trigger guarantee for SIMULATED/CONDITIONAL orders.** Its documented model says orders "remain active" and you can "receive an execution without being logged in" [1][2] — but that language is cleanest for *native/resting* orders. For *simulated* stops/conditionals, IBKR adds the hedge that they "may be subject to performance issues of third parties outside our control" [5]. **Do not assume a simulated trigger fires while we're fully offline until probed.**
2. **GTC is not forever:** ~90-day-no-login cancel, quarter-boundary, and corporate-action/dividend>3% cancels [11]. Always-on S5 tails need periodic login + re-stage.
3. **DAY vs GTC was the real rebalance bug** — no gateway setting saves a DAY order past session end.
4. **Untransmitted / session-local orders are cleared on restart** [8] — never rely on a tier-(iii) order surviving.
5. **Weekly server reset** can briefly delay simulated-order execution reports/triggers [5] — a short blind window for S5 conditionals.
6. **Nightly auto-restart drops the API socket** — our client must reconnect and reconcile open orders; "Maintain and resubmit" (≥10.40) carries the orders, but we must come back [10].

**Items that warrant a LIVE PAPER PROBE before we trust them (highest value first):**
1. **Does a transmitted Conditional Order actually survive a Gateway KILL on paper, and trigger after we're gone?** Stage an SPX-triggered GTC conditional on a DU sub, kill the gateway, drive the trigger (or wait for a real cross), reconnect, confirm it fired. **This is the load-bearing test for the whole "leave it at IB" thesis.**
2. **Confirm our Gateway does NOT auto-cancel open orders on API disconnect** — verify no client-side/3rd-party cancel-on-disconnect is configured, and that our shutdown path never calls `reqGlobalCancel`. Check the **"Maintain and resubmit orders when connection is restored"** box is ON.
3. **Does a GTC order survive the nightly auto-restart on paper** (transmit, let the gateway auto-restart overnight, confirm still working next session)?
4. **FA-block compatibility** of conditional/OCA/bracket orders (probe on `test_group`, mirror `fa_block_test.py`) — or confirm we keep server-side staging on direct legs only.
5. **Simulated stop / SPX-triggered conditional offline-trigger** specifically: confirm a simulated stop fires with the client fully disconnected (not just "remains visible").

---

## 9. Sources (primary IBKR unless noted; cross-checked)

1. Conditional Orders — IBKR Campus / Traders' Academy: https://www.interactivebrokers.com/campus/trading-lessons/conditional-orders/ (condition types; "submitted or cancelled only if criteria met"; "Allow condition to be satisfied … outside RTH"; orders remain active on exit) — *page 403s to automated fetch; content captured via IBKR search snippets and cross-checked against [2][7].*
2. Order Types & Algos — IBKR: https://www.interactivebrokers.com/en/trading/ordertypes.php ("Active orders will remain active upon exiting Trader Workstation … receive an execution without being logged in"; native vs simulated; GTC resting).
3. One-Cancels-All (OCA) — TWS API: https://interactivebrokers.github.io/tws-api/oca.html (ocaGroup; ocaType 1/2/3; overfill protection; "completion of one piece … causes cancellation of the remaining").
4. Stop order product page — IBKR: https://www.interactivebrokers.com/en/trading/orders/stop.php (stop simulated by IB; held until triggered).
5. Order Types & native-vs-simulated / system status — IBKR: https://www.interactivebrokers.com/en/trading/ordertypes.php and https://www.interactivebrokers.com/en/software/systemStatus.php ("IB simulates certain order types (stop or conditional)"; "Accepted/Working … at the exchange or on IB's servers"; "existing native orders operate normally while … simulated orders will be delayed until the reset is complete"; simulated "subject to performance issues of third parties").
6. OCA in IBUSOPT / OCO attribute — IBKR Campus: https://www.interactivebrokers.com/campus/trading-lessons/using-the-one-cancels-another-oca-order-attribute-in-ibkrs-ibusopt/ (OCO = 2-leg OCA; server-grouped).
7. Order Conditioning — TWS API: https://interactivebrokers.github.io/tws-api/order_conditions.html (six condition types Price/Execution/Margin/PercentChange/Time/Volume; conditions reference a DIFFERENT conId+exchange; `ConditionsCancelOrder`; `order.conditions.append(...)`).
8. Placing Orders / Transmit flag — TWS API: https://interactivebrokers.github.io/tws-api/order_submission.html ("Untransmitted orders will only be available within that TWS session … and will be cleared on restart").
9. Bracket Orders — TWS API: https://interactivebrokers.github.io/tws-api/bracket_order.html (parentId linkage; staggered transmit; last child transmit=True activates predecessors).
10. "Maintain and resubmit orders when connection is restored" — TWS/Gateway release notes + Campus: https://www.ibkrguides.com/releasenotes/prod-2025.htm and https://www.interactivebrokers.com/campus/ibkr-api-page/twsapi-doc/ (default ON ≥10.28; ≥10.40 covers network disconnect + auto-restart; "if TWS is closed during this time, the orders are deleted regardless of the setting").
11. GTC longevity — IBKR Campus / GTC pages: https://www.interactivebrokers.com/campus/trading-lessons/mosaic-good-till-cancelled-gtc-order-type/ and https://www.interactivebrokers.com/en/trading/orders/gtc.php (GTC cancels after ~90 days no login; quarter-boundary; corporate-action/dividend>3% cancels; resting for days/weeks/months).
12. ib_async API docs: https://ib-api-reloaded.github.io/ib_async/api.html (PriceCondition/TimeCondition/MarginCondition/ExecutionCondition/VolumeCondition/PercentChangeCondition; OrderCondition .And()/.Or(); Order.conditions/conditionsCancelOrder/ocaGroup/ocaType/parentId; IB.bracketOrder(); IB.oneCancelsAll()).
13. Conditional Order — TWS User Guide: https://www.interactivebrokers.com/en/software/tws/usersguidebook/specializedorderentry/conditional.htm (use stocks/options/futures/indices to trigger; submit-or-cancel; outside-RTH activation) — *403 to fetch; corroborated via [1][7].*
14. Trigger Methods — TWS API: https://interactivebrokers.github.io/tws-api/trigger_method_limit.html ("These trigger methods only apply to stop orders simulated by IB"; if handled natively the trigger method is ignored; secType compatibility matrix; default=0, double bid/ask=1, last=2, double last=3, bid/ask=4, last-or-bid/ask=7, mid-point=8).

*Cross-checks: the disconnect-survival claim is corroborated across the IBKR Campus conditional-orders lesson [1], the order-types page [2], and the TWS-API transmit/persistence docs [8]; the native-vs-simulated distinction across [4][5][14]; OCA/bracket server-enforcement across [3][6][9]; cross-instrument conditioning across [7][13]. Several IBKR Campus / product pages return HTTP 403/402 to automated fetchers; their load-bearing text was captured via IBKR's own search snippets and quoted rather than paraphrased, and cross-verified against the fetchable github.io API docs. The single genuinely under-documented point — offline-trigger guarantee for simulated/conditional orders — is flagged as a probe rather than asserted.*
