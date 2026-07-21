# MODEL_PORTFOLIO_GATEWAY_TEST_PLAN.md — live paper-gateway verification checklist

**Conductor item:** #43
**Audience:** whoever has the PAPER IBKR Gateway (port 4002) open in front of them today.
**Goal:** verify the Model Portfolios sleeve design (`paperbot/model_portfolio.py`) against
the real paper FA master, and settle the open ambiguities the foundation module couldn't
resolve without a live, *allocated* model.
**Posture:** READ-ONLY / whatIf by default. Nothing here transmits a real trade. The whatIf
tests (#0, #3, #4, #9) and the readonly-enforcement test (#8) touch the transmit boundary
and are called out explicitly — read the **GATE box** under each before running it.
**RUN TEST 0 FIRST.** It is the single showstopper that decides the whole architecture (see
Design context below); do not spend effort on tests #1–#9 until #0 is settled.

**Design context (biggest open risk):** the whole "two models in one account" design assumes
**both** sleeves can live under a `modelCode`. But IBKR advisor docs indicate Model Portfolios
support **stocks and single-leg positions only — NOT multi-leg options combos**. S8 trades SPX
0DTE credit spreads (two-leg BAG/combo orders). If models can't hold a multi-leg combo, **S8
cannot be a modelCode sleeve at all** — the single biggest risk to this design. **Test 0 settles
it.** Recommended fallback if Test 0 fails: **hybrid** — `S0_ALLWEATHER` = Model Portfolio sleeve,
`S8_ZERODTE` = its own **dedicated sub-account** under the same FA master (or, failing that,
all-sub-accounts, no models).

Grounding facts (verified against the paper gateway 2026-07-20 — see
`docs/CRM_HANDOFF_model_allocation.md` §8 and `docs/MODEL_PORTFOLIO_SPEC.md`):

- FA master **`DF8922141`**; client sub-accounts **`DU8922142`–`DU8922146`**, each ~$1.08M–$1.12M paper NetLiq.
- Models **`S0_ALLWEATHER`** and **`S8_ZERODTE`** exist in TWS (`MODEL_REGISTRY` in `model_portfolio.py`) but are **INVISIBLE to the account API until an account is reallocated/invested into them**.
- Model creation / allocation / rebalance are **UI-only** (no API). `modelCode` must match the TWS name **byte-for-byte**.
- ib_async 2.1.0: `Order.modelCode` works; `reqAccountUpdatesMulti` works (now timeout-bounded after the `read_model_account_values` fix); `reqPositionsMulti` wrapper callbacks are **no-op stubs**, worked around inside `read_model_positions`.

---

## Two "read-only" layers you must keep straight

There are **two independent** switches, and the whatIf tests depend on the second one:

1. **Connection readonly flag** — `ibkr_paper.connect(consumer, readonly=True/False)`. The desk connects `readonly=True` everywhere by default.
2. **Gateway "Read-Only API" checkbox** — the actual transmission wall (`paperbot/arming.py`). When ON, the Gateway rejects any order request with **code 321, "The API interface is currently in Read-Only mode."** Toggling this OFF is **arming** — the deliberate, gated step. Normal desk state is **ON (locked)**.

A `whatIf` order is a **margin/commission preview** — IBKR computes it and never places anything (it is zero-transmit by construction, independent of `transmit=False`). The open question is whether the **Gateway Read-Only API checkbox blocks even a whatIf**. Tests #3/#4/#9 answer that empirically and, if blocked, tell you arming is required just to preview.

---

## PREREQUISITE (manual TWS step — and itself partly under test)

In TWS → **Model Portfolios**, **Reallocate** one *free* paper sub-account —
suggest **`DU8922144`** or **`DU8922146`** (both reserved/unused) — to:

- **75% `S0_ALLWEATHER`**
- **25% `S8_ZERODTE`**

Use the exact model names `S0_ALLWEATHER` / `S8_ZERODTE` (byte-for-byte). Let IBKR's model
rebalance invest the account into each model's defined holdings if prompted.

> This Reallocate is the manual UI action whose **necessity we are partly testing** (test #3
> asks whether a model-tagged order is even accepted *without* it). Do it on **one** account
> so we have both an allocated account (this one) and un-allocated ones (`DU8922142/143/145`)
> to compare against. Pick `DU8922144` or `DU8922146` below; this doc assumes **`DU8922144`**
> as `ALLOCATED_ACCT` and **`DU8922145`** as `UNALLOCATED_ACCT` — change if you chose others.

For tests #5/#6/#7 you also need the models to actually **hold** something. Getting a
position into a sleeve is a **TWS-UI action** (set the model's target holdings + Reallocate,
or place a model order in the TWS UI by hand) — **not** a desk transmit. Keep it that way:
the desk stays read-only; TWS creates the positions. For the **fungibility** test (#6) both
`S0_ALLWEATHER` and `S8_ZERODTE` must be set up in TWS to hold the **same** instrument
(e.g. both hold **SPY**).

---

## Shared connection preamble

Every scripted test below assumes this header. Save each test as its own `.py` in the
scratchpad and run it with the venv python. **Use clientId consumer `paperbot_recon` (id 32)**
— it is free. Do **not** use `paperbot_accounts` (31) or `capabilities_introspect` (41); both
got jammed in earlier probing.

```python
# --- HEADER: prepend to every test script ---
import sys
sys.path.insert(0, r"C:\TradingDesk\connections")
sys.path.insert(0, r"C:\TradingDesk\paperbot")
from connections import ibkr_paper           # noqa: E402
import model_portfolio as mp                  # noqa: E402

ALLOCATED_ACCT   = "DU8922144"   # the account you Reallocated 75/25 (prereq)
UNALLOCATED_ACCT = "DU8922145"   # an account NOT reallocated into any model
FA_MASTER        = mp.FA_MASTER_ACCOUNT      # "DF8922141"

# READ-ONLY connect (default). id 32 = paperbot_recon.
ib = ibkr_paper.connect("paperbot_recon", readonly=True, launch=False, timeout=15)
```

Run pattern:

```powershell
& "C:\TradingDesk-Local\venv\Scripts\python.exe" "C:\Users\andre\AppData\Local\Temp\claude\C--TradingDesk\b83d72b8-f988-4971-b34d-bca28beb236b\scratchpad\mp_test_N.py"
```

The existing read-only probe `scratchpad\mp_live_probe.py` is a good warm-up: it enumerates
managed accounts and dumps every `modelCode` the API currently reports. Run it first to
confirm the gateway is up and see the pre-Reallocate baseline.

---

## Tests

### Test 0 — RUN FIRST · SHOWSTOPPER: can a modelCode hold a MULTI-LEG options combo?
**Question:** Can a **multi-leg** options combo — an SPX 0DTE credit spread (two option legs, a
BAG/combo order) — be placed and held under an IBKR `modelCode` **at all**? IBKR's advisor
documentation indicates Model Portfolios support **stocks and single-leg positions only — NO
multi-leg options combos**. If that limit is real, **S8 (`S8_ZERODTE`) cannot be a modelCode
sleeve**, and only S0 (ETFs) can. This one test decides whether **S8-as-a-model is alive or
dead**, and therefore which overall account architecture we adopt. **Do this before anything
else in this doc.**

**Step A — TWS UI check FIRST (safest; may answer it with no order at all).** In TWS →
**Model Portfolios**, open the `S8_ZERODTE` model and try to **add / hold a multi-leg options
position** inside it (a two-leg SPX/SPXW spread as a model target holding, or place a combo
order in the TWS ticket tagged to the `S8_ZERODTE` model). Observe whether TWS **even lets you**:
does the model editor accept a combo/BAG holding, or does it refuse / silently accept only the
single legs / grey out multi-leg? Record exactly what TWS does. **If TWS itself won't let a
multi-leg position into the model, the question is already answered — FAIL — and you need no
order at all.**

**Step B — scripted whatIf on a modelCode-tagged combo (only if Step A was inconclusive).**
Build a two-leg SPX/SPXW credit-spread **BAG** (combo) order tagged `modelCode="S8_ZERODTE"`
against the FA master and attempt a `whatIf`. Read-only first, do **NOT** transmit.

> **⚠ GATE box.** This is a `whatIf` — it never transmits, but it travels the order path, so
> the **Gateway "Read-Only API" checkbox** may reject it with **code 321 ("Read-Only mode")** —
> the same caveat as tests #3/#4/#9. **Stage 1:** run on the normal read-only setup (HEADER
> as-is, checkbox ON). Code 321 / "Read-Only mode" means you only learned whatIf is blocked
> read-only — record it and STOP; do **not** arm on your own initiative. **Stage 2 (only if
> Andrew explicitly approves arming):** he arms the Gateway (`arming.py` toggles the Read-Only
> API checkbox OFF), you reconnect `readonly=False`, run, and **disarm immediately after**.
> whatIf is zero-transmit either way. Never `placeOrder(..., transmit=True)`.

```python
# ... HEADER ...  (stage 1: readonly=True; stage 2: reconnect readonly=False only if armed)
from ib_async import Option, Contract, ComboLeg, LimitOrder

# --- Two-leg SPXW 0DTE credit spread as a BAG combo (adjust strikes/expiry to live 0DTE) ---
EXPIRY = "20260721"        # 0DTE expiry (today); update to a live SPXW expiration
SHORT_K, LONG_K = 5600.0, 5595.0   # sell the 5600 put, buy the 5595 put (put credit spread)

short_leg = Option("SPXW", EXPIRY, SHORT_K, "P", "CBOE", tradingClass="SPXW", currency="USD")
long_leg  = Option("SPXW", EXPIRY, LONG_K,  "P", "CBOE", tradingClass="SPXW", currency="USD")
qual = ib.qualifyContracts(short_leg, long_leg)     # need conIds for the combo legs
print("qualified:", [(c.localSymbol, c.conId) for c in qual])

bag = Contract(symbol="SPX", secType="BAG", currency="USD", exchange="CBOE")
bag.comboLegs = [
    ComboLeg(conId=short_leg.conId, ratio=1, action="SELL", exchange="CBOE"),
    ComboLeg(conId=long_leg.conId,  ratio=1, action="BUY",  exchange="CBOE"),
]

order = LimitOrder("BUY", 1, 0.05)      # net-debit form for a BAG; tiny non-marketable price
order.tif = "DAY"; order.transmit = False
order.account = FA_MASTER               # FA master + modelCode (per research; cf. Test 4)
order.modelCode = mp.MODEL_S8           # "S8_ZERODTE"

try:
    st = ib.whatIfOrder(bag, order)
    print("ACCEPTED combo whatIf under modelCode -> initMargin:",
          getattr(st, "initMarginChange", None), " commission:", getattr(st, "commission", None))
except Exception as exc:
    print("REJECTED:", type(exc).__name__, exc)
# Watch ib.errors / console: distinguish (a) a models-don't-support-combos error,
# (b) code 321 read-only, (c) an unrelated contract/strike error. Note the EXACT code + text.
ib.disconnect()
```

**PASS (S8-as-model is ALIVE):** IBKR (UI and/or whatIf) **accepts** the multi-leg combo under
`modelCode="S8_ZERODTE"` — TWS lets the combo into the model and/or the whatIf returns an
`OrderState` with margin numbers → models **do** support multi-leg combos, so **S8 can be a
modelCode sleeve** and the "two models in one account" design stands. Record the exact accepted
form.
**FAIL (S8-as-model is DEAD → change the architecture):** TWS refuses the multi-leg holding, or
the whatIf is **rejected with an error indicating models don't support combos / multi-leg**
(record the exact code + text) → **S8 cannot be a modelCode sleeve.** Go to the **hybrid
architecture**: `S0_ALLWEATHER` = Model Portfolio sleeve, `S8_ZERODTE` = its own **dedicated
sub-account** under the same FA master (or, failing that, all-sub-accounts, no models). This is
an **architecture decision** — flag to Andrew immediately; do not proceed with S8-as-model.
**Ambiguous (code 321):** you only learned the read-only checkbox blocks the whatIf, not whether
combos are allowed — fall back to the **Step A UI check** (which needs no order) and/or the
stage-2 armed run to get the real answer.

---

### Test 1 — Post-allocation surfacing
**Question:** After the Reallocate, do `S0_ALLWEATHER` and `S8_ZERODTE` now appear in
`reqAccountUpdatesMulti` with a per-model NetLiq?

```python
# ... HEADER ...
for mc in (mp.MODEL_S0, mp.MODEL_S8):
    nlv = mp.net_liq_for_model(ib, ALLOCATED_ACCT, mc)
    print(f"{ALLOCATED_ACCT} / {mc:<14} NetLiq -> {nlv}")
# also dump every (account, modelCode) NetLiq row seen for the account:
for v in mp.read_model_account_values(ib, ALLOCATED_ACCT, ""):
    if getattr(v, "tag", "") == "NetLiquidation":
        print("  row:", v.account, repr(getattr(v, "modelCode", "")), v.value)
ib.disconnect()
```

**PASS:** both `modelCodes` return a numeric NetLiq, and the split is roughly **75% / 25%**
of the account's total NetLiq (e.g. on ~$1.1M: S0 ≈ $825k, S8 ≈ $275k).
**FAIL / surprise:** models still invisible (empty/None) after a completed Reallocate → the
"invisible-until-allocated" model is stronger than thought, or the Reallocate didn't take
(wrong account, name mismatch). Means the desk cannot read per-sleeve state until IBKR
decides the model is "invested," which the CRM/transport design must account for.

---

### Test 2 — Sizing parity
**Question:** Does `account NetLiq × 0.75` (our `sleeve_capital`) match the S0 model's
NetLiq that IBKR reports? Confirms our sizing math matches IBKR's model accounting.

```python
# ... HEADER ...
policy = mp.AllocationPolicy(ALLOCATED_ACCT,
                             {mp.MODEL_S0: 0.75, mp.MODEL_S8: 0.25},
                             label="75/25").validate()
total = mp.net_liq_for_model(ib, ALLOCATED_ACCT, "")           # account-level NetLiq (no model)
# fall back to plain account NetLiq if the no-model query is empty:
if total is None:
    for v in ib.accountValues(ALLOCATED_ACCT):
        if v.tag == "NetLiquidation" and not getattr(v, "modelCode", ""):
            total = float(v.value)
ours = mp.sleeve_capital(total, policy)                        # {model: dollars}
for mc in (mp.MODEL_S0, mp.MODEL_S8):
    ibkr = mp.net_liq_for_model(ib, ALLOCATED_ACCT, mc)
    print(f"{mc:<14} ours={ours[mc]:>14,.2f}  ibkr={ibkr}")
ib.disconnect()
```

**PASS:** `ours[S0]` ≈ IBKR's S0 NetLiq and `ours[S8]` ≈ IBKR's S8 NetLiq, within cash-drift
tolerance (a few %; models drift from exact weight as prices move and cash sits idle).
**FAIL / surprise:** a large or structured gap → IBKR's model NetLiq is **not** a simple
`total × weight` snapshot (e.g. it excludes/includes cash differently, or counts unrealized
P&L per model in a way our flat multiply doesn't). Means `sleeve_capital` is the wrong sizing
base and the rebalancer must read IBKR's per-model NetLiq directly rather than derive it.

---

### Test 3 — DECIDING TEST: tagged order without prior allocation
**Question:** Build a whatIf model-tagged order against a model on an account **NOT** yet
reallocated into it. Does IBKR **accept** (return a whatIf margin preview) or **reject**?
This decides whether the manual TWS **Reallocate is a REQUIRED one-time step** or whether a
model tag alone routes an order (making allocation a CRM-only bookkeeping entry).

> **⚠ GATE box.** A `whatIf` never transmits, but it still travels the order path, so the
> **Gateway "Read-Only API" checkbox** may reject it with **code 321 (Read-Only mode)**.
> **Run in two stages:**
> 1. **First, on the normal read-only setup** (HEADER as-is, checkbox ON). If you get code
>    321 / "Read-Only mode," you've learned **whatIf is blocked read-only** — record that and
>    STOP; do not arm on your own initiative.
> 2. **Only if Andrew explicitly approves arming** to get the answer: he arms the Gateway
>    (`arming.py` — toggles the Read-Only API checkbox OFF) and you reconnect
>    `readonly=False`. whatIf is still zero-transmit. **Disarm immediately after.** This is a
>    deliberate, logged, gated step — not a default.

```python
# ... HEADER ...  (stage 1: readonly=True; stage 2: reconnect readonly=False only if armed)
from ib_async import Stock, LimitOrder
built = mp.build_model_limit_order(
    "SPY", "BUY", 1, 1.00,                       # $1 limit: guaranteed non-marketable, preview only
    account=UNALLOCATED_ACCT, model_code=mp.MODEL_S0, as_of="2026-07-21")
built.order.transmit = False
try:
    state = ib.whatIfOrder(built.contract, built.order)
    print("ACCEPTED whatIf -> initMargin:", getattr(state, "initMarginChange", None),
          "commission:", getattr(state, "commission", None))
except Exception as exc:
    print("REJECTED:", type(exc).__name__, exc)
# also watch ib.errors / the console for code 321 (read-only) vs a model/account rejection.
ib.disconnect()
```

**PASS (accept):** IBKR returns an `OrderState` with margin numbers → a `modelCode` tag alone
books the order; **the manual Reallocate is NOT a hard prerequisite for routing** (it's then
just IBKR-side bookkeeping / display). Big simplification for the CRM: allocation could be a
data entry, not a required UI ritual.
**PASS (reject-as-unallocated):** IBKR rejects specifically because the account isn't in that
model → **the Reallocate IS a required one-time step per account**; the CRM flow must include
"reallocate in TWS" as a real, tracked step before the desk can trade the sleeve.
**Ambiguous (code 321):** you only learned the checkbox blocks whatIf — the design question is
still open; you need the stage-2 armed run to actually answer it.

---

### Test 4 — Account field: FA master vs client sub-account
**Question:** When placing a `modelCode` order, does IBKR expect `Order.account` = the FA
master (`DF8922141`) or the client sub-account (`DU…`)? Our `apply_model_fields` currently
sets the **client sub-account + modelCode**. Research suggested **master + modelCode**. Real
ambiguity — test both and see which IBKR accepts.

> **⚠ GATE box.** Same as Test 3 — this is a whatIf; run read-only first, and only do the
> armed variant with Andrew's explicit approval. Zero-transmit either way.

```python
# ... HEADER ...  (run against ALLOCATED_ACCT so allocation isn't the confounder)
def try_whatif(acct, model):
    o = mp.build_model_limit_order("SPY", "BUY", 1, 1.00,
                                   account=acct, model_code=model, as_of="2026-07-21")
    o.order.transmit = False
    try:
        st = ib.whatIfOrder(o.contract, o.order)
        return f"ACCEPTED (initMargin={getattr(st,'initMarginChange',None)})"
    except Exception as exc:
        return f"REJECTED ({type(exc).__name__}: {exc})"
print("account=DU sub  :", try_whatif(ALLOCATED_ACCT, mp.MODEL_S0))
print("account=DF mast :", try_whatif(FA_MASTER,       mp.MODEL_S0))
ib.disconnect()
```

**PASS:** exactly one form is accepted → that's the correct `Order.account` convention.
**Action on result:** if **master + modelCode** is the accepted form, `apply_model_fields`
(model_portfolio.py:226) needs to stamp the FA master, not the client sub-account — that's an
**order-affecting change** (bump `paperbot/version.py` + CHANGELOG) and must be flagged to
Andrew before editing. If the **DU sub** form is accepted, the current code is correct — bank
the confirmation.
**Surprise:** both accepted, or both rejected → dig into the error text; "both accepted" means
the tag is what matters and account is cosmetic (verify booking lands in the right slice via
test #5), "both rejected" points back at arming/allocation, not the account field.

---

### Test 5 — `read_model_positions` with a real position
**Question:** Once a sleeve actually **holds** a position (created via TWS, see prereq), does
`read_model_positions` return per-model rows **keyed by modelCode**? This proves the no-op-stub
workaround collects real data — we only ever tested the empty case.

```python
# ... HEADER ...  (READ-ONLY; positions must already exist via TWS)
rows = mp.read_model_positions(ib, account=ALLOCATED_ACCT, model_code="", timeout=8.0)
print(f"rows: {len(rows)}")
for p in rows:
    print(f"  acct={p.account} model={p.model_code!r} "
          f"{getattr(p.contract,'symbol','?')} pos={p.position} avg={p.avg_cost}")
ib.disconnect()
```

**PASS:** one or more `ModelPosition` rows come back, each carrying the correct `model_code`
(`S0_ALLWEATHER` / `S8_ZERODTE`) and a non-zero `position` — proving the wrapper's temporary
collector captures the `positionMulti` stream against a live allocated model.
**FAIL / surprise:** zero rows despite TWS showing the holding, or rows with **empty**
`model_code` → the ib_async no-op-stub workaround doesn't actually harvest per-model data
against a real model. That would undercut per-sleeve reconciliation and force the upstream
`positionMulti` fix (SPEC §3 TODO) before the design is usable.

---

### Test 6 — FUNGIBILITY (critical, load-bearing)
**Question:** If **both** sleeves hold the **same** instrument (both `S0_ALLWEATHER` and
`S8_ZERODTE` long **SPY**), does IBKR return **TWO** position rows keyed by `modelCode`, or
**net** them into one? The whole design rests on the answer being "two, per-model."

**Setup (TWS-UI, read-only from the desk):** in TWS give **both** models SPY as a target
holding and Reallocate `ALLOCATED_ACCT` so each model buys some SPY — or hand-enter a small
SPY order tagged to each model in the TWS order ticket. Confirm in the TWS positions view that
`ALLOCATED_ACCT` shows SPY under **both** models before running the read.

```python
# ... HEADER ...  (READ-ONLY)
rows = mp.read_model_positions(ib, account=ALLOCATED_ACCT, model_code="", timeout=8.0)
spy = [p for p in rows if getattr(p.contract, "symbol", "") == "SPY"]
print(f"SPY rows: {len(spy)}")
for p in spy:
    print(f"  model={p.model_code!r} pos={p.position} avg={p.avg_cost}")
# contrast with the model-blind view (expected to net):
plain = [q for q in ib.positions(ALLOCATED_ACCT) if getattr(q.contract,'symbol','')=="SPY"]
print("plain ib.positions() SPY rows:", [(q.position) for q in plain])
ib.disconnect()
```

**PASS (design holds):** `read_model_positions` returns **TWO** SPY rows — one
`model_code='S0_ALLWEATHER'`, one `'S8_ZERODTE'` — with **separate** positions/avg costs,
while plain `ib.positions()` shows a single netted SPY. This is the load-bearing confirmation
that `model_share_deltas` can size the two SPY sleeves independently and never net them.
**FAIL (design broken):** only **one** SPY row from `read_model_positions`, or the two rows are
netted/indistinguishable → IBKR does **not** keep per-model position identity for a fungible
instrument via this API path. That invalidates the core sleeve assumption (SPEC §2
"Fungibility") and must stop the design pending a rethink — **flag immediately, do not paper
over it.**

---

### Test 7 — Per-model P&L / reporting surface
**Question:** What tags does `reqAccountUpdatesMulti` return **per model**? Can we get
per-model **P&L** (`UnrealizedPnL` / `RealizedPnL`) for client reporting, or **only** NetLiq?

```python
# ... HEADER ...  (READ-ONLY; run after a sleeve holds a position so P&L is non-trivial)
for mc in (mp.MODEL_S0, mp.MODEL_S8):
    vals = mp.read_model_account_values(ib, ALLOCATED_ACCT, mc)
    tags = sorted({v.tag for v in vals})
    print(f"\n{mc} — {len(vals)} rows, {len(tags)} tags:")
    for v in vals:
        if v.tag in ("NetLiquidation", "UnrealizedPnL", "RealizedPnL",
                     "GrossPositionValue", "TotalCashValue"):
            print(f"    {v.tag:<20} {v.value}")
    print("    ALL tags:", tags)
ib.disconnect()
```

**PASS (rich):** per-model rows include `UnrealizedPnL` / `RealizedPnL` (and ideally
`GrossPositionValue`) → the desk can produce true per-sleeve P&L for client reporting straight
from IBKR.
**Partial:** only `NetLiquidation` (and cash) come back per model → per-sleeve **P&L must be
computed by the desk** from per-model positions + cost basis (test #5 data), not read from
IBKR. Record which tags are actually present; that's the reporting contract.
**No modelCode-tagged rows at all:** per-model reporting isn't available via this call — falls
back to position-derived reporting only.

---

### Test 8 — readonly enforcement (belt-and-suspenders on the gate)
**Question:** Does a `readonly=True` connection genuinely refuse a tagged order? Confirms the
connection-level flag is a real wall, independent of the Gateway checkbox.

> **⚠ GATE box.** This test deliberately pokes the transmit boundary. Use the **zero-transmit
> probe pattern** from `paperbot/arming.py:probe_api_readonly` — it never places an order; it
> asks the Gateway to cancel a **fabricated, never-placed** orderId and reads the rejection
> code. Do **not** call `ib.placeOrder` with `transmit=True` here. Keep the HEADER's
> `readonly=True`.

```python
# ... HEADER ...  (readonly=True — do NOT change)
# Mirror arming.probe_api_readonly: a whatIf on a read-only connection should be refused.
from ib_async import LimitOrder
o = mp.build_model_limit_order("SPY", "BUY", 1, 1.00,
                               account=ALLOCATED_ACCT, model_code=mp.MODEL_S0,
                               as_of="2026-07-21")
o.order.transmit = False
try:
    st = ib.whatIfOrder(o.contract, o.order)
    print("returned a preview (connection did NOT block):", getattr(st,'initMarginChange',None))
except Exception as exc:
    print("refused (expected):", type(exc).__name__, exc)
# Inspect ib recent errors for code 321 / 'Read-Only mode'.
ib.disconnect()
```

**PASS:** the read-only connection refuses (error / code 321 "Read-Only mode"), or at minimum
never lets an order through → the read-only flag + Gateway checkbox are the walls the desk
relies on. (Note: if `whatIf` *succeeds* read-only that's fine here — it means whatIf is exempt
from the wall, which is the answer test #3 wanted; the wall is about **transmission**, and a
whatIf transmits nothing.)
**FAIL / surprise:** any path that actually transmits on a read-only connection → the gate is
not what we believe. Stop and escalate; that's a safety-critical finding.

---

### Test 9 — `modelCode` typo behavior
**Question:** Place a whatIf with a **deliberately wrong** `modelCode`. Does IBKR **reject** it,
or **silently misroute** (accept it against no/other model)? Informs how hard we must guard the
strings (our `require_known_model` guards our side; this tests IBKR's side).

> **⚠ GATE box.** whatIf — same staging as Test 3. Read-only first; armed only with Andrew's
> explicit OK. Note our own `build_model_limit_order` calls `require_known_model` and will
> raise on an unregistered code, so we bypass it here by building the order directly to reach
> IBKR with a bad string.

```python
# ... HEADER ...
from ib_async import Stock, LimitOrder
contract = Stock("SPY", "SMART", "USD")
order = LimitOrder("BUY", 1, 1.00); order.tif = "DAY"; order.transmit = False
order.account = ALLOCATED_ACCT
order.modelCode = "S0_ALWEATHER_TYPO"        # deliberately wrong (note the misspelling)
try:
    st = ib.whatIfOrder(contract, order)
    print("ACCEPTED despite bad modelCode -> initMargin:",
          getattr(st, "initMarginChange", None), " (SILENT MISROUTE RISK)")
except Exception as exc:
    print("REJECTED (good):", type(exc).__name__, exc)
ib.disconnect()
```

**PASS (IBKR rejects):** IBKR errors on the unknown `modelCode` → a typo fails loudly on both
sides; the string is still critical but IBKR is a backstop.
**FAIL / surprise (IBKR accepts):** IBKR silently accepts a bad code (routes to the base
account or a phantom model) → the `modelCode` string is a **single point of failure with no
broker-side guard**. Our `require_known_model` + byte-for-byte name discipline (SPEC §4) become
mandatory, and the CRM must never let a code be hand-typed. Record this — it directly sets how
paranoid the string-handling has to be.

---

## OPEN QUESTIONS to eyeball in TWS (not scripted — observe while you have access)

- **Drift behavior:** once an account is reallocated 75/25, does IBKR **auto-adjust** the split as NetLiq drifts (continuous), or is Reallocate a **one-time snapshot** the weights drift away from? (Watch the per-model NetLiq % after a day of price moves.)
- **Deposits/withdrawals:** how does new cash added to a reallocated account **split across models** — pro-rata to weights, into the base account, or does it require a fresh Reallocate?
- **Per-model buying power / margin:** does TWS show **per-model** buying power / margin within one account, or only account-level? (Affects S8's margin preflight when it's a sleeve.)
- **Reallocate cadence:** is the manual Reallocate **one-time-per-account**, or required **on every weight change**? (Determines whether the CRM's "policy change" needs a matching manual TWS action each time — a big UX/ops fact for the transport design in CRM_HANDOFF §7.)
- **Does UI Reallocate auto-invest** the account into each model's holdings, or only set target weights and wait? (Determines whether tests #5/#6/#7 need a separate buy step.)

---

## Results log (fill in as you go)

| Test # | Question | Result (PASS/FAIL/observed) | Implication / action | Date |
|---|---|---|---|---|
| 0 | **SHOWSTOPPER:** can a modelCode hold a MULTI-LEG combo (SPX 0DTE spread)? | | | |
| 1 | Post-alloc: do models surface w/ per-model NetLiq (~75/25)? | | | |
| 2 | Sizing parity: NetLiq×0.75 == IBKR S0 NetLiq? | | | |
| 3 | **DECIDING:** tagged order accepted without Reallocate? | | | |
| 4 | Order.account = DF master or DU sub? | | | |
| 5 | read_model_positions returns real per-model rows? | | | |
| 6 | **FUNGIBILITY:** two SPY rows per model, not netted? | | | |
| 7 | Per-model P&L available, or only NetLiq? | | | |
| 8 | readonly connection refuses a tagged order? | | | |
| 9 | IBKR rejects a typo modelCode, or misroutes? | | | |

---

### Gate / safety summary (read before running)

- **RUN FIRST — showstopper:** test **0** decides whether S8 can be a model at all. Its **Step A** is a pure TWS-UI check (no order, safest) and may answer it outright; **Step B** is a whatIf on a combo — same gate as #3/#4/#9. Settle #0 before spending effort on the rest.
- **Read-only, no setup needed:** tests **1, 2, 5, 6, 7** (positions in #5/#6/#7 are created in the **TWS UI**, never by the desk transmit path).
- **whatIf — touches the order path:** tests **0 (Step B), 3, 4, 9**. Run read-only **first**. If the Gateway returns code 321 ("Read-Only mode"), a whatIf requires **arming** (Andrew toggles the Read-Only API checkbox OFF via `arming.py`, then a `readonly=False` reconnect, then **disarm immediately**). whatIf is zero-transmit even when armed, but **arming is the gate** — Andrew's explicit, logged decision only.
- **Gate boundary probe:** test **8** uses the zero-transmit `arming.probe_api_readonly` pattern — never a real `placeOrder`.
- **Never** call `ib.placeOrder(..., transmit=True)` anywhere in this plan.
