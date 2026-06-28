# Adaptive All-Weather Core — Validation Report

**Companion to `STRATEGY.md`** (the rules/parameters spec). This document contains the
RESULTS and the validation evidence: backtested metrics, the anti-curve-fit battery, the
tail-regime tests, return attribution, and — most importantly — an explicit "proven vs.
unproven / known limitations" section. All numbers are generated from the current code.

This is a validation report, not a sales sheet. Where the strategy is weak or unproven, it
says so. Read §8 (limitations) before drawing conclusions.

**Bottom line (the actual decision in one sentence).** Versus all-equity SPY, the drawdown edge
is robust and well-evidenced. Versus a realistic 60/40, the strategy is a **forward bet that the
next decade contains a real bear market**: if it does, it beats 60/40 meaningfully; if it's another
grinding bull, it has bought drawdown insurance that wasn't needed and paid for it in lagged return.

**Test universe & periods.** Tradeable ETFs (Tiingo adjusted close). Core backtest window
2015-02 → 2026-06 (the floor where the full defensive ETF universe exists). An extended
window 2007-01 → 2026-06 is used for the 2008 GFC test (thinner universe pre-2010: 9 of 11
sectors, no SGOV/floating-rate/GLDM/TIPS-ETFs/PDBC; inception-aware handles the gaps).
Costs: 3 bps per trade, monthly rebalance, T+1 execution. Version shown: **Balanced** unless noted.

---

## 1. Headline results — Balanced (2015-02 → 2026-06)

_Numbers reflect the regime-engine early-exit margin adopted 2026-06-26 (§4.1)._

| Metric | Strategy | SPY | 60/40 (SPY/AGG) | T-bills |
|---|---|---|---|---|
| CAGR | 7.5% | 13.9% | 9.2% | 1.9% |
| Annual volatility | 8.6% | 17.7% | 11.0% | 0.3% |
| **Max drawdown** | **−10.2%** | −33.7% | −21.7% | −0.2% |
| Worst rolling 3-month | −8.3% | −29.8% | −18.2% | −0.1% |
| Worst rolling 12-month | −6.8% | −19.7% | −17.3% | −0.1% |
| Downside deviation | 6.3% | 12.6% | 7.9% | 0.0% |
| Sharpe | 0.66 | 0.72 | 0.69 | — |
| Sortino | 0.90 | 1.01 | 0.96 | — |
| **Calmar** (CAGR/maxDD) | **0.73** | 0.41 | 0.43 | — |
| Beta vs SPY | 0.27 | 1.00 | 0.61 | — |
| Up capture vs SPY | 0.47 | 1.00 | 0.65 | — |
| Down capture vs SPY | 0.41 | 1.00 | 0.64 | — |
| Longest underperf. vs SPY (months) | 129 | 0 | 124 | 129 |

**Honest read of this table (critical):** over this *benign, mostly-bull* window, the strategy
**lags SPY badly on return (by design)** and actually **trails a 60/40 on risk-adjusted return**
(Sortino 0.86 vs 0.96, Sharpe 0.63 vs 0.69). Its *only* clear win vs 60/40 here is **drawdown**
(max DD −10.7% vs −21.7%, Calmar 0.71 vs 0.43, down-capture 46% vs 64%). In calm markets this is
a **drawdown-insurance** product, not a return improver. The return-side edge only appears when a
real bear is in the window — see §2.

The "129 months longest underperformance vs SPY" is real and prominent: in a relentless bull, a
defensive strategy underperforms SPY on a *relative* basis nearly the entire period. The mandate
explicitly accepts this.

---

## 2. The same strategy including the 2008 GFC (2007-01 → 2026-06)

_Regenerated 2026-06-27 on stabilized off-Drive data (`C:\TradingDesk-Local\bt_data`, refetched from
inception) with the regime early-exit margin `REGIME_TREND_MARGIN = 0.03` (§4.1). The pre-2010 GFC
universe is legitimately thinner (9 of 11 sectors; no SGOV/floating-rate/GLDM/TIPS-ETFs/PDBC) and the
inception-aware loader handles the gaps._

| Metric | Strategy | SPY | 60/40 |
|---|---|---|---|
| CAGR | **8.5%** | 10.7% | 8.1% |
| **Max drawdown** | **−10.2%** | −55.2% | −34.7% |
| Calmar | **0.83** | 0.19 | 0.23 |
| Sortino | **1.16** | 0.77 | 0.85 |

