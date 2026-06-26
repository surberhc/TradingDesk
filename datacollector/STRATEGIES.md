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
