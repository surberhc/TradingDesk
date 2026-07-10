# Reference Library

**What this folder is, in one line:** the two source documents that justify *why*
TradingDesk exists — they argue that passive "buy-and-hold the S&P" can fail investors
for 13–25 years at a stretch, and that a disciplined, regime-aware system is the answer.

Everything we build (S0 regime engine, S4 vol-control fund, S5 convexity overlay, the
condors) is a concrete implementation of the ideas in these two papers. This file is the
index and the plain-English digest. **Lead takeaway: our whole thesis — don't own full
equity all the time; dial exposure up and down on observable signals to protect the
compounding engine — is exactly what the source paper argues for.**

The PDFs themselves live in this folder but are **git-ignored** (`*.pdf` in `.gitignore`,
same rule that keeps the options warehouse out of git). Only this README is tracked.

**Separately**, `market_commentary_log.md` in this same folder is a growing, dated log of
external market commentary (Hedgeye, Tier1 Alpha, etc.) — as opposed to the static
founding-thesis papers indexed below, which don't change. Check there for ongoing
commentary digests and any candidate research leads spun off from them.

| File | Role |
|---|---|
| `Navigating_Lost_Decades_Final_revised.pdf` | The source paper (23 pp). Gorman, Keel, Randazzo. |
| `Tactical Options and Income.pdf` | A short derived summary (4 pp) that re-frames the paper toward options/income. |
| `AsymmetricReturns.pdf` | AllianceBernstein institutional white paper (11 pp). McKoan & Ning, Jul 2011. The cleanest outside statement of the S5 convexity/tail thesis. |

---

## 1. Navigating Lost Decades — Gorman, Keel & Randazzo

**What it is:** A 23-page research paper (CFA/CMT-credentialed authors) using Robert
Shiller's 155-year dataset to make the case for systematic, breadth-based regime
recognition over passive buy-and-hold.

**Core thesis (2–3 sentences):** "Stocks for the long run" is true on average but
dangerously incomplete — roughly 35% of U.S. equity history since 1871 sits inside
"lost decades" (1929–54, 1966–82, 2000–13) where buy-and-hold earned ~zero real return
for 13–25 years while enduring 50–77% drawdowns. The damage is not temporary; interrupted
compounding is *permanent* wealth destruction (a 13-year zero-return gap leaves you at 80%
of the steady-7% path forever; a 50% loss needs a 100% gain back). The fix is not to
predict tops but to **respond** to observable deterioration — using valuation as the
long-term risk backdrop and market **breadth** as the earlier, tactical regime signal.

**Key mechanisms / ideas:**

- **Lost decades are structural, not flukes.** Three U.S. episodes plus Japan (35 yrs)
  and Europe (Euro Stoxx 50 / FTSE, ~25 yrs from 2000). They arise from *different*
  causes (Depression, stagflation, dot-com/GFC) but produce the *same* investor experience.
- **Compounding-destruction math.** Drawdown recovery is asymmetric (50%→+100%, 75%→+300%);
  at 7%/yr a 50% drawdown takes ~10 yrs to recover, at 3%/yr it takes ~23 yrs. The cost of
  a lost decade is foregone growth that can never be recovered.