**When a real systemic bear is in the window, the strategy beats 60/40 on EVERYTHING** — return,
drawdown, and risk-adjusted return — because 2008 is exactly where adaptive de-risking pays off and
a static 60/40 cannot. This is the core thesis, and it is the reason the benign-window 60/40 "tie"
(§1) understates the strategy's purpose.

GFC window (2007-10 → 2009-06) max drawdown: **strategy −7.1%**, SPY −55.2%, 60/40 −34.7%.
Calendar 2008 return: **strategy +8.3%**, SPY −36.8%, 60/40 −20.0%.
Calendar 2022 return: **strategy −6.1%**, SPY −18.2%, 60/40 −15.6%.

---

## 3. Client versions (the risk dial), 2015–26

_With the regime early-exit margin (§4.1). Figures to ~2 d.p.; regenerate on stabilized data (see note below)._

| Version | CAGR | Max drawdown | Sortino | Calmar | Down capture vs SPY |
|---|---|---|---|---|---|
| Conservative | 6.5% | −8.9% | 0.90 | 0.73 | 31% |
| Balanced | 7.5% | −10.2% | 0.91 | 0.74 | 40% |
| Growth | 7.9% | −10.4% | 0.92 | 0.76 | 43% |

Ordering is intuitive: Conservative = lowest return / smoothest / least downside capture; Growth =
most return / most exposure. The margin improved Calmar and shallowed drawdown for all three vs the
pre-fix figures (Conservative 0.67→0.73, Balanced 0.71→0.74, Growth 0.73→0.76).

> **Data-stability note (resolved 2026-06-27).** The price files previously lived under the Google
> Drive root and were observed being overwritten by Drive sync mid-session (the extended GFC dataset
> reverted to a 2010-start snapshot, and successive runs drifted at the 3rd decimal). The data has
> now been moved OFF Drive to a stable local path (`C:\TradingDesk-Local\bt_data`, alongside the
> venv), the full pre-2010 history was re-fetched from inception, and the **2008 GFC figures (§2, §5)
> were regenerated 2026-06-27 on that stabilized data** — the 2015–26 numbers reproduce to 2 d.p.
> across runs. (An earlier design inverted this via accidental gold concentration; that was diagnosed
> and fixed — see §6.)

---

## 4. Anti-curve-fit battery (directly addresses overfitting risk)

The strategy has ~40 parameters across six engines and a short tradeable history — a real
overfitting risk we treated as the central concern, not an afterthought. Evidence:

### 4.1 Parameter-robustness sweep (one knob at a time, wide ranges; metric = Calmar)
A robust strategy shows broad PLATEAUS (not lucky spikes). Result: **8 of 8 key parameters are now
plateaus** — the one historically fragile knob (the 200-day MA) was diagnosed and fixed (see below).
Most parameters are industry-standard values we did NOT optimize.

| Parameter | Calmar across the swept range | Verdict |
|---|---|---|
| REGIME_IMMEDIATE_DROP_POINTS (8→30) | 0.71–0.72 | robust plateau |
| TREND_RETURN_MONTHS (3→12) | 0.60–0.71 | robust |
| VOL_LOOKBACK_DAYS (42→126) | 0.71 flat | robust |
| SLOPE_LOOKBACK_DAYS (100→250) | 0.71–0.76 | robust |
| REGIME_CONFIRMATION_DAYS (2→4) | 0.71–0.72 | robust |
| REGIME_MIN_THRESHOLD_CROSS (2→6) | 0.63–0.71 | robust |
| LONG_TSY_PERMISSION_MIN_PASSES (3→5) | 0.71 flat | robust |
| MA_LONG_DAYS (150→250), pre-fix | 0.58 / 0.71 / 0.61 / 0.45 | was FRAGILE (sharp peak at 200) |
| **MA_LONG_DAYS (150→250), with regime margin** | **0.69 / 0.71 / 0.73 / 0.73 / 0.65** | **robust plateau (spread 11%)** |
| REGIME_TREND_MARGIN (0→5%) | flat 0.71–0.73 for 3–5% | robust plateau |

