# IBKR Order Types & Execution Algos — Research + Design for TradingDesk

**Scope:** RESEARCH + DESIGN ONLY. No code changed, no orders placed. PAPER account only (port 4002, DU subs under master DF…141). Triggered by the 2026-06-29 multi-account rebalance where the liquid ETFs (PDBC/RSP/SPY/VTI) filled instantly but the Treasury/cash legs **TFLO** (all 5 accounts) and **VGSH** (DU142) sat at a resting reference-limit, never crossed, and died at session disconnect. The engine had no fallback. This report fixes that.

**Library reality (verified locally):** the venv runs **ib_async 2.1.0**. Its `ib_async.order` module exposes only `MarketOrder, LimitOrder, StopOrder, StopLimitOrder, BracketOrder` — **there is NO `MidpriceOrder` / `PegMkt` / `Adaptive` convenience class.** Every dynamic order below must be built from the **base `Order`** with the right `orderType` string and (for algos) `algoStrategy` + `algoParams`. The base `Order` dataclass DOES carry all required fields: `orderType, lmtPrice, auxPrice, lmtPriceOffset, percentOffset, peggedChangeAmount, midOffsetAtWhole, midOffsetAtHalf, algoStrategy, algoParams, tif, outsideRth, account, faGroup, faMethod, trailStopPrice, trailingPercent`. `TagValue` imports from `ib_async`. (Both verified by introspection of the installed package, 2026-06-29.)

---

## 1. Executive summary (decision-grade)

- **Root cause of the unfilled legs:** we sent a **static `LMT` at the neutral reference price** (last/mid/close) with **`tif="DAY"`** and **no re-pricing, no peg, no algo, no fallback**. For a wide-spread, thin-tape Treasury ETF like TFLO/VGSH, a limit *at the mid* simply does not cross — it rests on our side of the spread until the session ends. Liquid ETFs crossed because their spread is ~1 tick, so "mid" is effectively marketable.
- **Top fix:** route equity/ETF legs through a **dynamic, self-improving order type instead of a frozen limit.** Two production-grade choices, both API-accessible from ib_async via the base `Order`:
  1. **`MIDPRICE`** (`orderType="MIDPRICE"`) with a protective cap — pegs to the NBBO midpoint *or better*, continuously, and (with a cap) will pay up to the cap to complete. US **stocks & ETFs only** — exactly our rebalance universe. Best **price** with good fill odds.
  2. **Adaptive Algo** (`algoStrategy="Adaptive"`, `adaptivePriority="Patient"|"Normal"|"Urgent"`) wrapped on a `LMT` or `MKT` — IBKR's smart-router walks the order between bid and ask for price improvement, then completes. Best **fill certainty** with documented better-than-market average fills.
- **Recommended policy:** a **tiered, instrument-aware order type with a FALLBACK LADDER** so the engine never "throws its hands up": start passive (midpoint), step toward marketable over a bounded number of seconds, and finish with a hard marketable cap. Concretely: **liquid ETF → marketable-limit (cross now)**; **illiquid ETF (TFLO/VGSH) → `MIDPRICE` capped, then escalate to Adaptive(Patient→Urgent) / marketable-limit**; **index options (S5 0DTE SPXW) → capped `LMT` / `REL` peg, never `MIDPRICE`/Adaptive (options unsupported / unsuitable).**
- **Two honest unknowns flagged for a live probe (PAPER):** (a) **do IB algos / MIDPRICE compose with FA *block* (group) orders?** The Adaptive/MIDPRICE docs are written for single-account orders; our blocks set `faGroup` and rely on the group's `ContractsOrShares`. This is *plausible but unconfirmed* — must be probed live before trusting it for Balanced/Growth blocks. (b) **paper-sim fidelity** of algos (paper simulates some order types differently than the real exchange). See Caveats.

---

## 2. Reference table — order types & algos

API column: how to specify in **ib_async 2.1.0** (base `Order` unless a convenience class exists). "Paper caveat" notes paper-account behavior. Sources are cited in §6 and inline by [n].

### 2a. Basic order types & TIF

