# Strategy Roster & Data Requirements

The backtesting program. Strategies are *tenants* that draw on the shared warehouse;
this file tracks each one and — critically — the data it needs, so we grab everything
required while the ThetaData subscription window is open.

## Data-grab status

| Layer | Scope | Status |
|---|---|---|
| **EOD options** | 49 roots, 8 yr, full chain (greeks/OI/IV/spot/quotes) | **In progress** (`download.py`) |
| **Intraday options** | SPX/SPXW (+SPY), 1-min, scoped DTE+strike band | **Queued** — see Strategy 2. Build after EOD grab, within the paid month |

ThetaData feasibility (probed live, 2026-06-25): Standard tier ($80) serves intraday
**bid/ask quotes** (`/v3/option/history/quote`, `interval=1m`) and intraday **IV +
underlying_price** (`/v3/option/history/greeks/first_order`). Second/third-order greeks
(incl. gamma) are **Pro-gated (403)** — we don't need them; we compute gamma from IV +
spot via BSM (`features/gex.py`). **No tier upgrade required.**

---

## S0 — Adaptive All-Weather Core  *(existing, separate repo: `backtester/`)*
- **Family:** tactical asset allocation / drawdown insurance. Judged on max DD / Calmar.
- **Data:** daily adjusted prices (Tiingo, free) + macro (Treasury/VIX/credit proxy). No options.
- **Status:** built, validated. Not part of this warehouse; listed for completeness.

## S1 — MSR Gamma/Vol Regime Overlay
- **Family:** volatility/regime sizing engine (NOT directional — amplitude only). Layers on a
  momentum/trend directional engine supplied elsewhere.
- **Edge:** dealer-gamma state + 1m/3m roll-off "twitchy/calm" → position sizing & hedge-cost timing.
  (Per MSR handoff: no directional or long-vol edge; sizing + hedge timing only.)
- **Data:** **EOD options** for the options-deep set (SPX, SPY, QQQ, GLD, XLB, XLI…). Already in the
  EOD grab. Realized-vol roll-off from Tiingo (free).
- **Status:** GEX engine built (`features/gex.py`). Next: calibrate to the 281-day Tier 1 Alpha set
  on the 2025–26 overlap, then re-run findings across 2018/2020/2022.

## S2 — Iron Condor Income (automated, regime-gated)
- **Family:** short-volatility options income. The practical *test* of S1 — its day-selection edge
  exists only if the regime signal genuinely flags big-move days in advance.
- **Edge:** "better DAYS, not better strikes" — only sell premium on regime-calm days; sit out the
  >1.5% move days that wreck short vol. Strike placement tests delta-based vs gamma-band-based.
- **Knobs (all swept):** strike placement (delta vs band) · wing width · distance from money · tenor 0–30 DTE.
- **Data — INTRADAY required** (this is what forces the intraday pull):
  1. **0DTE** must be evaluated on intraday P&L *paths* (gamma risk is concentrated intraday;
     open-to-close badly understates it).
  2. **Regime-triggered exit** needs gamma state / flip / expected-move recomputed *intraday*.
  - Specifics: 1-min bid/ask (4-leg P&L) + 1-min IV + underlying spot, for SPX/SPXW (primary) and
    SPY, across the condor strike band (body + wings) over the tested DTEs. Gamma computed by us.