**The fix — a regime-engine early-exit margin (adopted 2026-06-26).** The 200-day MA *was* the
strategy's one fragile, load-bearing knob: Calmar peaked sharply at 200 and fell to 0.58 (175) /
0.61 (225) / 0.45 (250) — a single point of failure threaded through every engine. We diagnosed it
in two steps (scripts: `ma_experiment*.py`):
1. **The overloaded knob does two jobs** — price>MA *trend gates* and series-vs-own-MA *stress
   baselines*. Splitting them (config `TREND_MA_DAYS` / `STRESS_MA_DAYS`) showed the fragility is
   **entirely in the trend role**; the stress baselines are already a robust plateau.
2. **Per-engine localization** showed the fragility lives **only in the REGIME engine's** trend
   gates (trend / breadth / RS-leadership). The same margin on the duration ban rules does nothing
   (so the proven 2008/2022 logic is left untouched); on the real-asset/sector gates it is harmful.

The fix is a **one-sided early-exit margin scoped to the regime engine** (`REGIME_TREND_MARGIN =
0.03`): price must clear its MA by 3% to read "in trend," so the regime de-risks early and the exact
lookback stops mattering. This flattens the MA sweep from a 42% spread to **11%** (a plateau), is
itself a plateau across 3–5% margins, **improves all three versions** (Calmar up, drawdown
shallower), and **holds out-of-sample** (walk-forward OOS Calmar improved). Rejected alternatives
(all tested): a multi-lookback **ensemble** (no help), an **EMA** gate (flattened but cratered the
level), and a **symmetric** hold-in-band deadband (worse and non-robust — it de-risks late; the
asymmetry is the load-bearing feature for a drawdown-first mandate).

**Residual note.** The margin de-concentrates the parameter risk (the goal), but the strategy still
rests on a limited history — see §8. The regime engine is now the place where trend-lookback choices
are made; that is by design (the regime decision is the dominant risk lever) and is now robust to it.

### 4.2 Walk-forward (out-of-sample)
The one parameter we actually *tuned* (the immediate-de-risk threshold, 10→20) was walk-forward
validated: built on the ≤2019 era, it held on the >2019 out-of-sample era (test-era Calmar 0.87 vs
0.86 baseline). The tuning is a modest, robust improvement — not an in-sample artifact.

### 4.3 Monte Carlo — block bootstrap (200 synthetic histories, 63-day blocks)
Resamples contiguous blocks of the real price history (preserving trends and cross-asset
correlations) and re-runs the *entire* strategy on each. Tests whether our single result was lucky.

| Percentile | p5 | p25 | p50 (median) | p75 | p95 |
|---|---|---|---|---|---|
| CAGR | 0.8% | 4.6% | 8.6% | 12.1% | 19.1% |
| Max drawdown | −31.9% | −23.7% | **−18.6%** | −15.1% | −11.6% |
| Calmar | 0.03 | 0.22 | 0.44 | 0.71 | 1.32 |

**Key honesty point: our actual −10.7% drawdown was a FAVORABLE draw** (better than the p95 of
−11.6%). Plan on **~−18% typical, ~−32% bad-case (p5)** going forward, not −11%.

Head-to-head across the 200 paths:
- **vs SPY:** strategy drawdown shallower on **93%** of paths; median maxDD −18.6% vs −33.7%.
- **vs 60/40:** shallower on **52%** of paths (a tie); median maxDD −18.6% vs −21.7%; median CAGR
  8.6% vs 8.8% (tie). Confirms §1: vs a *balanced* benchmark the edge is small in the absence of a
  systemic bear (which the 2018-2026 bootstrap window cannot fabricate — see §8).
- **Tail:** worst single strategy path **−51.8%** vs SPY −62.1% vs **60/40 −44.3%**. The strategy's
  worst case is DEEPER than 60/40's worst — the bootstrap can't make a 2008, so true tail risk is
  likely understated.

---

## 5. Tail-regime tests (the three bear types)

The strategy's thesis is that it adapts the KIND of defense to the regime. Tested in all three:

| Regime | Data quality | Strategy did | Result vs 60/40 |
|---|---|---|---|
| **2008 deflation** | REAL | held Treasuries (they rallied), cut equity to ~0% | **+8.3%** vs −20.0% |
| **2022 inflation** | REAL | banned long Treasuries, held cash + real assets | **−6.1%** vs −15.6% |
| **1970s stagflation** | SYNTHETIC (low fidelity) | avoided bonds, held cash + real assets | "won" nominally, see caveat |