| Name | `orderType` | What it does | ib_async (2.1.0) | Paper caveat | Best-fit use |
|---|---|---|---|---|---|
| Market | `MKT` | Fill now at market, no price protection | `MarketOrder(action, qty)` | Fills at sim NBBO; fine | Emergency completion only (we avoid naked MKT) |
| Limit | `LMT` | Fill at limit or better; no fill guarantee | `LimitOrder(action, qty, lmtPrice)` | Fills only if marketable | Current (static) behavior — the thing we're replacing |
| Market-to-Limit | `MTL` | MKT that converts to LMT at the first fill price for the remainder | base `Order(orderType="MTL")` | OK | Thin books where you want one clean print then rest |
| Market-if-Touched | `MIT` | Becomes MKT when a trigger price is touched | base `Order(orderType="MIT", auxPrice=trigger)` | Simulated by IBKR | Not needed for rebalance |
| Limit-if-Touched | `LIT` | Becomes LMT when trigger touched | base `Order(orderType="LIT", lmtPrice, auxPrice=trigger)` | Simulated | Not needed |
| Market-on-Close | `MOC` | Fill at the closing auction | base `Order(orderType="MOC")` | Cutoff times apply | Possible for S4 daily if we want the close print |
| Limit-on-Close | `LOC` | Limit at the close auction | base `Order(orderType="LOC", lmtPrice)` | Cutoff applies | S4 daily, price-protected close |
| Stop / Stop-Limit / Trailing / Trailing-Limit | `STP` / `STP LMT` / `TRAIL` / `TRAIL LIMIT` | Risk triggers; trailing follows the market | `StopOrder`, `StopLimitOrder`; trailing via base `Order(orderType="TRAIL", trailingPercent=…)` or `trailStopPrice`/`auxPrice` | Simulated by IBKR | Not used in rebalance; possible S5 protective exits |

**TIF (`order.tif`) [1][2]:** `DAY` (expires at session end — what we use now), `GTC` (good-till-canceled), `GTD` (good-till-date), `IOC` (immediate-or-cancel; cancels unfilled remainder), `FOK` (fill-or-kill; all-or-none, now), `OPG` (open auction only), `DTC` (day-till-canceled). **Add `order.outsideRth=True`** to work pre/post-market — relevant because TFLO/VGSH liquidity and the disconnect timing were a factor; our flatten path already sets this.

### 2b. Dynamic / pegged orders — **the key ones for the unfilled-leg problem**

| Name | `orderType` | What it does (price behavior) | ib_async (2.1.0) | Paper caveat | Best-fit |
|---|---|---|---|---|---|
| **MidPrice** | `MIDPRICE` | Continuously pegs to the **NBBO midpoint or better**; with a `lmtPrice` it will pay **up to that cap** to complete, so it improves price AND can finish. **US stocks & ETFs only — NOT options.** With an offset it auto-caps at the opposite NBBO ±1 tick so it won't over-cross. [3][4] | base `Order(orderType="MIDPRICE", action, totalQuantity=qty, lmtPrice=cap)` | Available on paper; needs a midpoint (live/delayed quote). | **TFLO/VGSH and any wide-spread ETF** — best price-vs-fill balance |
| Relative / Pegged-to-Primary (REL) | `REL` | Peg to **your-side NBBO + an offset**, repricing as the market moves, to sit at the front of the queue more aggressively than NBBO without showing size; optional **cap = `auxPrice`** acts as a hard limit. Offset = `auxPrice` (absolute) or `percentOffset`. Stocks **and options**. [5][6] | base `Order(orderType="REL", action, qty, auxPrice=offset_or_cap, percentOffset=…)` | Works on paper; lit-market peg | Aggressive-but-capped fills; viable options peg for S5 |
| Pegged-to-Midpoint | `PEG MID` | Peg to NBBO midpoint with positive/negative offsets (IBKR ATS / IBUSOPT); offsets via `midOffsetAtWhole`/`midOffsetAtHalf`. | base `Order(orderType="PEG MID", lmtPrice=cap, midOffsetAtWhole=…, midOffsetAtHalf=…)` | Routing/venue dependent | Niche vs MIDPRICE; MIDPRICE is the simpler smart-routed cousin |
| Pegged-to-Market | `PEG MKT` | Peg to bid (buy) / ask (sell) ± offset | base `Order(orderType="PEG MKT", auxPrice=offset)` | Venue dependent | Rarely needed for us |
| Pegged-to-Benchmark | `PEG BENCH` | Price tracks a *reference contract* by a ratio/offset | base `Order(orderType="PEG BENCH", …)` (`referenceContractId`, `peggedChangeAmount`) | Specialized | Future pairs/overlay ideas; not now |
| Snap-to-Market / Midpoint / Primary | `SNAP MKT` / `SNAP MID` / `SNAP PRIM` | One-shot snapshot peg: prices off bid/ask/mid/primary **at submission**, then behaves like a limit (does NOT keep re-pegging) | base `Order(orderType="SNAP MID", auxPrice=offset)` | OK | Lighter alternative to a live peg; one-and-done |