- **Intraday pull plan (phased, per the handoff's "prioritize 0DTE first"):**
  - **Phase 1:** SPXW **0DTE** + near-money strikes (±~3–5% band), 1-min. Small (~GBs). Unblocks the
    0DTE intraday-path backtest and the regime-exit test.
  - **Phase 2:** widen to 1/7/14/30 DTE buckets and a wider strike band as the design firms up.
  - Full-chain-intraday-for-years is terabyte-scale and deliberately NOT attempted; we bound by
    DTE + strike band.
- **Status:** design captured (`Downloads/STRATEGY_2_IRON_CONDOR_INCOME.md`). Blocked on S1 calibration
  (shared regime features) and the Phase-1 intraday pull. Backtester build = condor P&L engine
  (4-leg, realistic costs, intraday-aware) → 3 exit modes → 2 strike methods → regime gate → sweep.
- **Pairing note:** natural opposite of a long-protection collar; if ever paired, design so the net
  tail is LONG, not short.

## S3 — Swiss Iron Condor (adaptive intraday seller)
- **Family:** intraday, multi-tranche, regime-aware premium-selling *engine* — serves BOTH iron
  condors AND 0DTE cash-settled covered calls. The intraday/adaptive evolution of S2.
- **Edge thesis (to be PROVEN, not assumed):** our gamma/vol regime work places strikes and picks
  days better than a fixed-delta seller. The **v1 fixed-delta control** is the benchmark every
  adaptive version (v2 bands → v3 +rvol → v4 +ivol) must beat *out-of-sample*. The control = the
  anti-curve-fit spine.
- **Three-layer stack:** Regime (master switch) → Morning/gap (a "wait & measure" gate, NOT a veto —
  gaps often precede muted days) → Placement (start from gamma bands known pre-open, add intraday
  realized/implied vol; tranches laddered wider early → narrower as time AND vol confirm).
- **Research agenda (must be measured first, §3):** morning settle-time · gap→day-range · morning-rvol
  →afternoon-range · strike-placement head-to-head · tranche-cadence. All conditioned BY REGIME
  (relationships likely invert calm vs twitchy).
- **Instrument/tax note:** for the covered-call mode, **cash-settled index options (SPX / XSP)** beat
  physical SPY (no assignment, European, Section 1256 60/40 tax). Hard mechanic: cash-settled short
  call is only "covered" if core + calls are in the SAME account + an explicit cash/margin reserve;
  the backtest must model that reserve (can't run 100% deployed).
- **Data — additive to S2:**
  - **+ XSP** (Mini-SPX, 1/10 notional) — EOD now, intraday later. *(NEW root — queued.)*
  - Intraday chain (bid/ask) + spot + IV, 0–30 DTE, for SPX/SPXW, SPY, **XSP**.
  - **Historical intraday-gap dataset** (overnight gap size, intraday path, close, intraday realized
    vol) for the §3 gap/settle/morning-vol studies — needs **intraday underlying** history. *(Open
    question: derive from the intraday options pull vs. add a dedicated ThetaData Indices/Stocks feed
    — see decision log.)*
- **Status:** SPEC = research agenda (`Downloads/STRATEGY_3...md`). Buildable NOW without regime data:
  the v1 fixed-delta control engine + single-account cash-settlement/reserve mechanics. Adaptive layers
  + regime gate BLOCKED on S1 calibration + intraday data. Paper account = final judge (separate from
  backtester).

## S4 — SPX Volatility-Control Fund  *(standalone; shared-brain strategy, `strategies/spx_vol_control.py`)*
- **Family:** single-asset volatility targeting. A faithful in-house replica of the FIA/RILA/annuity
  vol-control engine (S&P 500 Daily Risk Control is the reference spec). **NOT diversified** — no bonds,
  no real assets, no regime engine. One risk asset (SPX via SPY) + a cash leg.
- **Goal (specific):** hold the S&P 500 at a constant *target annualized volatility* by scaling exposure
  daily between T-bills and a leverage cap, so the fund delivers SPX-like-or-better **risk-adjusted**
  returns with a materially **smoother equity curve and shallower max drawdown** than buy-and-hold SPX.
  The objective is to know, empirically and on our own data, exactly what a pure vol-control fund **can
  and cannot do** — not to beat SPX on raw CAGR.
- **Core mechanic (universal vol-control formula):**
  `exposure_t = min(leverage_cap, target_vol / realized_vol_t)`, rebalanced **daily**; residual
  `(1 − exposure)` in T-bills (cash earns the risk-free rate; >100% borrows at it). Realized vol uses the
  asymmetric **`max(fast, slow)`** estimator (de-risk fast, re-risk slow — the FIA "headline steal").
- **Knobs (both swept — Andrew's call 2026-06-28):**
  - `TARGET_VOL` — annualized vol the fund holds constant (sweepable; FIA standard 10%, SPX runs ~15–16%).
  - `LEVERAGE_CAP` — max exposure (sweepable **1.0–2.0**; FIA/RILA standard 1.5×). Cap 1.0 = pure
    smoothing/unlevered; >1.0 = lever up in calm markets to chase the target.
  - Secondary: estimator windows (fast/slow), an optional rebalance band / max-daily-weight-change
    (turnover control), an optional observation lag, and the cash/financing rate (see construction).
- **Construction & measurement (from "Vol Control Funds Analysis" memo, 2026-06-28):** build a
  **Total-Return** version (cash leg earns the risk-free rate; >100% borrows at it) AND report an
  **Excess-Return** variant (subtracts the financing/cash return), because real insurance indices are
  usually ER and the gap is large. Hard anchor from a 2024 SEC supplement (S&P 500 **5%** Daily Risk
  Control, 5yr ending 2024-04-01): **S&P 500 TR +14.74%/yr → DRC-5% TR +5.68% → DRC-5% ER +3.55%.** That
  is the bull-market give-up (target-vol drag) AND the ER drag, stacked — a sanity target for our 5%
  build, and a reminder our headline numbers must state TR-vs-ER and dividends-in/out explicitly. The
  live retail standard is the **10% target at 150% cap** (Lincoln S&P 500 10% DRC participation 150–170%).
- **The key reframe (why this is NOT the rejected overlay):** vol-control was previously tested as a
  *subordinate trim* on S0's regime band and rejected — the band already collapses to the floor in
  2008/2022, leaving the trim no room. **S4 has no regime band underneath; vol-targeting IS the whole
  mechanism**, so that rejection does not apply. See memory `vol-control-borrowables`.
- **Data:** SPY daily adjusted prices (Tiingo, free) + a cash/T-bill series (BIL/SGOV) + the CBOE vol
  family (VIX/VIX3M/VIX9D/VVIX) — **all already on local disk** (`C:\TradingDesk-Local\bt_data`). No
  options, no new pull. **Buildable today.**
- **Benchmark:** buy-and-hold **SPY** (and SPX total-return), NOT 60/40 — this is a single-asset product.
  Judged on Sharpe/Sortino, max drawdown, and equity-curve smoothness vs SPY, across 2008 AND 2022.
- **Status:** BUILT + validated 2026-06-28. Pure strategy at strategies/spx_vol_control.py (SpxVolControl); standalone daily TR/ER runner + 2-D sweep at backtester/s4_vol_control.py; report at backtester/output/s4_vol_control_20260628.md. Validated near-exactly against a published SEC index supplement (S&P 500 5% Daily Risk Control, 5yr->2024-04: our 14.93/5.70/3.75% vs SEC 14.74/5.68/3.55%). Realized vol lands on target; at 10%/1.5x -> CAGR 7.5% vs SPY 10.7% but max drawdown -21% vs SPY -55% (2008 -13% vs -37%); Sharpe/Sortino beat buy-and-hold across the whole surface. Clean-mechanics study (costs/chart/re-entry-lag follow-ups in progress). S0 + paperbot untouched.

## S5 — Financed Convexity Overlay on a Synthetic SPX Core  *(spec; braids S1–S4; `docs/S5_SPEC.md`)*
- **Family:** financed long-convexity overlay. A long-SPX core carrying a PERMANENT, self-financed tail
  hedge. Where S4 dials **DELTA** (cash vs SPY), S5 dials **CONVEXITY** (own long puts always; flip a
  *short* overlay on/off). Same signal brain, different lever. Judged on whether it closes S4's V-bottom gap.
- **Thesis:** never buy protection *after* the spike (that's S4's re-entry lag in disguise). Instead: (1)
  ALWAYS own longer-dated downside puts, bought cheap when calm; (2) finance their theta with calm-day
  0DTE selling; (3) in the crash you ALREADY own the hedge — as the puts balloon near the bottom,
  MONETIZE them and roll proceeds into cheap equity. **The hedge becomes the re-entry fuel.** Long-put
  delta auto-de-risks the core as spot falls (no signal, no timing) — so the core stays CONSTANT.
- **Core:** SPX held SYNTHETICALLY (long ATM call + short ATM put ≈ long index) inside the SPXW/SPX
  cash-settled, European, **Section-1256 (60/40)** complex — no assignment/pin risk, portfolio-margin
  netting (long puts cut margin on the shorts), deepest 0DTE liquidity. Cost: embeds a financing leg (~r)
  + must be rolled.
- **Signal:** reuse S4's `max(fast,slow)` realized-vol estimator (v1) + S1 gamma/term-structure
  (contango/backwardation, dealer-gamma sign) confirmation (v2+). CALM ⇒ HARVEST 0DTE premium (the S2/S3
  engine, flat by the close); ROUGH ⇒ STAND DOWN, let the owned puts work.
- **Knobs:** tail layer (strike/DTE/uncapped-vs-spread), Tier-2 income-gated put-spread sizing, regime
  thresholds + hysteresis, overlay delta/wing/tenor (S3), combo roll cadence, financing rate (TR vs ER).
- **Design rules:** (A) **net convexity MUST stay LONG always** — shorts are FINANCING, never the bet
  (extends S2's "if paired, net tail LONG"); (B) financing can run a DEFICIT in choppy-no-crash tape —
  measure it, don't assume it away; (C) regime whipsaw; (D) intraday execution complexity; (E) the
  synthetic's carry leg is real cost (TR-vs-ER like S4).
- **Two open forks (see spec):** Fork 1 constant-core (RECOMMENDED — let put delta be the variable
  exposure) *conditional on* Fork 2 keeping an **UNCAPPED tail** (RECOMMENDED laddered hybrid: mandatory
  cheap deep-OTM uncapped Tier-1 + optional income-financed put-spread Tier-2). The two recommendations
  are one coupled decision.
- **Feasibility (BSM, rough):** calm-day 0DTE income ~5–9% of notional/yr (16d strangle, 50% haircut,
  40–70% calm days) plausibly covers a layered tail carry of ~0.3–1.0%/yr **in a normal year** — but the
  honest test is the FULL-CYCLE ledger (choppy-no-crash deficit vs crash-year payoff), not the calm-year
  surplus. Needs warehouse for skew-adjusted tail pricing + intraday pull for real 0DTE path P&L.
- **Data:** warehouse EOD SPX/SPXW chains (combo + tail + spread pricing, settlement) — **EOD version
  buildable NOW**; **intraday SPXW 0DTE pull (Phase 1) BLOCKED** exactly like S2/S3 for the realistic
  0DTE overlay + intraday monetization. Reuses S3's cash-settled reserve + S4's TR/ER accounting.
- **Status:** spec + EOD prototype + tail-size sweep + **REAL-SKEW VALIDATION all DONE on EOD (2026-06-28).**
  V-bottom gate PASSED (edge = the passive uncapped tail; active monetization demoted to Phase-2). Tail-size
  sweep re-run on ACTUAL SPXW skew (≈ +0.71 vol-pts/1% OTM): design SURVIVES honest pricing, frontier shape
  identical, **validated default ~0.50 notional / 20–25% OTM** (≈ −0.24% CAGR carry; reserve ~0.7–2.1% NAV,
  non-binding). **DEFENSIVE / tail half is now real-data-validated.** Spec = a **ledger priority-waterfall**
  (Tier1 tail → Tier2 protection → T-bill reserve → surplus→upside calls). **STILL PENDING (the OFFENSIVE /
  harvest half): the harvest engine + Phase-2 active monetization + dynamic-throttle layer, all gated on the
  intraday SPXW pull.** See docs/S5_SPEC.md

## S6 — SPX Cash-Flow 0DTE (Brandon W)  *(refuted)*
- **Family:** intraday 0DTE SPX defined-risk credit-spread income.
- **Data:** SPXW 1-min NBBO warehouse (intraday). No new pull.
- **Status:** **REFUTED** — honest backtest shows NO edge on the documented chassis (all
  36 exit×gamma×VIX cells lose; hold-to-settle worst). Strike-selection variant was the
  only residual and never greenlit. See memory `s6-spx-cashflow-0dte`, `s2s3-intraday-condor-refuted`.

## S7 — SPX 45-DTE Managed Premium-Income Condor  *(`backtester/s7_income_condor.py`; prereg `docs/PREREG_S7_income_condor_2026-07-04.md`)*
- **Family:** monthly-style **defined-risk premium income** — the volatility-risk-premium
  harvester income traders actually run. Lineage S2/S3 iron-condor income; distinct chassis
  (multi-week tenor, held-and-managed, weekly-laddered book). NOT 0DTE (that whole family
  is refuted); own strategy number per Andrew.
- **Thesis (to be PROVEN, not assumed):** implied≈1.43×realized VRP can be harvested net of
  honest half-spread fills across a full cycle (incl. 2018-Q4 / 2020 COVID / 2022 bear) by a
  45-DTE weekly-laddered iron condor, actively managed (50% profit-take + 21-DTE time-stop).
- **Chassis:** symmetric IC (short put/call ≈ target delta, fixed 25-pt long wings),
  weekly-laddered 1 lot/week, cash-settled European (no assignment), managed daily.
- **Data — EOD only, buildable NOW:** warehouse EOD SPX chains 2018-2026 (bid/ask + greeks).
  **KNOWN CAVEAT:** vendor delta/IV are CORRUPT (2021 total, 2020 partial) — pricing off
  clean bid/ask, strikes off a clean BSM re-inversion where the delta column is degenerate.
- **Grid (prereg, plateau-not-peak):** DTE{30,45} × short-delta{10,16} × mgmt{hold/25%/50%}
  × fill{0,0.25,0.50 headline,1.0}. Headline 45/16/50%/f.50.
- **Pass bar:** net-positive across mid→50% fill AND OOS AND as a grid plateau; managed arms
  must beat hold-to-expiry AND the random-exit-matched placebo on TOTAL P&L; crisis-survivable.
- **Status:** **in-progress** — prereg committed; engine + honest backtest + report + tests
  in build. A clean refutation is a valid outcome.