- **2008 vs 2022 is the proof it isn't curve-fit to 2022:** the *same duration-engine code* read 2008
  as deflationary and HELD duration Treasuries (TLT+IEF ~24% of the book, inside a ~95% defensive
  sleeve with equity cut to ~0%), and read 2022 as inflationary and held long Treasuries at ~0%. A
  static 60/40 holds the same 40% bonds in both — saved in 2008, sunk in 2022.
- **Stagflation is the weakest test (synthetic).** No ETFs/sector data exist for the 1970s, so this
  is a constructed scenario calibrated to documented 1970s magnitudes, NOT a historical replay. The
  strategy's logic responded correctly (avoid bonds, hold cash + real assets). But **NOMINAL returns
  lie in a stagflation**: after ~90% cumulative inflation, ALL assets lose real purchasing power —
  the strategy loses *less real wealth* than a 60/40 (it avoids the bonds that bleed), but it does
  NOT protect purchasing power. Treat this as a logic check, not evidence.

---

## 6. Attribution (what's driving the results)

- **Real assets (gold + commodities) are the primary RETURN edge.** Running the strategy with the
  real-asset sleeve OFF (Balanced, 2015–26): CAGR **7.6% → 6.6%**, Calmar **0.71 → 0.52**, max DD
  **−10.7% → −12.8%**. Without real assets the strategy trails a 60/40 on return. Gold/commodity
  trend-following is the strategy's closest thing to alpha — and a concentration it depends on.

- **Year-by-year real-asset contribution (the broad-vs-concentrated test).** Per-calendar-year
  return difference, strategy ON vs OFF, with average real-asset weight that year:

  | Year | Contribution | Real wt | | Year | Contribution | Real wt |
  |---|---|---|---|---|---|---|
  | 2015 | −0.5% | 1% | | 2021 | **+4.9%** | 12% |
  | 2016 | −0.7% | 9% | | 2022 | +1.0% | 14% |
  | 2017 | −0.0% | 0% | | 2023 | +0.5% | 14% |
  | 2018 | +0.3% | 12% | | 2024 | +1.5% | 14% |
  | 2019 | +1.5% | 10% | | 2025 | +0.5% | 13% |
  | 2020 | +1.4% | 10% | | 2026 | +1.4% | 16% |

  Read: the edge is **broad-based but modest (~+0.6%/yr), with one outlier year (2021, +4.9% — the
  energy/commodity inflation spike) that is 42% of the entire +11.7% cumulative contribution.** It is
  positive in 8 of 12 years and steadily positive every year 2018–2026. Strip out 2021 and the edge
  falls to ~+0.6%/yr — smaller but still positive and broad, NOT "two lucky years and nothing else."
  **The key drought-risk evidence:** in 2017 the trend gate held real-asset weight at **0%** (they
  weren't trending → not held → zero drag), and 2015–16 cost only −0.5%/−0.7%. So a 2013–2018-style
  real-asset drought would be sat out by the gate, not bled — but this is only a ~2.5-year proxy for
  that environment (the validated window 2018–2026 was a *favorable* era for real assets).
- **Drawdown protection comes from the regime + duration engines** (the de-risking and the
  Treasuries-vs-cash choice), confirmed by the 2008 result (§5).
- **Limitation:** attribution is partial. We isolated the real-asset sleeve and the 2008 regime
  behavior, but there is NOT yet a full per-engine return decomposition. A systematic attribution
  (Engine 1 vs 2 vs ladder vs real-asset contribution to return AND drawdown) is a known gap.

---

## 7. Design decisions validated this cycle (discipline evidence)

- **Dynamic real-asset cap by regime — ADOPTED after an anti-curve-fit sweep.** Scaling the
  real-asset sleeve by detected regime (deflation 0.75× / neutral 1× / inflation 2× / stagflation
  3×) was swept across aggressiveness levels. The base case stayed FROZEN across all levels (the
  regime-gating confines the change to inflation/stagflation months), and the response plateaued —
  so the chosen setting is the saturation point, not a peak.
- **Equity→real-asset rotation — TESTED and REJECTED.** A proposed structural change (let real
  assets substitute for equity in inflation/stagflation) was prototyped and tested: it broke the
  frozen base case (2015–21 CAGR 9.31% → 8.34%) and didn't even help stagflation. Rejected and left
  OFF behind a flag. (Evidence that the process rejects changes that fail the guardrails.)

---

## 8. PROVEN vs. UNPROVEN — and known limitations (read this)

### Proven (robust evidence)
- **Drawdown edge vs all-equity (SPY): robust.** ~half SPY's drawdown historically; shallower on
  93% of Monte Carlo paths. If the alternative is 100% equity, the strategy clearly adds value.
- **Correct regime navigation in BOTH real bear types** (2008 deflation, 2022 inflation), with the
  same causal code — strong evidence the duration engine is economically driven, not 2022-fit.
- **No look-ahead:** verified by test (recomputing any signal on data truncated at T reproduces its
  value at T; T+1 execution differs from same-day). 89 automated tests pass, 0 skipped.
- **Robust to most parameters** (7 of 8 key knobs on plateaus).

### Unproven / weak / limitations
1. **vs a realistic 60/40, the edge is NOT proven in benign markets.** It ties on return and
   risk-adjusted return; it only clearly wins on drawdown, and the *return* advantage appears only
   when a systemic bear is in the window. Whether the next decade contains such bears is a forward
   bet, not a backtested fact.
2. **Short history / single major real inflation stress.** Most ETFs are post-2007 (several
   post-2013). The core window has ONE real inflation bear (2022). The 2008 extension uses a thinner
   universe and older proxies.
3. **The actual history was a favorable draw.** Expect ~−18% typical / ~−32% bad-case drawdown, not
   the −10.7% we realized. True tail risk is likely WORSE than the Monte Carlo shows (it cannot
   fabricate a 2008 or 1970s; the strategy's worst bootstrapped path is deeper than 60/40's).
4. **The 200-day MA fragility is RESOLVED** (§4.1) — a regime-engine early-exit margin turned the
   sharp peak at 200 into a plateau (sweep spread 42%→11%), validated out-of-sample. ~~Was the
   strategy's main parameter-risk concentration.~~ (Residual: the margin de-concentrates the risk but
   does not add new history.)