### 2c. IB execution algos (`algoStrategy` + `algoParams=[TagValue(...)]`) [7]

All accessible from ib_async by setting `order.algoStrategy` and `order.algoParams = [TagValue(name, value), …]`. Exact strings & params from the TWS API algos reference [7].

| Algo | `algoStrategy` | Key `algoParams` | Optimizes / use | Min-size & restrictions |
|---|---|---|---|---|
| **Adaptive** | `Adaptive` | `adaptivePriority` = `Urgent` / `Normal` / `Patient` | Smart-router walks the order **between bid and ask** for price improvement on a `MKT` or `LMT`; "on average better fills than plain MKT/LMT" [8]. **Patient** = highest price-improvement odds (scans slowest); **Urgent** = fastest fill, least improvement; **Normal** = default. **TIF must be DAY (no GTC).** [8] | No min size. Stocks/ETFs (and options per docs). Paper-available. |
| Arrival Price | `ArrivalPx` | `maxPctVol` (0.1–0.5), `riskAversion` (Get Done/Aggressive/Neutral/Passive), `startTime`, `endTime`, `forceCompletion`, `allowPastEndTime` | Hit/beat arrival mid; paces by market-risk + %vol | Sized for larger orders; thin tape = slow |
| TWAP | `Twap` | `strategyType` (Marketable/Matching/Midpoint/…), `startTime`, `endTime`, `allowPastEndTime` | Time-weighted avg over a window | **US equities**; needs a time window |
| VWAP | `Vwap` | `maxPctVol` (≤0.5), `startTime`, `endTime`, `noTakeLiq`, `speedUp` | Volume-weighted avg to close | **US equities**; needs real volume |
| Pct-of-Volume | `PctVol` | `pctVol` (0.1–0.5), `startTime`, `endTime`, `noTakeLiq` | Participate at X% of volume | Needs meaningful ADV |
| Accumulate/Distribute | `AD` | `componentSize`, `timeBetweenOrders`, `randomizeTime20`, `randomizeSize55`, `giveUp`, `catchUp`, `waitForFill`, `activeTimeStart/End` | Slice a big order into small random clips over time | Multi-asset; large parent orders |
| Close Price | `ClosePx` | `maxPctVol`, `riskAversion`, `startTime`, `forceCompletion` | Execute toward the close, min slippage | — |
| Balance Impact/Risk | `BalanceImpactRisk` | `maxPctVol`, `riskAversion`, `forceCompletion` | Trade off impact vs price-change risk | — |
| Minimize Impact | `MinImpact` | `maxPctVol` (≤0.5) | Time-slice to reduce impact | — |
| DarkIce | `DarkIce` | `displaySize`, `startTime`, `endTime`, `allowPastEndTime` | Hidden-size iceberg | — |

**Takeaway for us:** the *scheduled* algos (TWAP/VWAP/Arrival/POV/AD) are built for **large orders worked over a time window** and **assume real volume**. Our rebalance clips are tiny (single-digit to low-tens of shares) and we want them done in seconds, not scheduled across the session — so the right tools are **MIDPRICE** (price) and **Adaptive** (completion), not the schedulers. Schedulers stay in the toolbox for any future large single-name unwind.

---

## 3. How to fix the TFLO/VGSH unfilled-leg problem

**Why it failed:** static `LMT` at the reference (last → bid/ask mid → close), `tif="DAY"`, no escalation. TFLO/VGSH trade with a wider spread and thin top-of-book; a limit *at the mid* is **not marketable**, so it rested on our side and never crossed, then expired at disconnect.

### Top recommendation: `MIDPRICE` with a marketable cap, escalating to Adaptive(Patient→Urgent)

**Primary order — `MIDPRICE` (capped).** Pegs to NBBO **midpoint or better** and, because we supply a `lmtPrice` cap, will pay **up to the cap** to actually complete instead of resting forever. US **stocks/ETFs only**, which is exactly our rebalance universe. This gives near-mid price on the easy ticks and still finishes the wide ones.

