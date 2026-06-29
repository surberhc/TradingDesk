# S5 — Financed Convexity Overlay on a Synthetic SPX Core

**Status:** SPEC drafted 2026-06-28; **V-bottom gate PASSED 2026-06-28 — edge = the always-on passive uncapped tail; active monetize→redeploy DEMOTED to a Phase-2 enhancement (see §1.1).** Ledger restructured into a priority waterfall (§4.3). **REAL-SKEW VALIDATION DONE 2026-06-28 (§1.2): the design SURVIVES honest skew-adjusted pricing — frontier shape identical, edge still the passive always-on tail; validated default tail shifts to ~0.50 notional / 20–25% OTM.** S5's **DEFENSIVE / tail half is now validated on real prices**; the **OFFENSIVE / harvest half remains the only open piece**, gated on the intraday 0DTE SPXW pull (~48h from full). Pending reserve sizing (now anchored on real skew) + the harvest engine on intraday data.
**Type:** Strategy specification + feasibility sketch. PAPER / research only. No code in this document.
**Author of concept:** strategy owner + Claude (this is a formalization of the owner's design, pressure-tested).

> **Working-title note.** "Financed Convexity Overlay" is accurate but dry. Alternatives considered:
> *Self-Financing Tail Carry*, *Permanent Hedge / Harvested Theta*, *Convexity-Control Fund* (parallels
> S4's "Vol-Control Fund"). Recommendation: keep **S5 — Financed Convexity Overlay** as the formal name
> and use *"Convexity-Control Fund"* as the marketing-parallel to S4 when the two are discussed side by
> side (S4 dials delta, S5 dials convexity — the names should rhyme).

---

## 1. One-line thesis

Hold a long-SPX core that **permanently carries a financed tail hedge**. A vol-control-style regime
signal (the S4 brain) flips a **short-option overlay** between two states: **HARVEST** premium on calm
days (0DTE SPXW selling that *finances the hedge's theta*) and **STAND DOWN** on rough days (stop
selling; let the already-owned long puts do their job). Unlike S4, which dials **DELTA** (cash vs SPY),
S5 dials **CONVEXITY**. Same signal brain, different lever.

The structural bet S5 is making — and the one thing it must be measured against — is:

> **You cannot buy convexity well *after* the spike (that is S4's re-entry-lag problem wearing a
> different mask). So you buy it *always*, cheaply, when calm — and you pay for it with harvested
> 0DTE theta. When the crash comes you already own the hedge, and its put delta has already
> de-risked the core all the way down; on the recovery that same delta re-risks you automatically —
> there is NO cash→equity re-entry decision to lag on. The owned hedge IS the re-entry fuel.**
>
> **UPDATE (V-bottom event study, 2026-06-28):** this re-entry-fuel *axis* is now **validated and
> large** — but the edge lives in the **passive always-on uncapped tail**, NOT in a discretionary
> "monetize the puts near the bottom" call. The active monetize→redeploy trigger was tested
> head-to-head and **demoted to a Phase-2 enhancement only.** See **§1.1** for the verdict.

---

## 1.1 V-bottom event-study verdict (2026-06-28) — the design pivot

The single most important open question (§10.1) — *does the financed-convexity book close S4's V-bottom
recovery gap?* — was tested in an EOD prototype head-to-head against S4 over GFC 2008-09, COVID 2020, and
the 2022 bear. Report: `backtester/output/s5_vbottom_eventstudy_20260628.md`. The gate **PASSED**, but it
splits into two **separable** findings that point in opposite directions and reshape the spec:

- **FINDING 1 — VALIDATED, robust, rule-independent: the re-entry-fuel AXIS is real and large.** Owning
  the hedge *in advance* removes the cash->equity re-entry **decision** entirely. At each crash bottom S4
  sits at **~0.16-0.38x exposure** (its documented re-entry lag) while the passive-tail S5 is **~1.0-1.3x**.
  Recovery capture (NAV gain / SPY gain from the bottom): **GFC ~118% / COVID ~123% / 2022 ~82%** for S5
  vs **~29% / ~21% / ~57%** for S4 — a **~90pp edge S4 structurally cannot replicate** with delta-targeting.
  **This validates the reason S5 exists.**

- **FINDING 2 — NEGATIVE: the ACTIVE monetize->redeploy TRIGGER does not earn its risk.** The causal rule
  fired only **twice in 19 years** (2008-10-15, 2020-03-13) and **both** times sold the hedge on a *dead-cat
  bounce* far above the true low, then levered the proceeds into a further **-24% (GFC) / -17% (COVID)** drop.
  Result: **DEEPER maxDD (-51% vs passive -38%)** and **WORSE Calmar (0.23 vs 0.29)**. The headline
  ">100% capture" was mostly a **decaying leverage surge, not bottom-timing** — and the sign of this finding
  is rule-dependent (faster triggers monetize even earlier and worse).

**THE PIVOT (load-bearing).** The edge lives in the **always-on uncapped passive tail** — its put delta
**auto-de-risks as spot falls and auto-re-risks as it recovers** (this *confirms* Fork 1's first-principles
logic, §4 below), NOT in a discretionary bottom-call. Therefore:

> **Active monetization is DEMOTED from a core mechanism to a cautious Phase-2 enhancement only.** If it is
> ever used, it must be a **slow, partial, LADDERED harvest, gated on intraday data** — **never** the
> all-in early surge that was tested and failed. The Phase-1 build centers on the **passive uncapped tail**;
> §3.2's old "sell the puts near the bottom and roll into equity" language is superseded accordingly.

**CAVEAT (do not over-read the raw return).** Hedge carry is **UNDERSTATED** in the study (flat-skew BSM,
no roll cost, no transaction cost), so the passive line's **~11% CAGR is optimistic** and a real-frictions
build will land lower. The **robust** wins are the **drawdown and Sharpe improvements** (passive maxDD
-38% vs SPY -55%, Sharpe 0.74 vs 0.54), **not** raw return. **→ This flat-skew caveat is now RESOLVED in §1.2.**

---

## 1.2 Real-skew validation of the tail-size sweep (2026-06-28) — the design SURVIVES honest pricing

The §1.1 caveat ("flat-skew BSM understates the deep/large tail carry → the passive CAGR is optimistic and
the sweet spot may drift") was the last EOD-answerable unknown on the **defensive / tail** half of S5. It is
now **resolved — favorably.** A real put-IV source was built from the **complete EOD SPXW chain** and the
**tail-size sweep was re-run on ACTUAL market skew**. Reports/code:
`backtester/output/s5_tail_sweep_realskew_20260628.md`, `s5_realskew_build_table.py` →
`output/s5_realskew_table.parquet` (2,132-day table), `s5_tail_sweep_realskew.py`; one surgical `tail_iv_fn`
hook added to the prototype engine (the default run verified **byte-identical**, 9.8719% / −28.27% / 0.35).

- **Measured 63-DTE put skew ≈ +0.71 vol-pts per 1% OTM** (+7 @10%, +10 @15%, +14.5 @20%, +18.4 @25% over
  ATM). The flat model had used a single **+6** for *every* strike — so it **UNDER-charged the 20–25% tail by
  8–12 vol-pts**, nearly **tripling** the full-notional deep-tail carry once priced honestly
  (1.74% → 4.46%/yr at full notional).

- **VERDICT: real skew TRIMS the numbers, it does NOT break the design.** The **frontier shape is identical**,
  the smallest tails remain a trap, and the **edge is still the passive always-on auto-de-risk tail** (NOT a
  bottom-call). The real-frictions cost is modest: **≈ −0.24% CAGR** at the new sweet spot (vs **≈ −0.36%** at
  the old 1.0/20% default) — the tail is **still cheap enough to run always-on.**

- **The validated default tail shifts toward smaller / closer — exactly as predicted:** **~0.50 notional /
  20–25% OTM** (the real-skew sweep picked **0.50 / 25% OTM**). The reserve is **larger in ratio** under real
  skew (≈ **0.7–2.1% of NAV** vs the flat model's 0.04–0.21%) but **still small and non-binding.**

- **Second-order finding (reinforces closer strikes).** Steep **crisis IV** makes a deep put's delta *less*
  negative at the bottom → a 100%-notional **deep** tail **auto-de-risks LESS** at the low (net delta ≈ 0.35
  vs ≈ 0.21 under flat vol) and **maxDD widens** (≈ −40% vs −28% on the calibrated GFC view). Closer-to-money
  strikes go ITM regardless of IV level, so they keep their de-risking bite in a crisis → the real-skew result
  **reinforces** the shift to **closer, smaller** tails.

- **Window caveat.** Direct real-skew pricing covers **2018+ only** (no real 2008 GFC chain). The GFC view is
  a **constant-slope CALIBRATED approximation** that brackets the pessimistic side — treat the 2008 numbers as
  an honest approximation, not measured.

**Bottom line:** S5's **DEFENSIVE / tail half is now validated on real market prices.** The frontier is real,
the always-on uncapped tail is the edge, and the default sizing is **~0.50 / 20–25% OTM**. The **only remaining
S5 unknown is the OFFENSIVE / harvest (income) half** — the real 0DTE harvest rate and the realized surplus
that funds Tier-2 + the upside barbell — which is gated on the **intraday 0DTE SPXW pull** (~48h from full).

---

## 2. Why this is distinct from S4 (and why both can coexist)

| | S4 — Vol-Control Fund | S5 — Convexity Overlay |
|---|---|---|
| Lever | **Delta** (exposure = `min(cap, target_vol/realized_vol)`; cash vs SPY) | **Convexity** (own long puts always; flip a *short* overlay on/off) |
| De-risking mechanism | Signal cuts exposure → must re-enter (timing problem) | **Put delta auto-de-risks** as spot falls; no re-entry decision for the core |
| Crash behavior | Sells down, sits in cash, **lags the V-bottom** (proven, industry-unsolved) | Already long the hedge; **monetizes the convex put into re-entry fuel** near the bottom |
| Cost of insurance | Opportunity cost (bull-market give-up / target-vol drag) | **Explicit cash cost** (hedge theta), *netted against harvested 0DTE income* |
| Instrument | SPY + T-bills (cash) | SPXW/SPX options complex (synthetic core + short overlay + long tail), one 1256 family |
| Tax | Equity / ordinary | **Section 1256 60/40** on the whole book |

S4 and S5 are not substitutes — they answer the same question ("how do I hold equity through a crash
without eating the full drawdown?") with **opposite tools**. S4's honest, documented failure mode (can't
catch the V-bottom) is *precisely* the gap S5 is designed to attack. Building both lets us measure,
on our own data, whether convexity-financing actually beats delta-targeting on the re-entry problem.

---

## 3. Structure (the book)

Everything lives inside the **SPXW / SPX cash-settled, European, Section-1256 options complex**, so the
core, the short premium, and the long protection net against each other for margin and tax.

### 3.1 Core — synthetic long SPX ("combo")
- **Long ATM call + short ATM put**, same expiry ≈ **long 1× the index** (put–call parity).
- Tenor: a **longer-dated** combo (e.g. quarterly / 60–120 DTE), rolled before expiry.
- **Why synthetic, not SPY/ES:**
  - No assignment / pin risk (European, cash-settled).
  - **Section 1256 60/40** tax treatment on the entire book (see §9).
  - **Portfolio-margin netting** — the long protective puts *reduce* margin against the short overlay
    and the short put leg of the combo, so the hedge partly pays for itself in capital efficiency.
  - Deepest 0DTE SPXW liquidity in the world for the overlay leg.
- **Cost to model honestly:** the synthetic embeds a **financing/carry leg ≈ the risk-free rate** (the
  combo prices in `r − q` forward drift). It is NOT free leverage. The backtest must charge this carry
  (it is the same `r` the S4 TR/ER accounting already handles — reuse that discipline). The combo must
  be **rolled**, incurring roll slippage.

### 3.2 Protection — the permanent, ALWAYS-ON tail (the heart of S5)
- **Longer-dated downside puts, 30–90+ DTE, bought when calm and IV is cheap**, continuously rolled so
  the book is *never* without protection.
- This is the inviolable rule: **own convexity before you need it, never chase it after the spike.**
- Layering (see Fork 2): a **deep-OTM uncapped tail layer** + optionally a **financed put-spread layer**
  nearer the money. The 0DTE income decides how much of each is affordable each cycle.
- **VALIDATED DEFAULT TAIL SIZING (real-skew sweep, §1.2):** **~0.50 notional / 20–25% OTM, 63 DTE.** This
  is the real-prices-validated sweet spot — it buys back meaningful rebound capture for a small drawdown-
  cushion give-up, and survives honest skew pricing at ≈ −0.24% CAGR carry. Tenor **63 DTE** confirmed optimal
  (30d bleeds, 90d worse). NOTE the *smallest* tails (0.25 / 10%) are a **trap** — too little notional close
  enough to bite a −55% crash, so they give up *both* rebound *and* cushion (the frontier is not monotone).
- **Re-entry mechanism (REVISED per §1.1 — the structural answer to the V-bottom lag):** the passive,
  always-on tail's **put delta is the re-entry engine**. As spot falls the put delta marches toward −1 and
  auto-de-risks the constant core; on the recovery it auto-re-risks the book back up — **with no signal and
  no discrete re-entry decision to lag on.** This is the validated, rule-independent edge (Finding 1).
- **Active monetization — DEMOTED to a Phase-2 enhancement (was a core mechanism; superseded by §1.1).**
  The original idea — "when the puts balloon near the bottom, *sell them and roll proceeds into cheap
  equity*" — was tested causally and **failed to earn its risk**: it monetized on dead-cat bounces and
  levered into the continued drop, *deepening* drawdown (Finding 2). It is therefore **not** part of the
  Phase-1 build. If revisited in Phase 2, it must be a **slow, partial, LADDERED** harvest gated on intraday
  data — **never** the all-in early surge that failed.

### 3.3 Overlay — the financing engine (regime-gated 0DTE selling)
- **CALM regime:** SELL short-dated **0DTE SPXW** premium (strangles / condors / put-spreads) against
  the core. 0DTE ⇒ **flat by the close** ⇒ no overnight gap risk on the shorts. Day-selection and
  intraday management *is* the S2/S3 engine.
- **ROUGH regime:** **STOP selling.** 0DTE makes standing down clean (nothing to unwind overnight). The
  already-owned long puts gain value and cap the core.
- The collected premium's job is exactly one thing: **finance the protection's theta.** It is *financing,
  never the main bet* (see Design Rule A).

### 3.4 The signal (regime classifier)
Reuse the S4 realized-vol estimator as the spine, optionally enriched by the S1 gamma/term-structure
regime:
- **CALM** ⇔ low/falling realized vol (S4's `max(fast, slow)` estimator) **AND** dealer gamma positive
  (S1) **AND** term structure in contango (VIX < VIX3M).
- **ROUGH** ⇔ realized vol rising **OR** gamma flips negative **OR** backwardation (VIX > VIX3M).
- Start with the **S4 vol estimator alone** (it is built, validated, and on-disk daily) as the v1 gate;
  add the S1 gamma/term-structure confirmations as v2+ once S1 is calibrated. This mirrors S3's
  "fixed control first, adaptive layers later" anti-curve-fit discipline.

---

## 4. THE TWO OPEN DESIGN FORKS — analysis + recommendation

### Fork 1 — Constant core vs. a core that also flexes with the signal

**Owner's leaning:** keep the core **CONSTANT** (pure convexity overlay), because the long puts already
deliver dynamic de-risking via their *delta*: a static core + static long puts automatically has a
**falling net delta as the market drops** — the put delta does the flexing for you, with no signal and
no re-entry timing.

**Analysis (first-principles).** This is sound, and the options math backs it cleanly:

- A long put has **negative delta that grows (toward −1) as spot falls** — that is just `−N(−d1)`
  marching toward −1 as the put goes in-the-money. So a *static* `long combo (delta +1) + long put`
  book has **net delta = 1 + δ_put(S)**, which **falls automatically and continuously as S drops**, with
  **no signal, no lag, no execution.** The put *is* the dynamic hedge, and it is a **convex** de-risker
  (it de-risks faster the further you fall — exactly the opposite of a stop-loss, which de-risks late and
  linearly).
- Flexing the core with the *same* signal would **duplicate** what the put delta already does, and worse,
  it would **reintroduce S4's re-entry-timing problem** — the very thing S5 exists to avoid. You would be
  re-coupling the core to a discrete regime flip (whipsaw-prone) instead of letting a continuous Greek do
  the work.
- It also keeps S5 **cleanly orthogonal to S4** (S4 = flex delta; S5 = flex convexity). Two strategies
  that flex delta off the same signal are one strategy; the distinction is the product.

**Where you *would* still need to flex the core** — and this is the important caveat:
- **If the protection is CAPPED** (pure put spreads — Fork 2), the put-spread delta **maxes out at the
  short strike** and goes *flat* below it. Past that point the auto-de-risking **stops** — net delta
  stops falling, and in a deep crash you are effectively **re-naked** with a static core and no more
  help from the hedge. *That* is the scenario where you'd be forced to flex the core manually (i.e.,
  cut the synthetic), reintroducing exactly the timing problem we wanted to avoid.
- Therefore: **"let the put delta be the variable exposure" is only fully valid if the tail is
  UNCAPPED.** The soundness of Fork-1's constant core is **conditional on Fork-2 keeping an uncapped
  layer.** (This coupling is confirmed in Fork 2 below.)

**RECOMMENDATION — Fork 1: AGREE with the leaning. Keep the core CONSTANT — *conditional on*
maintaining an uncapped tail layer (Fork 2).** Add one guardrail: define an explicit **net-delta floor**
(e.g. if net book delta falls below some bound because puts went deep ITM, that is *fine* and intended —
do **not** add core to "rebalance" it back up mid-crash; that would re-buy convexity high). The only core
action in a crash is the **monetization roll** (§3.2): harvest the convex put, then *add* equity at lower
prices — which raises delta deliberately, as re-entry, not as a hedge-defeating rebalance.

---

### Fork 2 — Outright puts vs. put spreads for the protection

**The trade-off, stated cleanly:**
- **Outright puts** = uncapped / true convexity + best re-entry fuel, but **expensive** (full theta
  bleed, negative carry).
- **Put spreads** = much cheaper (the short leg finances the long), but **CAPPED** — below the short
  strike you are naked again, *and the cap removes exactly the fat-tail payoff + re-entry fuel that the
  whole structure exists for.* A put spread is a bet on a *moderate* decline; S5's reason to live is the
  *immoderate* one.

**Owner's leaning:** a **LADDERED HYBRID** — keep an **UNCAPPED deep-OTM tail layer** (outright puts: the
catastrophe insurance + re-entry fuel, cheap *because* deep OTM) **+ optionally a financed put-SPREAD
layer nearer the money** for routine 15–25% corrections; let the 0DTE income decide how much of each is
affordable.

**Analysis + sizing reasoning (BSM, §6 has the full ledger):**
- **Deep-OTM tail is genuinely cheap.** A 20–25% OTM 90-DTE put carries at **~0.1–0.25% of notional/yr**
  (worst case, rolled, expires worthless). A 25% OTM put runs **~0.09%/yr**. This is the layer you must
  never give up — it is the uncapped convexity *and* it costs almost nothing. Buying the **uncapped tail
  is close to a free option** relative to the harvested income; capping it to save ~0.1%/yr is a terrible
  trade.
- **Near-the-money outright protection is what's actually expensive.** A 10% OTM 60-DTE put carries at
  **~1.2% of notional/yr**; 15% OTM 90-DTE ~0.7%/yr. This is the layer that *tempts* you into spreads.
- **The spread's economics are subtle — do not be fooled by raw "carry".** A naive carry calc makes a
  10%/20% put spread look *more* expensive per year than the outright 10% put (because the net debit is
  small and decays fast). That number is misleading: the right metric is **cost per unit of protection
  delivered within the protected band.** Inside 10–20% down, the spread protects at a fraction of the
  outright cost. **But it delivers ZERO of the tail/re-entry payoff below 20% down** — and the tail is
  the entire thesis.

**RECOMMENDATION — Fork 2: AGREE with the laddered hybrid, with a strict priority ordering:**
1. **Tier 1 (mandatory, never financed away): UNCAPPED deep-OTM tail.** ~15–25% OTM, 60–90 DTE outright
   puts. This is the catastrophe layer and the re-entry fuel. ~0.1–0.3% of notional/yr — pay it always.
2. **Tier 2 (optional, income-gated): a financed put-SPREAD** nearer the money (~5–15% OTM long / ~15–25%
   short) for routine 15–25% corrections. Buy this layer **only with harvested 0DTE income** — if a year
   is choppy and income is thin, **this is the layer that shrinks**, never Tier 1.
3. **Sizing rule:** size Tier 1 to a fixed notional fraction (it's cheap, keep it constant). Size Tier 2
   dynamically off a **trailing income budget** (e.g. only deploy Tier 2 debit up to *X%* of the last
   quarter's net harvested theta). This makes the *expensive* layer self-financing-or-absent, while the
   *cheap, essential* layer is permanent.

**Confirm the coupling to Fork 1:** **YES, confirmed and load-bearing.** "Uncapped tail + constant core"
form a **coherent pair**: the uncapped tail keeps the put delta marching to −1 in a deep crash, which is
what lets the constant core auto-de-risk all the way down with no signal. Capping the tail (pure spreads)
breaks that — the spread delta flattens at the short strike, the auto-de-risking stalls, and you are
pushed back toward needing to flex the core manually (S4's lag problem returns). So the two
recommendations are not independent choices; **they are one decision: keep an uncapped tail so the
constant core stays valid.** If a future variant ever caps the tail, Fork 1 must be revisited.

---

## 4.3 Self-Funding Hedge Ledger (core design principle)

S5 keeps an **INTERNAL running ledger of net premium harvested from the 0DTE selling**. Protection spending is **CONSTRAINED to what the ledger holds** — the hedge budget is **ENDOGENOUS, not a fixed parameter**. Good times (sustained calm, fat premium harvest) → the ledger grows → the strategy can fund more/better protection (further-dated, larger, or closer-to-money tail) → more firepower going into the next rough patch. Lean or twitchy times → the ledger is thin → it spends less → it avoids bleeding capital into hedges it cannot afford. This directly attacks the **financing-deficit / "chopped up in a twitchy market" risk**: the strategy can only spend what it earns, so it cannot bleed itself to death, and its **protection capacity COMPOUNDS with its own success.**

### The ledger is a strict PRIORITY WATERFALL (not "all income → more puts")

Harvested premium is **not** poured indiscriminately into ever-more protection. It fills a fixed sequence
of buckets, **each filled to its target before any flows to the next.** This is the spending discipline of
the endogenous budget:

1. **Tier 1 — deep uncapped tail (mandatory, small, always-on floor).** Funded **FIRST**, every cycle, from
   the minimum budget. ~15–25% OTM outright puts, the catastrophe layer + the re-entry engine (its put
   delta is the validated edge, §1.1). Cold-start/lean periods are **NEVER fully naked** because this
   bucket has top priority. (Cold-start wrinkle: at inception the ledger is empty, so **SEED it with a small
   upfront insurance budget** — like an expense ratio — to cover Tier 1 on day one while harvest accumulates.)

2. **Tier 2 — routine-correction protection (ledger-funded, SATURATING).** The financed put-spread layer
   nearer the money (routine 15–25% corrections), scaling with the pot **up to a "fully protected"
   ceiling.** This ceiling is a real **SATURATION point**: once the book is fully protected across the band
   you can actually use, **buying more puts just bleeds carry for downside you cannot benefit from.** Past
   saturation, Tier 2 stops accepting flow — the waterfall moves on.

3. **Reserve buffer (mandatory minimum balance).** A floor of banked premium that must **always** be
   maintained so the hedge program survives choppy/deficit stretches without running the ledger negative.
   Filled before any upside spending. **Full specification in §4.4.**

4. **Surplus above the reserve → UPSIDE convexity (the financed barbell).** Only premium *in excess of* a
   fully-funded reserve (Tiers 1–2 already saturated) may be spent here: **buy OTM calls / call-spreads when
   a low-vol "grind-higher" regime signal is on.** This makes S5 **long convexity on BOTH ends — downside
   tail and upside calls — both paid for by harvested theta** (the financed-barbell idea). Key properties:

   - **(a) It SELF-TIMES.** Surplus only accumulates in **calm, sustained uptrends** — the *exact* regime in
     which long calls pay off — and the ledger is empty/deficit in chop, the exact regime where calls would
     just bleed. So the **funding and the payoff are correlated to the same market state**: you can afford
     upside convexity precisely when it is most likely to work, and you can't when it isn't. The timing is
     structural, not a forecast.
   - **(b) GUARDRAIL — banked surplus only.** Spend **only realized/BANKED surplus.** **NEVER** sell *more*
     0DTE to fund the calls — that would pile on short gamma to buy long gamma and **violate the sacred
     Design Rule A (net convexity must stay LONG).** The upside bucket is funded by money already in the
     ledger, full stop.
   - **(c) House-money optionality.** Calls are **still negative carry**, so this is **house-money
     optionality bounded to surplus, never to capital.** A bad call year can only burn realized surplus; it
     can never touch the core or the mandatory hedge budget.
   - **(d) Realized aggressiveness SELF-THROTTLES.** S5 becomes **aggressive-growth-like (>100% upside
     participation) in calm bull runs** — when it can afford calls — and **reverts to defensive growth in
     chop**, when the ledger has nothing to spend. The aggressiveness dials itself.

   **Net positioning note:** S5 thus **self-categorizes as risk-managed Growth** that tilts toward
   *"aggressive growth, fully hedged"* **only WHEN the market is paying for it** — never on a discretionary
   bet, always financed by the harvest.

**This priority waterfall is a CORE design principle of S5** and should be tested as its own experiment
(separate from the V-bottom event study, which is kept clean). **Open question for the test:** does the
endogenous, waterfall-ordered budget (Tier 1 → saturating Tier 2 → reserve → upside) actually reduce
twitchy-market bleed and improve full-cycle results versus a flat fixed hedge budget?

---

## 4.4 The Reserve Buffer (bucket 3 of the §4.3 waterfall)

A **minimum ledger balance that must ALWAYS be maintained** so the hedge program survives choppy/deficit
stretches without ever running the ledger negative. This is the direct analogue of an **insurer's reserve /
option-budget float** — and specifically the **FIA carrier-reserve concept**: the carrier holds a reserve
so it can keep paying for the embedded option budget through years when nothing comes in to replenish it.

- **Purpose.** Carry **Tier 1 (+ minimum Tier 2) hedge cost through a harvest DROUGHT.** Choppy-no-crash
  years are the killer: you **stand down often** (few sell-days, thin harvest) **AND still pay the hedge
  theta** every day — a structural financing **deficit** (Design Rule B). The reserve absorbs that deficit
  so the program **never runs the ledger negative** and is never forced to drop the mandatory tail.

- **Sizing.** Cover roughly **N months/quarters of hedge carry with little-to-no harvest income.** Calibrate
  **N from the worst observed harvest-drought paired with its carry** (a data/backtest job — see §10
  backlog). **Until that calibration exists, use a conservative placeholder: ~1–2 years of Tier-1 tail
  carry.**

- **Held in T-bills.** The reserve sits in **T-bills so it earns the risk-free rate** rather than dead cash
  — it is a working float, **not a drag** on the strategy.

- **Replenish-FIRST priority.** After any period that draws the reserve down, **new harvest refills the
  reserve back to target BEFORE any upside (bucket 4) spending resumes.** Strict upstream-first ordering —
  the reserve is senior to the upside bucket, always.

- **Hysteresis.** Require surplus to **exceed the reserve target by a band** before deploying to the upside
  bucket, so the strategy does **not whipsaw in and out of buying calls** right at the reserve line. (Same
  hysteresis discipline as the regime gate, Rule C.)

---

## 5. Design rules & risks (the guardrails)

| # | Rule / Risk | Why it bites | Mitigation / measurement |
|---|---|---|---|
| **A** | **NET CONVEXITY MUST STAY LONG, ALWAYS.** Short premium is FINANCING, never the main bet; long tail must dominate. | A gap-down with net-short convexity can blow up the book (the S2 roster rule: "if paired, net tail must be LONG"). | Hard invariant in the engine: at every point, **book gamma/vega ≥ 0** (long-vol). The 0DTE shorts are *flat by the close* and *capped in size* by the cash-settled reserve (S3 mechanic). Assert it; reject any cycle that would flip the book short-convexity. |
| **B** | **Financing can run a DEFICIT** in choppy-but-not-crashing tape. | Not enough 0DTE premium on enough calm days to cover the protection's theta — vol is high enough to keep you standing down, but no crash arrives to pay off the hedge. **This bleed is the cost of insurance.** | **Measure it, do not assume it away.** Report the financing P&L as its own line (income − hedge theta) per regime-year. A negative year is expected and acceptable *if* the crash-year payoff justifies it. The honest test is the *full-cycle* ledger, not the calm-year ledger. |
| **C** | **Signal whipsaw** on calm↔rough flips. | Each flip = stop/restart the overlay → transaction costs + missed/forced selling; same whipsaw pathology S4/S0 fight. | Hysteresis band on the regime flip (separate calm→rough and rough→calm thresholds), exactly like the regime-engine tuning lessons. Start alert-only; gate hard. |
| **D** | **Intraday options execution complexity.** | 0DTE selling + intraday management + monetization timing is genuinely hard to model and to run; fills, slippage, partial breaches. | Cross-the-spread fills + commission (the S3 discipline). **Blocked on the Phase-1 intraday SPXW pull** (same gate as S2/S3) for the 0DTE-path realism; an EOD/daily approximation is buildable sooner (§8). |
| **E** | **Carry / financing leg of the synthetic** is real cost, not free leverage. | The combo prices in `r − q`; ignoring it overstates returns (same TR-vs-ER trap S4 documented). | Charge the financing rate explicitly; report **TR and ER** variants like S4. Reuse S4's cash/financing-rate knob. |
| **F** | **Roll risk** (combo + tail + spread all roll). | Roll slippage, calendar gaps, IV term-structure moves against you at the roll. | Model roll costs; stagger roll dates so the whole book never rolls on one session. |
| **G** | **Monetization timing is itself a market call.** | "Sell the puts near the bottom" assumes you can identify the bottom — you can't. Sell too early → leave convexity on the table; too late → give it back. | Rule-based, not discretionary: monetize on a **vol/▽-spot trigger** (e.g. partial-scale the puts as they cross delta/vega thresholds), ladder the harvest, and re-deploy into equity on a *ladder*, not a single shot. Accept that you will not nail the bottom — laddering is the hedge against the hedge-timing. |

---

## 6. Feasibility / cost ledger (back-of-envelope, BSM)

**Method:** analytic Black–Scholes (offline, numpy only, throwaway script — not retained). Assumptions:
SPX index ≈ 6000, `r = 4%`, `q = 1.5%` div yield, one SPX-multiplier core unit = **$600k notional**.
Long-run SPX realized vol ~15–16%; tail IV 18–24% (vol smile, term premium); calm-day 0DTE IV ~13%.
**These are analytic estimates — see §6.3 for what needs the warehouse to nail down.**

### 6.1 Cost side — annual carry of the tail hedge (rolled, worst case = expires worthless)

| Layer | Strike / DTE / IV | Price (pts / $) | Put delta | **Annual carry (% of notional)** |
|---|---|---|---|---|
| Near-money outright | 10% OTM, 60 DTE, 18% | 12.1 / $1,211 | −0.06 | **~1.23%** |
| Mid outright | 15% OTM, 90 DTE, 20% | 10.0 / $1,001 | −0.04 | **~0.68%** |
| Deep tail | 20% OTM, 90 DTE, 22% | 3.8 / $377 | −0.02 | **~0.25%** |
| Deep tail | 25% OTM, 90 DTE, 24% | 1.4 / $137 | −0.01 | **~0.09%** |

**Read:** a sensible permanent book — Tier-1 deep tail (~20–25% OTM) **+** a modest Tier-2 mid layer —
costs roughly **0.3%–1.0% of notional per year** in the worst case (no crash, everything expires
worthless). A pure deep-tail-only book is **~0.1–0.3%/yr** — almost free.

### 6.2 Income side — calm-day 0DTE harvesting

A **16-delta 0DTE strangle** at ~13% IV is worth **~6.4 pts ≈ $644 / contract / day** in gross credit.
Apply a **50% haircut** (realized losses on the inevitable not-actually-calm days, slippage,
breaches — conservative). Selling on a regime-gated fraction of ~252 sessions:

| Calm fraction (sell-days) | Days | Gross credit | **Net @ 50% keep** | **% of notional** |
|---|---|---|---|---|
| 40% | 101 | $64,921 | **$32,460** | **~5.4%** |
| 55% | 139 | $89,266 | **$44,633** | **~7.4%** |
| 70% | 176 | $113,612 | **$56,806** | **~9.5%** |

### 6.3 Bottom line — does calm-day income plausibly fund the tail?

**Yes — with a comfortable margin in a *normal* year, but the margin is the whole game and the
estimate is generous.**

- Even at the **pessimistic** end (40% calm days, 50% haircut → ~5.4% of notional income) against the
  **expensive** end of a real layered hedge (~1.0% carry), income covers the hedge **~5×** and leaves a
  surplus that is *itself* extra return (or funds the Tier-2 spread).
- The deep-tail-only book (~0.1–0.3% carry) is covered **20–50×** — essentially free insurance financed
  by harvested theta.
- **The catch (Design Rule B):** this is the *calm-year* ledger. The honest number is the **full-cycle**
  one. In a **choppy-but-no-crash** year you stand down often (few sell-days) *and* still pay the hedge
  → a **financing deficit**. The 50% haircut is a guess; the *real* haircut is fat-tailed (one ungated
  big-move day can wipe a month of credits). And the crash year, when the hedge finally pays, must repay
  several lean years at once. **The strategy is only justified across a full cycle, not on the calm-year
  surplus.**

### 6.4 What needs the actual warehouse (`C:\TradingDesk-Local\warehouse`) vs. what's analytic

| Quantity | Source | Confidence |
|---|---|---|
| Tail-put carry (above) | **Analytic BSM** | Rough — real IV smile/term premium makes OTM puts *pricier* than flat-vol BSM (skew). Needs warehouse EOD chains (SPX/SPXW have `delta, bid/ask, IV, underlying_price`) to price the **actual** skew-adjusted tail. |
| 0DTE strangle credit | **Analytic BSM @ 13% IV** | Rough — 0DTE IV and the *realized* path P&L need the **Phase-1 intraday SPXW pull** (1-min bid/ask + spot). Open-to-close badly understates 0DTE gamma risk (S2's whole point). |
| Calm-day fraction / haircut | **Assumed** | This is the big unknown. Needs the S4 regime signal run over history × the intraday 0DTE path P&L to get the *real* gated sell-day count and the *real* loss distribution on "calm" days. |
| Monetization payoff | **Not modeled** | The re-entry-fuel claim (does monetizing the ballooned put + re-deploying actually close the V-bottom gap?) needs a full event-study backtest over 2008/2020/2022. **This is the claim that decides whether S5 is worth building.** |

---

## 7. Reuse map — how S5 braids the existing roster + warehouse

| Existing piece | Where it lives | S5 role |
|---|---|---|
| **S1 — gamma / term-structure regime** | `features/gex.py`, GEX engine | **CALM/ROUGH confirmation**: dealer-gamma sign + VIX/VIX3M contango/backwardation enrich the v2+ regime gate (v1 uses S4 vol alone). |
| **S2 — Iron Condor Income (regime-gated)** | `Downloads/STRATEGY_2_*.md` | The **"better days, not better strikes"** day-selection logic *is* the overlay's calm-day filter. Intraday-path P&L requirement is inherited (same Phase-1 gate). |
| **S3 — Swiss Iron Condor + cash-settled reserve** | `backtester/s3_condor_control.py` | The **0DTE premium-selling engine** (strangle/condor build, fixed-delta control), the **cash-settled SPXW settlement** (`settle()`, 4-leg intrinsic on expiry-day close), and the **single-account reserve / non-100%-deployed sizing** (`risk_frac`/`reserve_util` caps) are reused directly for the overlay leg. Design Rule A (net tail long) extends S3's pairing note. |
| **S4 — vol-control signal brain** | `strategies/strategies/spx_vol_control.py` | The **`max(fast, slow)` realized-vol estimator** + the causal/no-look-ahead discipline + the **TR/ER financing accounting** (cash/`r` knob) are reused as the regime spine and the carry accounting. S5 is "S4's signal, convexity lever." |
| **Options warehouse (EOD)** | `C:\TradingDesk-Local\warehouse\raw\options\{SYM}\{YYYYMMDD}.parquet` (SPX/SPXW; `delta, bid/ask, IV, underlying_price, expiration, strike, right`) | Prices the **synthetic combo, the tail puts, and the put spreads** on real (skew-adjusted) chains for the EOD/daily version; supplies settlement underlying. |
| **Intraday SPXW pull (Phase 1, queued)** | per S2/S3 phasing | Unblocks the **realistic 0DTE overlay** (1-min path P&L, intraday regime exit, intraday monetization). The same blocker as S2/S3. |
| **IBKR forward collector** | `datacollector/ibkr_forward.py` | Extends the warehouse forward for free (paper-era data) so S5 paper validation has fresh chains. |
| **Backtester TR/ER + sweep harness** | `backtester/s4_vol_control.py` pattern | Template for the S5 runner (daily cadence, TR + ER variants, parameter sweep, markdown report). |

---

## 8. Phased build plan (gated like S2/S3)

### Phase 0 — Spec & fork decisions *(this doc)*
- Resolve Fork 1 (constant core — recommended) and Fork 2 (laddered hybrid, uncapped Tier-1 — recommended).
- Owner sign-off on the two recommendations before any code.

### Phase 1 — **EOD / daily version (buildable NOW, no new data)**
On warehouse EOD chains + on-disk daily prices/vol family — *no intraday pull required*:
1. **Synthetic-core + permanent-tail accounting**: build & roll the combo + Tier-1 tail on EOD chains;
   charge financing (`r`), roll slippage; TR + ER ledger (reuse S4 accounting).
2. **Daily regime gate (v1 = S4 vol estimator only)**: CALM/ROUGH flips on the validated `max(fast,slow)`
   signal; add hysteresis (Rule C).
3. **Overlay as a *daily* short** (EOD proxy for 0DTE): sell a 1-DTE-ish defined-risk condor/strangle on
   calm days using the S3 engine + cash-settled reserve; hold-to-expiry settlement. This is an
   *approximation* of the 0DTE path — explicitly flagged as understating gamma risk (S2's caveat).
4. **Monetization event-study — DONE (2026-06-28), gate PASSED with a pivot.** The headline experiment
   (does the financed-convexity book beat S4 on the V-bottom?) is run — see **§1.1**. Verdict: the
   **passive uncapped tail** is the edge; the **active monetize→redeploy rule was demoted to Phase 2.**
   Phase-1 build therefore centers on the passive-tail accounting, not an active bottom-call.
5. **Full-cycle ledger** (Rule B): income vs hedge theta per regime-year, TR + ER, vs S4 and vs B&H SPY.

**Phase-1 deliverable:** a standalone `s5_*` runner (S4-style), markdown report, head-to-head vs S4.

### Phase 2 — **Intraday version (BLOCKED on the Phase-1 intraday SPXW pull)**
Same gate as S2/S3 — needs 1-min SPXW 0DTE near-money quotes + spot + IV:
1. Replace the daily-condor proxy with the **real 0DTE intraday-path** overlay (S2/S3 intraday P&L).
2. **Intraday regime exit** (gamma flip / expected-move recompute) for cleaner stand-down.
3. **Intraday monetization** of the tail near the bottom (1-min marks).
4. Re-run the full-cycle ledger with realistic 0DTE fills; compare to the Phase-1 EOD approximation to
   measure how much the EOD proxy distorted.

### Phase 3 — Paper validation
- Run on the PAPER account (DU…, port 4002) once the backtest clears. Paper is the final judge, separate
  from the backtester (S3 discipline). Net-convexity-long invariant (Rule A) enforced live.

---

## 9. Tax & margin notes

- **Section 1256 (60/40):** SPX / SPXW / XSP are broad-based index options → **60% long-term / 40%
  short-term** regardless of holding period, **marked-to-market** at year-end. The *entire* S5 book —
  synthetic core, short overlay, long tail — sits in this regime. This is a genuine, structural edge over
  an SPY/equity version (especially for the high-turnover 0DTE overlay, which would otherwise be 100%
  short-term). Keep the whole book in the 1256 family for this reason; do **not** mix in SPY legs.
- **Portfolio-margin netting:** under portfolio margin, the **long protective puts and the long call of
  the combo reduce the margin requirement** on the short put leg and the short overlay — the risk-array
  stress sees the long convexity offsetting the short. **Net effect: the hedge partly pays for itself in
  capital efficiency** (less reserve tied up per unit of overlay). The backtest's reserve model (S3's
  `reserve_util`) should be extended to credit this offset rather than reserving each short leg in
  isolation — otherwise S5's capital efficiency is understated. *(Exact offset is broker-specific; model
  conservatively, confirm on the paper account's actual margin report.)*
- **Cash-settled, European:** no assignment, no pin risk, no early-exercise on the short legs — the
  reason the overlay can be "flat by the close" cleanly (Rule A's safety depends on it).

---

## 10. Open questions (resolve before / during build)

1. **THE headline unknown — RESOLVED 2026-06-28 (§1.1).** Does the re-entry-fuel mechanism close the
   V-bottom gap S4 can't? **YES on the axis** (passive owned tail removes the re-entry decision; ~90pp
   recovery-capture edge over S4), **NO on the active monetize→redeploy trigger** (demoted to Phase 2). The
   edge is the **always-on uncapped tail**, not a bottom-call. See §1.1 for the full verdict.
2. **Skew cost — RESOLVED 2026-06-28 (§1.2).** How much pricier is the *real* skew-adjusted tail than the
   flat-vol BSM estimate in §6? **Measured: 63-DTE put skew ≈ +0.71 vol-pts/1% OTM** (flat model used a flat
   +6 → under-charged the 20–25% tail by 8–12 vol-pts). It **trims** the numbers (≈ −0.24% CAGR at the new
   sweet spot) but **does not break the design**; the validated default shifts to **~0.50 / 20–25% OTM** and
   the reserve grows to **~0.7–2.1% of NAV** (still non-binding). Frontier shape identical, tail still the
   edge. The flat-skew caveat on the §1.1 prototype is now **closed.**
3. **Full-cycle financing deficit (Rule B):** across choppy-no-crash years, how deep does the bleed go,
   and does the crash-year payoff repay it? The calm-year surplus is reassuring but irrelevant in
   isolation.
4. **Combo vs. ES/SPY core:** is the 1256 + netting benefit worth the synthetic's financing-leg + roll
   complexity vs. a simpler SPY/ES core with a separate SPX hedge? (Lean: yes for tax + single-family
   netting, but quantify the roll drag.)
5. **Monetization rule design (Rule G):** what trigger ladder harvests the convex put without becoming a
   market-timing call? Needs the intraday data (Phase 2) to tune honestly.
6. **Regime signal:** S4-vol-only (built) vs. +S1 gamma/term-structure (blocked on S1 calibration) — how
   much does the gamma confirmation actually improve the calm-day gate? Build v1 on vol-only; measure the
   S1 lift as a separate experiment.

### Backlog tests (separate experiments — do NOT fold into the clean V-bottom study)

- **B1 — Upside-convexity waterfall (§4.3 bucket 4).** Test the financed-barbell: does spending *banked
  surplus* (above a fully-funded reserve) on OTM calls / call-spreads in a low-vol grind-higher regime add
  full-cycle return without breaking Design Rule A? Validate the **self-timing** claim (surplus accrues in
  the same calm-uptrend state where calls pay) and the **self-throttling** of realized aggressiveness.
- **B2 — Reserve sizing / calibration (§4.4).** Calibrate **N** (months/quarters of Tier-1 + min-Tier-2
  carry the reserve must cover) from the **worst observed harvest-drought paired with its carry**; until
  then hold the conservative **~1–2 years of Tier-1 carry** placeholder. Test the replenish-first ordering
  and the upside-deploy hysteresis band.
- **Data note for B1 + B2:** both ultimately want the **Phase-1 intraday SPXW pull** to nail the precise
  0DTE harvest (and thus the realized surplus / drought depth) numbers — but the **structure of both can be
  prototyped on EOD** first, like the rest of Phase 1.

---

*Reference files read for this spec: `datacollector/STRATEGIES.md` (S0–S4), `strategies/strategies/
spx_vol_control.py` (S4 brain), `backtester/s3_condor_control.py` (cash-settled condor + reserve),
memory notes s4-spx-vol-control-fund / tradingdesk-architecture / options-warehouse. BSM ledger via a
throwaway offline numpy script (not retained).*