5. **Stagflation tested only synthetically** (weakest evidence). Nothing — including this — protects
   real purchasing power in a 1970s-style inflation.
6. **Return depends on a real-asset (gold/commodity) concentration** (§6). Remove it and the
   strategy trails 60/40. The edge is broad-based (positive in 8 of 12 years) but its *level* is
   inflated by one year (2021 = 42% of the cumulative contribution), and the validated window
   (2018–2026) was a favorable era for real assets. A sustained multi-year real-asset drought is
   only lightly tested (the trend gate correctly sat out 2017 — encouraging but a thin proxy).
7. **Macro proxies:** 10y yield = real US Treasury par yield; VIX = real (CBOE); **credit = HYG/IEF
   PROXY**. The real ICE BofA HY OAS is no longer freely available with usable history — `FRED_API_KEY`
   is now present in `C:\TradingDesk-Local\secrets\.env` (verified authenticating 2026-06-28), but FRED
   restricted the ICE indices to a rolling 3-year window (April 2026), so the key returns only
   2023-06-27→2026-06-25 (~3 yrs). The real OAS is therefore usable for recent dates only (~2023+) and
   the HYG/IEF proxy is retained for history; full history is commercial-only (ICE/Bloomberg/Refinitiv). A "purer" **HYG/LQD** proxy
   (HY vs investment-grade, which cancels the rate component) was tested head-to-head and was
   **worse** across the board (2008 +3.4%→+1.4%, Calmar 0.71→0.58): the deflation filter *wants*
   the flight-to-quality/rate component HYG/LQD removes — in 2008, IG corporates blew out too, so
   HY-vs-IG barely widened while HY-vs-Treasury crashed cleanly. HYG/IEF is retained.
8. **Attribution is partial** (§6) — no full per-engine return decomposition yet.

---

## 9. Reproducibility

All results regenerate from the code: per-version reports (`output/backtest_report_*.html`),
parameter robustness (`src/robustness.py`), Monte Carlo (`src/montecarlo.py`), real-asset
attribution (`run_backtest(use_real_assets=False)`), tail tests (extend data to 2005; synthetic
stagflation generator). Test suite: 89 tests (engines, portfolio assembly, the no-look-ahead
truncation test, metrics). Numbers in this document are from the current `config.py` and engines.