```python
from ib_async import Order, Stock, TagValue

def midprice_capped(action, qty, cap, account=None, fa_group=None):
    o = Order(orderType="MIDPRICE", action=action, totalQuantity=qty, lmtPrice=cap)
    o.tif = "DAY"
    o.outsideRth = True          # match our flatten path; helps thin Treasury legs
    if account:  o.account = account     # direct (lone-account) leg
    if fa_group: o.faGroup = fa_group; o.faMethod = ""   # FA block — SEE CAVEAT §5
    return o
```
- **Cap** = a deliberately marketable bound, e.g. BUY cap = `ask * (1 + k)`, SELL cap = `bid * (1 - k)` with a small `k` (e.g. 0.001–0.003) or an absolute tick budget. The point of the cap is "I'll pay up to here to GET DONE, but the peg should usually fill me better than this."
- **Trade-off:** MIDPRICE without a cap maximizes price but can still hang if the mid never moves to us; **with a cap** you trade a few cents of price for fill certainty. For TFLO/VGSH that trade is correct.

**Escalation if MIDPRICE doesn't complete in N seconds — Adaptive Algo.** Wrap a `LMT` (at the marketable cap) with the Adaptive algo, starting **Patient** (max price improvement) and, if still unfilled, re-issuing **Urgent** (fastest completion). Adaptive "ensures market and aggressive limit orders trade between the bid and ask" and averages better fills than plain MKT/LMT [8].

```python
def adaptive_limit(action, qty, cap, priority="Patient", account=None):
    o = Order(orderType="LMT", action=action, totalQuantity=qty, lmtPrice=cap)
    o.algoStrategy = "Adaptive"
    o.algoParams = [TagValue("adaptivePriority", priority)]   # Patient | Normal | Urgent
    o.tif = "DAY"                 # Adaptive forbids GTC
    o.outsideRth = True
    if account: o.account = account
    return o
```

**Last-resort completion — marketable limit (already supported).** `live_quotes.limit_price(style="marketable_limit")` already crosses the spread (BUY at ask / SELL at bid). Keep it as the final rung with a sane cap so we never end a rebalance with an open leg.

**Why this order (MIDPRICE → Adaptive(Patient) → Adaptive(Urgent) → marketable-limit cap):** each rung trades a little price for more fill certainty, and the ladder *terminates* — there is always a rung that completes. That is the structural thing the 2026-06-29 run lacked.

---

## 4. Tiered execution policy + fallback ladder (the design)

Per-instrument-class order type, with a bounded escalation ladder. "N seconds" rungs are config knobs (start ~10–20s/rung on paper, tune from fills).

| Instrument class | Examples | Primary order | Fallback ladder (each rung waits N s, then escalates) |
|---|---|---|---|
| **Liquid ETF** (≤~1–2 tick spread) | SPY, VTI, RSP, PDBC | Marketable-limit (cross now), capped | rung-1 marketable-limit → rung-2 Adaptive(Urgent) cap → done |
| **Illiquid ETF** (wide/thin) | **TFLO, VGSH** | **`MIDPRICE` capped** | rung-1 MIDPRICE(cap) → rung-2 Adaptive(Patient) → rung-3 Adaptive(Urgent) → rung-4 marketable-limit at hard cap |
| **Daily index ETF (S4)** | SPY/SPX-tracking | Marketable-limit or `MIDPRICE` capped; optionally `LOC` for a close print | MIDPRICE(cap) → Adaptive(Normal) → marketable-limit cap |
| **Index option (S5 0DTE SPXW)** | SPXW 0DTE | **Capped `LMT`**, or `REL`/`PEG MID` peg with `auxPrice` cap. **NOT `MIDPRICE` (options unsupported) and avoid schedulers.** | rung-1 LMT at mid → rung-2 REL peg (offset toward marketable, capped) → rung-3 marketable-limit cap. Hand-tune; options spreads are wide and gamma-sensitive. |

**Where it plugs into the codebase (no code written here — pointers only):**