- **Valuation = context, not a timing signal.** CAPE at ~39.9 (99th percentile, beaten only
  by 2000's 44.2); Buffett Indicator ~190%, Tobin's Q and ERP also stretched. Starting CAPE
  explains ~24% of 10-yr / ~33% of 15-yr forward-return variance — meaningful but *not* a
  trigger. Top-quintile CAPE → 3.6% avg 10-yr returns with 24% of outcomes negative.
- **The "missing best days" myth — debunked.** 18 of the 20 best S&P days (1988–2025, 90%)
  occurred *below* the 200-day MA; 42% during bear markets. Best and worst days **interleave**
  in crises (Oct 2008, Mar 2020) — you cannot capture one without the other. So a defensive
  strategy that misses both is not giving up free return.
- **Breadth-first regime recognition.** Breadth (participation across stocks, A/D lines,
  new-high/new-low, % above key MAs) deteriorates *before* cap-weighted price indexes,
  giving earlier warning than trend alone. Cites Faber (2007), Moskowitz-Ooi-Pedersen (2012),
  Desmond (2002) breadth thrusts, Zweig, Jegadeesh-Titman (1993).
- **Four design principles** (these are essentially our regime-engine spec):
  1. **Multi-dimensional confirmation** — require several independent measures, not one knob.
  2. **Asymmetric implementation** — tops form *gradually* (incremental de-risking); bottoms
     form *abruptly* with breadth thrusts (decisive re-engagement).
  3. **Systematic execution** — rules at fixed intervals, no discretion (kills behavioral bias).
  4. **Duration filters** — signals must *persist* before acting (cuts whipsaw).
- **Honest about headwinds:** 2010–2020 punished breadth/trend systems (policy-driven
  V-recoveries triggered defensive signals just as markets resumed). The paper accepts this
  cost openly — the asymmetry argument is that boom-time underperformance is *recoverable*
  while a lost-decade drawdown is not.

### How it maps to TradingDesk

This paper is essentially the **prose specification of S0 and the regime engine.** Direct hits:

- **S0 adaptive all-weather / regime engine** — The paper's "breadth + trend + duration
  filter, multi-dimensional confirmation, asymmetric tops vs bottoms" *is* S0. Our open
  problems map one-to-one onto the paper's own admissions:
  - Our finding that the bleed is **re-entry lag + shallow-dip whipsaw, not crisis exits**
    is the paper's "asymmetric implementation" (decisive re-engagement) and "duration filter"
    principles. The paper agrees the hard part is re-entry, not the exit.
  - Our `REGIME_TREND_MARGIN=0.03` early-exit fix and the 200d-MA fragility work live exactly
    where the paper says trend confirmation belongs.
  - The paper's "2010–2020 headwind / policy-driven V-recoveries" is the *same phenomenon*
    as our S4 "V-bottom gap" — and it openly concedes trend systems get whipsawed there.
- **No-curve-fit research discipline** — Strong corroboration. The paper repeatedly insists
  on *response, not prediction*; multi-indicator confirmation over single-knob timing;
  duration filters over reactivity; and it honestly reports where the approach underperforms.
  That is our discipline stated by an outside source — useful as a sanity check that our
  "gate hard, plateau-not-peak, structural-fix-over-overlay" instincts match the literature.
- **S4 SPX vol-control fund** — Indirect but aligned: S4 is a *mechanical* exposure dial
  (target_vol / realized_vol). The paper would call vol-targeting a useful but *subordinate*
  lever — exactly our own prior conclusion (vol-trim is subordinate; the real lever is the
  regime band / re-entry ladder). The paper's compounding-preservation framing is S4's
  reason-to-exist.
- **S5 financed convexity overlay** — The paper's "preserve compounding capacity, re-engage
  decisively at breadth thrusts" is the strategic case for S5's permanent uncapped tail hedge
  *and* for the monetize-tail→redeploy mechanism (turn a crisis bottom into dry powder). The
  "best/worst days interleave in crises" point is a direct argument for owning convexity
  rather than trying to dodge the down days by going flat.
- **S2 / S3 condors** — The paper's warning is the guardrail here: short-premium income is
  **not free yield**; it is equity risk with capped upside. Condors should be *regime-gated*
  (size down / widen / stand aside when breadth + trend break), never sold mechanically every
  cycle. See the summary doc below for the explicit regime→structure mapping.

---

## 2. Tactical Options and Income (derived summary)

**What it is:** A short (4-page) summary that takes the Lost Decades paper and translates
its conclusions specifically into an **options-income** lens — i.e. how to chase equity-like
returns with less equity-direction dependence.

**Core thesis (2–3 sentences):** Don't "solve" lost-decade risk by swapping equity beta for
option-income beta — a static covered-call or short-put program is *still* equity risk and
will suffer in the same drawdowns the paper warns about. Instead build a **regime-aware,
rules-based options allocation** that blends premium income, controlled equity participation,
collateral yield, and dynamic de-risking. Goal: equity-like outcomes that don't require the
S&P to rise every year.

**Key mechanisms / ideas:**

- **Option income is not free yield** — selling premium blindly = capped upside + full
  downside; must be regime-aware.
- **Breadth should pick the *structure*, not just the size.** Explicit regime ladder:
  - *Risk-On* (broad participation, positive trend): partial covered calls, put spreads,
    call spreads, buffered/defined-risk bullish exposure — preserve upside.
  - *Neutral / Distribution* (valuations high, leadership narrowing, internals weakening):
    reduce net delta, less naked premium, collars, put-spread financing, call overwriting,
    more Treasury collateral.
  - *Risk-Off* (trend breaks, breadth deteriorates, vol expands, credit stress): stop
    treating premium as yield — collars, long puts, low-delta income only, mostly T-bills;
    objective = preserve compounding capacity.
  - *Recovery / Re-Engagement* (breadth thrust, vol normalizing): re-add equity-linked
    exposure; you don't need the exact bottom, just to avoid staying defensive too long.
- **Multiple return sources** — premium + tactical equity + collateral yield +
  vol-aware sizing + defensive de-risking.

### How it maps to TradingDesk

- **S2 / S3 condors** — This is the document that most directly governs the condors. The
  four-regime structure ladder is a ready-made gating spec: condor width, delta, and whether
  to trade at all should key off the same regime state S0 computes. "Premium is not yield"
  is the headline caution.
- **S5 financed convexity overlay** — The "calm-day 0DTE selling finances a permanent tail
  hedge" design is precisely the summary's *combine return sources + de-risk dynamically*
  thesis: harvest premium when calm, but the convexity (not the premium) is the point. The
  Risk-Off → "stop treating premium as yield, prioritize protection" rule is the guardrail
  that keeps S5's income leg from quietly becoming the dominant risk.
- **S0 / regime engine** — Reinforces that the *same* regime signal should drive *option
  structure selection*, not just an equity weight. A practical extension: S0's output
  regime → an options-structure recommendation.
- **S4 vol-control fund** — "Vol-aware position sizing" as one of several return sources is
  literally S4's mechanism, framed here as one ingredient in a broader blend.

---

## 3. Seeking Asymmetric Returns — McKoan & Ning (AllianceBernstein)

**What it is:** An 11-page institutional white paper from AllianceBernstein (J.J. McKoan,
Director–Absolute Return Investments; Michael Ning, Senior Quantitative Analyst; July 2011),
written in the wake of the 2008 crisis. It is a manifesto for "asymmetric returns" — and the
single cleanest external statement of the thesis behind S5.

**Core thesis (2–3 sentences):** The job of investing is the management of *risk*, not the
chasing of return (Ben Graham). A linear long-only payoff is a coin toss — equal odds of a
large gain or a large loss — but you can **bend the return profile** so you keep most of the
upside while capping the downside, i.e. buy **convexity**. The key to sustainable compounding
is a **dynamic risk-management process that limits the probability of large losses** — because
fat-tailed crashes do *permanent, non-linear* damage (a 50% loss needs a 100% gain back), and
**a handful of large losses can wipe out a long string of winning years**. "Winning by not
losing" — defense wins championships.

**Key mechanisms / ideas:**

- **Convexity is the prize.** A positive-convexity payoff has more upside than downside (in
  bonds: a convex bond rallies *more* as rates fall and sells off *less* as they rise). The
  search for convexity at a *reasonable cost* is the whole game; the challenge is that buying
  it is usually expensive.
- **Exploit other people's constraints.** Asymmetric opportunities are *event-driven* and come
  from forced behavior: utility preferences (corporates over-paying to hedge FX), regulatory
  constraints (insurers *forced* to dump "fallen angels" at the bottom → they then outperform),
  and liquidity/financing gaps (CDS-vs-cash-bond basis trades). The unconstrained investor
  takes the other side. Many of these trades are *not directional bets* and have low/negative
  correlation to the market.
- **Volatility is the common link.** Vol is priced almost uniformly across asset classes and
  correlations go to 1 in a crisis — which is *why* you can "macro hedge" one asset's tail with
  a derivative in another, and why diversification alone fails exactly when you need it.
- **Insurance is cheap before a crisis, ruinous after.** The cost of insuring a credit
  portfolio spiked in late 2007, *well before* Lehman. Once everyone is running for the exits,
  protection is prohibitively expensive — so you must own it *before* the spike, not buy it
  during. Tail events are essentially unpredictable (Enron taught you nothing about Lehman), so
  the answer is *standing* protection, not a timing call.
- **Far-OTM S&P puts are the named tail-hedge instrument.** "We have found that put options on
  the S&P 500 Index are the most effective and liquid instrument available for dynamically
  hedging tail risk… buy far out-of-the-money options to reduce the cost of protection." That
  is S5's tail leg, named explicitly by an outside institution.
- **The collar finances protection.** In the FX example, an investor *sells a put to finance a
  long call* (a "collar"), so the premium collected offsets the cost of the hedge — especially
  attractive when puts are rich relative to calls. That is exactly S5's *income-finances-
  protection* mechanism, stated generically.
- **Dynamic risk management = "winning by not losing."** Stop-loss strategies can systematically
  thin the left tail and make the return distribution more "normal" / options-like. The paper is
  *honest* that they are not foolproof: in a crash, **a stop-loss can fail to fill if liquidity
  vanishes** and there are no buyers at the limit price. Counterparty risk on OTC hedges is
  flagged too (diversify dealers; post collateral).

### How it maps to TradingDesk

This is the **cleanest external statement of the S5 financed-convexity thesis** — it says, in an
institution's own words, what S5 is trying to do.

- **S5 financed convexity overlay** — Direct, multi-point hit:
  - *Dial convexity, not direction.* The paper's whole frame is "bend the return profile / buy
    convexity," and it stresses these are *not directional bets*. That is S5's core distinction
    (dial CONVEXITY not delta) verbatim from an outside source.
  - *The instrument is named.* Far-OTM S&P 500 puts as the tail-hedge tool = S5's permanent,
    uncapped tail leg.
  - *When to own it.* "Insurance is cheap before a crisis, ruinous after; tail events are
    unpredictable" is the strategic justification for owning the tail **permanently** rather
    than trying to time it on — the *when-to-own-it* logic S5 already assumes.
  - *The collar = income-finances-protection.* Sell-a-put-to-fund-a-call is precisely S5's
    "calm-day premium selling finances the tail" mechanism.
- **S0 / regime engine** — "Winning by not losing," stop-losses that thin the left tail, and "a
  few large losses wipe out years of gains" are the *compounding-preservation* argument that is
  S0's reason to exist, restated. The honest caveat that **stops can fail to fill in a liquidity
  vacuum** corroborates our own bias toward *standing exposure control* over reactive stop-outs.
- **S2 / S3 condors** — Reinforces the existing guardrail: the value of the short-premium leg is
  to *finance* convexity, not to be the return source. The paper's whole point is that the
  convexity is the prize, not the harvested premium.

**Honest caveat (scope):** Most of the paper's concrete trade examples — fallen-angel bonds,
high-yield CDS-vs-cash *basis* trades, FX carry collars, Tier-1 bank debt — are **credit and
cross-asset arbitrage that sit OUTSIDE our equity-options scope**. They are valuable as
*illustrations of the convexity principle and of exploiting others' constraints*, not as
strategies for us to build. The transferable content is the principle (convexity, cheap-
insurance timing, collar financing, dynamic risk management), not the specific cross-asset
trades.

---

## Candidate research leads — UNVETTED, NOT ADOPTED

These are ideas the two documents *suggest*. They are leads to test under our normal
no-curve-fit discipline (gate hard, alert-only first, out-of-sample, plateau-not-peak).
**None of these is adopted. Each carries curve-fit risk; flagged inline.**

1. **Add a breadth dimension to the S0 regime engine.** The paper's strongest empirical
   claim is that breadth (A/D line, % above 200d, new-high/new-low) leads price. We currently
   lean on trend/MA. Testable: does a persistence-filtered breadth signal give *earlier exit
   and earlier re-entry* — directly attacking our known "re-entry lag" bleed?
   *Curve-fit risk: HIGH.* Many breadth series, many thresholds, easy to overfit; requires
   a real breadth data source we may not warehouse yet. Demand multi-indicator confirmation
   + duration filter from day one, exactly as the paper prescribes.

2. **Breadth-thrust as the S0 re-engagement trigger (asymmetric re-entry ladder).** The
   paper's "tops gradual / bottoms abrupt" asymmetry argues for a *decisive* breadth-thrust
   re-entry rather than a symmetric band. This is the closest thing to a structural fix for
   the re-entry-lag problem we already flagged as the real lever.
   *Curve-fit risk: HIGH.* Breadth-thrust definitions (Zweig/Desmond) are tunable; the GFC
   cold-start confound applies. Start alert-only, score on out-of-sample crises.

3. **Regime-gated condor structure ladder (S2/S3).** Adopt the summary's four-regime
   structure map: drive condor width/delta/participation off S0's regime state instead of a
   fixed monthly sale. Testable: does regime-gating cut the tail losses condors take when a
   calm regime breaks?
   *Curve-fit risk: MEDIUM.* The mapping is qualitative (good), but width/delta thresholds
   per regime are knobs — keep them coarse and shared across S2/S3.

4. **Regime-conditioned S5 income harvest.** Use S0's regime to throttle the 0DTE
   premium-selling leg — full harvest only in Risk-On/Neutral, throttle/halt in Risk-Off so
   the income leg can't dominate risk just as the tail hedge matters most.
   *Curve-fit risk: MEDIUM.* Conceptually clean; the throttle schedule is the fitted part.

5. **Valuation (CAPE) as a slow regime-context overlay, not a signal.** The paper is
   emphatic CAPE is context not timing. A *very coarse* CAPE-percentile state (e.g. only
   tighten risk bands when CAPE > top quintile) could bias the system toward caution when the
   backdrop is fragile — without trying to time anything.
   *Curve-fit risk: LOW-to-MEDIUM if kept coarse; HIGH if it becomes a timing knob.* The
   paper itself warns CAPE can stay extreme for years — so this must never gate entries on
   its own.

6. **Compounding-preservation as the primary backtest objective.** The paper reframes the
   goal away from "beat the S&P every year" toward "minimize permanent compounding impairment
   across full cycles." Lead: weight our strategy scoring toward terminal-wealth /
   deep-drawdown-recovery metrics over annual tracking error.
   *Curve-fit risk: LOW (it's an objective-function choice, not a fitted parameter)* — but
   verify it doesn't quietly select for strategies that just sit in cash.

7. **Buy tail protection while it's cheap, before vol spikes (S5).** *(from Asymmetric Returns.)*
   The paper shows the cost of insurance spikes *before* the crash and is ruinous once everyone
   is at the exit — an argument for owning S5's tail when calm and avoiding buying it into a
   vol spike. Lead: let regime context (vol cheap / calm) inform *when the tail is well-priced
   to roll/add*, never *whether* to be protected at all.
   *Curve-fit risk: LOW–MEDIUM as pure regime context; **HIGH the moment it becomes a timing
   rule** ("be unhedged when vol is low").* The paper's own thesis is that tail events are
   unpredictable, so this must stay a *cost-of-protection* read, not a hedge-on/hedge-off
   trigger. The permanent tail stays permanent.

8. **Stop-loss tail-thinning as a portfolio overlay.** *(from Asymmetric Returns.)* The paper
   reports stop-loss strategies systematically thin the left tail and make returns more
   options-like. Lead: test whether a coarse portfolio-level stop improves deep-drawdown
   recovery.
   *Curve-fit risk: HIGH.* Stop thresholds are directly tunable (easy to fit to one crisis), and
   — as the paper itself concedes — **a stop can fail to fill in a liquidity vacuum**, so a
   backtest will overstate its protection. Alert-only first; never trust a modeled fill at the
   stop price in a crash.