- **`live_quotes.py`** — extend the price policy into an **order-spec builder**, not just a scalar limit. Today `limit_price()` returns one float and the style set is `{limit, marketable_limit, market}`. Add: a function that, given `(side, quote, instrument_class)`, returns an **order recipe** (orderType + cap + algo) per the table. Classify instrument by a static set (TFLO/VGSH → illiquid) and/or live spread width `(ask-bid)/mid`.
- **`order_router.py`** — today `build()` and `build_fa_block()` hardcode `LimitOrder(...)`. Generalize to accept an **order recipe** so they emit `Order(orderType="MIDPRICE"/"REL"/"LMT"+Adaptive)` with the cap as `lmtPrice`/`auxPrice`. Keep the **HARD PRICE GUARD** (`_check_limit_price`) — extend it to validate the **cap** the same way (NaN/≤0 rejected). FA-block builder sets `faGroup`/`faMethod=""` as today.
- **`order_router.place()`** — today places once and watches fills for `fill_timeout`. Add a **ladder loop**: place rung-k, watch N s; if `remaining > 0`, **cancel** (`ib.cancelOrder`) and place rung-(k+1) at the escalated spec; stop at the terminal rung. This is the "never give up" behavior. It stays fully inside the existing arming gate — the ladder only runs when `transmit_guard` permits.
- **`rebalance_execute.py`** — already places "ONE block at a time and watches fills" (lines ~316–344). That loop is the natural home to call the laddered `place()` per route. No change to the FA `ContractsOrShares` write path.
- **`config.py`** — replace the single `ORDER_STYLE` with a small **policy table** (instrument-class → primary order + ladder timings + cap `k`). Keeps the engine pure and the policy auditable.

**Idempotency note:** the deterministic `orderRef` (`paperbot:<acct>:<as_of>:<side>:<symbol>`) must be preserved across ladder rungs (same logical intent, escalating mechanics) so a restart still detects the in-flight order rather than double-sending.

---

## 5. API accessibility summary (ib_async 2.1.0)

- **All** order types in §2 are reachable because the base `Order` carries every needed field (verified locally). The *only* convenience classes are Market/Limit/Stop/StopLimit/Bracket — **MIDPRICE, REL, PEG MID/MKT/BENCH, SNAP\***, and **every algo** are built via `Order(orderType=…)` and/or `algoStrategy` + `algoParams=[TagValue(...)]`.
- **Algos:** `order.algoStrategy = "Adaptive"`; `order.algoParams = [TagValue("adaptivePriority", "Patient")]`. Confirmed param names/values against the TWS API algos reference [7] and an independent IB-API code sample [9].
- **GUI-only?** The headline order types and the listed IB algos are all API-exposed [1][7]. Some *TWS conveniences* (one-click "Adaptive" presets, certain order-preset defaults) are GUI affordances, but the underlying order is API-constructible. Nothing we need is GUI-only.

---

## 6. Caveats (honest)

1. **MIDPRICE is US stocks/ETFs ONLY — not options** [4]. Perfect for TFLO/VGSH and S0/S4 ETFs; **do NOT** use it for S5 SPXW options. For options use capped `LMT` or `REL`.
2. **Adaptive forbids GTC** — TIF must be `DAY` (or IOC) [8]. Our orders are DAY anyway; just don't set GTC when attaching Adaptive.
3. **Scheduler algos (TWAP/VWAP/Arrival/POV/AD) assume real volume and a time window.** On thin Treasury ETFs and tiny clip sizes they will under-perform or sit; they are the wrong tool for the rebalance. Keep them only for a future *large* single-name unwind.
4. **FA *block* (group) compatibility is UNCONFIRMED — flag for a live PAPER probe.** Our Balanced/Growth legs are FA group orders (`faGroup` + group `ContractsOrShares`); the MIDPRICE/Adaptive docs are written for single-account orders. It is *plausible* that `orderType="MIDPRICE"` or `algoStrategy="Adaptive"` rides on an FA block, but **not proven**. Recommended probe (mirrors `fa_block_test.py`): on paper, place a tiny FA-block MIDPRICE and a tiny FA-block Adaptive order on `test_group`, confirm via `reqExecutions`/execIds that it (a) is accepted (no Error 10226-class rejection) and (b) allocates across subs. **Until confirmed, the safe pattern is: keep FA blocks on capped marketable-limit, and apply MIDPRICE/Adaptive to the DIRECT (lone-account) legs first** — which is exactly where the Conservative-tier TFLO/VGSH legs live (DU142 ran as 6 direct orders). That gets the immediate TFLO/VGSH win with zero FA-block risk.
5. **Paper-sim fidelity.** IBKR *simulates* some order types on paper and may price algos differently than the real venue; paper midpoints depend on the quote you receive. Our paper market data is **bound to the live subscription** — live ticks on paper only if the live user is subscribed; otherwise IBKR serves **delayed** data, which makes a "midpoint" stale. `live_quotes.fetch()` already requests live and tolerates delayed; just be aware a delayed mid can make MIDPRICE peg to a stale level. Treat paper fills as directional evidence, not exact economics.
6. **`whatIfOrder` HANGS** on this setup, and **never on a group order** (known trap). The ladder must place-and-watch, never what-if. (Carried from memory: paper-arming-and-fills / fa-block-order-allocation.)
7. **Cap math must stay inside the PRICE GUARD.** Any escalated cap is still a price — route it through `_check_limit_price` so a missing quote can never produce a NaN/≤0 cap.

---

## 7. Sources (primary unless noted)

1. Order Types — IBKR API / IBKR Campus: https://www.interactivebrokers.com/campus/ibkr-api-page/order-types/
2. TWS API — Basic Orders (orderType strings, TIF table): https://interactivebrokers.github.io/tws-api/basic_orders.html
3. MidPrice (offset auto-cap behavior, NBBO midpoint-or-better): https://www.interactivebrokers.com/campus/trading-lessons/ibkr-desktop-midprice-order/ and https://www.interactivebrokers.com/en/trading/orders/midprice.php
4. MidPrice product availability (US stocks & ETFs only, options not supported): https://www.interactivebrokers.com/campus/trading-lessons/mosaic-midprice-order-type/ and https://www.interactivebrokers.com/en/trading/orders/midprice.php
5. Relative / Pegged-to-Primary (REL) — offset + absolute cap (auxPrice), more aggressive than NBBO: https://www.interactivebrokers.com/campus/trading-lessons/tws-relative-or-pegged-to-primary-order-type/ and https://www.interactivebrokers.com/en/trading/orders/pegged-to-primary.php
6. Pegged-to-Midpoint (positive/negative offsets, IBKR ATS / IBUSOPT): https://www.interactivebrokers.com/campus/trading-lessons/ibusopt-pegged-to-midpoint-order-type/
7. TWS API — IB Algorithms (algoStrategy strings + algoParams for Adaptive/ArrivalPx/Twap/Vwap/PctVol/AD/ClosePx/BalanceImpactRisk/MinImpact/DarkIce): https://interactivebrokers.github.io/tws-api/ibalgos.html
8. Adaptive Algo (Urgent/Normal/Patient semantics; trades between bid/ask; DAY only, no GTC; market or limit): https://www.interactivebrokers.com/campus/trading-lessons/adaptive/ and https://www.interactivebrokers.com/en/trading/orders/adaptive-algo.php
9. Independent IB-API Adaptive code sample (orderType + algoStrategy='Adaptive' + adaptivePriority TagValue): https://www.mathworks.com/matlabcentral/answers/494680-create-adaptive-algo-order-interactive-broker-api
10. ib_async API docs / order module (Order subclasses; base Order fields): https://ib-api-reloaded.github.io/ib_async/ and https://ib-api-reloaded.github.io/ib_async/_modules/ib_async/order.html
11. Paper-account market-data binding to live subscription (delayed otherwise): https://www.interactivebrokers.com/campus/ibkr-api-page/market-data-subscriptions/
12. Local verification: `ib_async 2.1.0` installed in `C:\TradingDesk-Local\venv`; order subclasses = {Market, Limit, Stop, StopLimit, Bracket} (no MidpriceOrder); base `Order` exposes orderType/algoStrategy/algoParams/lmtPrice/auxPrice/lmtPriceOffset/percentOffset/midOffsetAtWhole/midOffsetAtHalf/tif/outsideRth/faGroup/faMethod; `TagValue` importable. (Introspection, 2026-06-29.)

*Cross-checks: order-type taxonomy and algo strings verified across IBKR Campus + TWS-API github.io; MIDPRICE stock/ETF-only and offset auto-cap verified across two IBKR pages; Adaptive priority semantics + DAY-only verified across the Adaptive lesson and the product page; the ib_async field set verified directly against the installed package, not just docs. Several IBKR Campus pages return HTTP 403 to automated fetchers; their content was captured via IBKR's own search snippets, which I have quoted rather than paraphrased where a specific behavior is load-bearing.*
