# Adaptive All-Weather Core — Complete Strategy Specification

**Purpose of this document:** a fully self-contained, rebuild-grade specification of a
rules-based, multi-engine tactical asset-allocation strategy. It contains every rule,
formula, and parameter needed to re-implement the strategy from scratch with no access
to the original code. Written for ingestion by an AI/LLM or a quant developer.

**Mandate (shapes every rule):** deliver a SMOOTHER ride for a retiree / pre-retiree book
sensitive to sequence-of-returns risk — lower max drawdown, lower downside capture — NOT
to beat the S&P 500. CAGR is a constraint ("don't lag too badly in bulls"), not the
objective. Judge by max drawdown, worst rolling 12-month, downside capture, Calmar.

---

## 0. Conventions & global rules

- **Prices:** daily ADJUSTED CLOSE (adjusted for splits AND dividends = total return). All
  signals and returns use adjusted close.
- **Trading-day unit:** `TRADING_DAYS_PER_MONTH = 21`. "N months" in a lookback = `N*21`
  trading days. `MA_LONG_DAYS = 200`, `MA_SHORT_DAYS = 50`, 10-month MA = `MA_MONTHS*21 = 210`.
- **Causality / NO LOOK-AHEAD (the single most important correctness rule):** every signal
  for date T uses ONLY data on/before T. All windows are trailing. A signal computed on a
  month-end date T executes on the FIRST TRADING DAY AFTER T (T+1). `EXECUTION_LAG_DAYS = 1`.
- **Inception-aware:** an asset that has not begun trading by date T is excluded from all
  calculations at T (its value is NaN; never forward-filled or fabricated). Breadth counts,
  rankings, and baskets use only assets actually trading.
- **Rebalance cadence:** `REBALANCE_FREQUENCY = "monthly"` (signal on the last trading day
  of each month). Also supports "weekly"/"biweekly" (last trading day of each ISO week /
  every other week) and a regime-adaptive cadence (out of scope for default).
- **All engines are computed on the full daily price panel, then SAMPLED at signal dates.**

---

## 1. Universe (tickers and roles)

Tradeable universe (`ALL_TICKERS`). Data source: Tiingo daily adjusted close.

| Role | Tickers |
|---|---|
| Equity core (broad beta) | SPY, VTI, RSP |
| Sectors (11 SPDR) | XLC, XLY, XLP, XLE, XLF, XLV, XLI, XLB, XLRE, XLK, XLU |
| T-bills / cash | SGOV, BIL |
| Short Treasuries | SHY, VGSH |
| Floating-rate Treasuries | USFR, TFLO |
| Intermediate Treasuries | IEF |
| Long Treasuries | TLT |
| Gold | GLDM, IAU |
| TIPS | SCHP, STIP |
| Broad commodities | PDBC, DBC |

Convenience groups:
- `DEFENSIVE_ASSETS` = T-bills + short + floating + intermediate + long = [SGOV,BIL,SHY,VGSH,USFR,TFLO,IEF,TLT]
- `REAL_ASSETS` = gold + TIPS + commodities

**Benchmark-only tickers** (NOT traded by any engine; used only for comparison): **AGG**
(iShares Core US Aggregate Bond) and **HYG** (used as a credit-proxy input, see §11).

**Benchmarks:** SPY; **60/40 = 0.60·SPY + 0.40·AGG** (daily-rebalanced constant mix, total
return); T-bills = BIL.

---

## 2. Engine 1 — Regime Engine (Market Health Score)

Outputs a 0–100 score, maps it to a regime, which sets the EQUITY BAND. Owns the de-risking
decision. Computed on the daily panel; SPY is the representative broad-beta series.

### 2.1 Score = mean of three equal-weight components × 100 (each component ∈ [0,1])

**Component A — Trend** (mean of 4 binary sub-signals on SPY):
1. `above_200d`  = SPY > SMA(SPY, 200)
2. `above_10m`   = SPY > SMA(SPY, 210)        (10-month MA)
3. `ret_6m_pos`  = SPY/SPY[t−126] − 1 > 0      (6-month total return positive)
4. `slope_pos`   = OLS slope of SPY over trailing 200 days > 0
- `trend = mean(sub1..sub4)`. Mask (NaN) the warm-up window before `max(210, 200)` bars exist.

**Component B — Breadth** (mean of 2 sub-signals):
1. `breadth_pct` = (# sectors with sector > SMA(sector,200)) / (# sectors trading that day).
   Inception-aware: a sector with no 200d MA yet is excluded from BOTH numerator and denominator.
2. `rsp_leads`   = (RSP/SPY) > SMA(RSP/SPY, 200)   → 1/0
- `breadth = mean(breadth_pct, rsp_leads)`.

**Component C — Stress** (mean of available sub-signals; "calm" = full marks):
1. `vol_calm`    = if real VIX available: VIX ≤ SMA(VIX,200); else SPY 63-day annualized
   realized volatility ≤ SMA(that realized-vol series, 200). (Realized vol = std(daily ret,63)·√252.)
2. `credit_calm` = if real HY OAS available: OAS ≤ SMA(OAS,200); else (HYG/IEF) > SMA(HYG/IEF,200).
- `stress = mean(available sub-signals)`.

`score = mean(trend, breadth, stress) · 100  ∈ [0,100]`.
Rate/inflation inputs are DELIBERATELY EXCLUDED from this score — they live in Engine 2.

### 2.2 Classification → regime → equity band (`REGIME_BANDS`)

Map score to the highest band whose lower bound it clears:

| Regime | Score range | Equity band (low, high) |
|---|---|---|
| RiskOn | 75–100 | (0.80, 1.00) |
| RiskOnNarrowing | 55–74 | (0.60, 0.80) |
| Caution | 40–54 | (0.35, 0.60) |
| Defensive | 25–39 | (0.10, 0.35) |
| CapitalPreservation | 0–24 | (0.00, 0.15) |

The equity band is later scaled by the client version's equity allowance (§9).

### 2.3 Hysteresis / whipsaw control (applied to the daily score series → "confirmed regime")

Parameters: `REGIME_CONFIRMATION_DAYS = 2`, `REGIME_MIN_THRESHOLD_CROSS = 3`,
`REGIME_IMMEDIATE_DROP_POINTS = 20`.

State: `confirmed` (regime), `ref_score` (score at last confirmation), `pending`, `pending_days`.
For each day with score s, raw = classify(s):
```
if confirmed is None: confirmed = raw; ref_score = s
elif raw == confirmed: pending=None; pending_days=0; ref_score = s   # stay anchored
elif NOT decisive(s, confirmed, raw): pending=None; pending_days=0   # within dead-zone: ignore
else:
    big_drop = (ref_score - s) > REGIME_IMMEDIATE_DROP_POINTS
    if raw is LOWER-health than confirmed and big_drop:              # immediate de-risk
        confirmed = raw; ref_score = s; pending=None; pending_days=0
    else:                                                            # buffer (re-risk always buffered)
        pending_days = pending_days+1 if pending==raw else 1; pending = raw
        if pending_days >= REGIME_CONFIRMATION_DAYS: confirmed = raw; ref_score = s; reset pending
emit confirmed
```
`decisive(s, confirmed, raw)`: regimes ordered high→low health.
- raw LOWER than confirmed (de-risk): `lower_bound[confirmed] − s ≥ REGIME_MIN_THRESHOLD_CROSS`.
- raw HIGHER than confirmed (re-risk): `s − lower_bound[band one step above confirmed] ≥ REGIME_MIN_THRESHOLD_CROSS`.

Net effect: small wiggles across a band edge (<3 pts) are ignored; a >20-pt collapse de-risks
immediately; otherwise a change must persist 2 days; re-risking is never instant (also see §8 ladder).

---

## 3. Engine 2 — Duration / Inflation-Deflation Engine

Owns the KIND of defense (cash/short/intermediate/long Treasuries) and detects the macro regime.
Long Treasuries are NOT default defense — they must earn exposure. Roles: TLT=long, IEF=intermediate,
BIL=T-bill, SPY=stock; inflation-filter reps: gold=IAU, commodity=DBC, floating=USFR.

**Yield input:** real US Treasury 10-year par yield if available; else proxy = −IEF (negated price,
so every yield-trend comparison inverts correctly: falling yield ⇔ rising bond price).

Helpers: `ret(x,m) = x/x[t−m·21] − 1`; `above_ma(x,w) = x > SMA(x,w)`;
`drawdown(x,w) = x / rolling_max(x,w) − 1`. `ma200=200`, `ma10m=210`.
`yld_ma = SMA(yield,200)`, `yld_prev = yield[t−21]`, `yld_rising = yield > yld_prev`.

### 3.1 Long-Treasury PERMISSION (need ≥ `LONG_TSY_PERMISSION_MIN_PASSES = 4` of 5)
1. `above_ma(TLT,200) OR above_ma(TLT,210)`
2. `ret(TLT,3) > 0`
3. `ret(TLT,3) > ret(BIL,3)`
4. yield flat/falling: `(yield < yld_ma) OR (yield ≤ yld_prev)`
5. drawdown ok: `drawdown(TLT,252) ≥ LONG_TSY_MAX_DRAWDOWN (−0.10)`
`perm_passes = count of the 5 true`.

### 3.2 Inflationary-bear filter (the 2022 guard) — true when ≥ ceil(n/2) of these hold
- `spy_weak = NOT above_ma(SPY,200)`
- `yield_up_rising = (yield > yld_ma) AND yld_rising`
- `tlt_weak = NOT above_ma(TLT,200)`
- `tbill_beats_dur = ret(BIL,3) > ret(IEF,3) AND ret(BIL,3) > ret(TLT,3)`
- `reals_beat_long`: among present reps {IAU, DBC, BIL, USFR}, count(`ret(rep,3) > ret(TLT,3)`) ≥ half.

### 3.3 Deflationary-panic filter — true when ≥ ceil(n/2) of these hold
- `spy_weak = NOT above_ma(SPY,200)`
- `yield_falling = yield < yld_prev`
- `dur_beats_tbill = ret(IEF,3) > ret(BIL,3) OR ret(TLT,3) > ret(BIL,3)`
- `credit_widening` = if real OAS: OAS > SMA(OAS,200); else (HYG/IEF) < SMA(HYG/IEF,200).
- `commodities_weak = NOT above_ma(DBC,200)`

### 3.4 Long-Treasury BAN (ANY one bans long duration)
- `NOT above_ma(TLT,200) AND NOT above_ma(TLT,210)`  (broken trend)
- `yield_up_rising`
- `ret(BIL,3) > ret(TLT,3) OR ret(BIL,6) > ret(TLT,6)`  (T-bills beat long over 3/6m)
- `NOT above_ma(SPY,200) AND NOT above_ma(TLT,200)`  (stocks & bonds both down)
- `inflationary_bear`
- `drawdown(TLT,252) < −0.10`

`long_allowed = (perm_passes ≥ 4) AND NOT long_banned`.

### 3.5 Macro regime label (drives the dynamic real-asset cap, §7)
Tilt only on an UNAMBIGUOUS regime (one filter on, the other off); else neutral.
- `infl = inflationary_bear AND NOT deflationary_panic`
- `defl = deflationary_panic AND NOT inflationary_bear`
- `sustained = rolling_mean(inflationary_bear, STAGFLATION_LOOKBACK_DAYS=126) ≥ STAGFLATION_PERSISTENCE=0.70`
- `macro_regime = "deflation" if defl; "stagflation" if (infl AND sustained); "inflation" if infl; else "neutral"`

### 3.6 Duration decision (per signal date + regime) → per-bucket caps (% of TOTAL portfolio)
Base caps from `DURATION_CAPS[bucket][regime]` (table below). Then override:
- if `NOT long_allowed`: `caps.long = (0, 0)`.
- if `inflationary_bear`: `caps.long = (0,0)`; `caps.intermediate.hi = min(hi, INFLATIONARY_INTERMEDIATE_CAP=0.15)`.
- unknown/Undefined regime → safe default: long(0,0), intermediate(0,0), short(0,1), tbill(0.5,1).

`DURATION_CAPS` (low, high) by bucket × regime:
```
            RiskOn      Narrowing   Caution     Defensive   CapPres
long        0–10%       0–10%       0–15%       0–25%       0–25%
intermediate0–10%       0–10%       0–25%       0–40%       0–40%
short       0–20%       0–20%       0–50%       0–100%      0–100%
tbill       0–20%       0–20%       20–70%      50–100%     50–100%
```
(tbill bucket = T-bills + floating-rate cash-likes; see §10.)

---

## 4. Engine 3 — Defensive Engine (cross-sectional ranking)

Ranks defensive candidates 0–100 to decide WHICH defensive assets fill the defense sleeve.
T-bills (BIL) are always eligible and the fallback. No forced diversification.

Candidates = `DEFENSIVE_ASSETS` present + BIL. For each, build six "higher-is-better" factors
(penalties negated), each turned into a cross-sectional PERCENTILE across that day's candidates:
- `return_3m`  = pct_change(63)                          weight 25
- `return_6m`  = pct_change(126)                         weight 20
- `abs_trend`  = price / SMA(price,200) − 1              weight 20
- `rel_vs_tbill` = return_3m − return_3m(BIL)            weight 15
- `volatility_penalty` = −(63d annualized realized vol)  weight 10
- `drawdown_penalty`   = price / rolling_max(price,252) − 1  weight 10
(`DEFENSIVE_SCORE_WEIGHTS`, sum = 100.)

`score = Σ weight_i · percentile_i(date)  ∈ [0,100]`. Pre-inception cells are NaN.
`rank_defensives(asof)` = that day's scores, NaN dropped, sorted descending.

---

## 5. Engine 4 — Volatility Multiplier (subordinate trim within the band)

Picks WHERE inside the regime equity band the allocation sits. Cannot de-risk below the band floor.
- `realized_vol = std(SPY daily returns, VOL_LOOKBACK_DAYS=63) · √252`.
- Buckets `VOL_BUCKET_MULTIPLIERS = (1.00, 0.85, 0.70)`, vs the version's target-vol range (lo,hi):
  `vol ≤ lo → 1.00 ; lo < vol ≤ hi → 0.85 ; vol > hi → 0.70`. NaN vol → 1.00.
- `equity_target(band, vol, version) = max(band_low, multiplier · band_high)`.

`TARGET_VOL_BY_VERSION` (annualized): Conservative (0.08,0.10), Balanced (0.10,0.12), Growth (0.12,0.15).

---

## 6. Engine 5 — Sector Engine (optional equity tilt; DEFAULT OFF)

`SECTOR_TILT_PCT = 0.0` (default off → equity sleeve = broad beta only). Returns equity-sleeve
weights (sum to 1):
- Broad-beta core = equal-weight `EQUITY_CORE` members above their 200d MA (SPY fallback if none).
- If `tilt > 0` (clamped 0–0.30): eligible sectors = those above `SECTOR_TREND_GATE_DAYS=200` MA;
  score = mean(RS_3m, RS_6m) where `RS_k = ret(sector,k) − ret(SPY,k)`, lookbacks `(3,6)`; pick top
  `SECTOR_COUNT_WHEN_USED[1]=4`; each sector weight = `min(tilt/k, SECTOR_MAX_WEIGHT=0.15)`; core gets
  `(1−tilt)`; renormalize. If no sector passes the gate → revert to broad beta.

---

## 7. Engine 6 — Real-Asset Sleeve (diversified, trend-gated, regime-scaled)

Its OWN third sleeve (equity / real assets / defense), NOT part of the defense budget. A basket of
GOLD + BROAD COMMODITIES (TIPS excluded — behaves like Treasuries → defense).

### 7.1 Basket selection (`select_real_basket`, per signal date)
`REAL_ASSET_BASKET = {"gold": ["GLDM","IAU"], "commodities": ["PDBC"]}`.
For each category, pick the best ETF that passes the trend+momentum gate:
- gate: `above_ma(x,200) AND ret(x,3) > 0 AND ret(x,6) > 0`
- among gated ETFs in the category, choose max momentum `score = (ret(x,3)+ret(x,6))/2`
- compute trailing vol `vol = std(daily ret, REAL_ASSET_VOL_LOOKBACK=252)·√252`
Present legs are **inverse-volatility weighted**: `weight_i = (1/vol_i) / Σ(1/vol_j)`.
If no category has a gated ETF → basket is empty (None). (Gold & commodities are ~uncorrelated, so the
basket vol < either leg alone.)

### 7.2 Sleeve sizing (in portfolio assembly, §9)
- Base target = `REAL_ASSET_SLEEVE_TARGET[version]` (Conservative 0.10, Balanced 0.15, Growth 0.20),
  floored at `REAL_ASSET_STRATEGIC_FLOOR = 0.0` (pure tactical).
- **Dynamic regime scale** `REAL_ASSET_REGIME_SCALE[macro_regime]`: deflation 0.75, neutral 1.0,
  inflation 2.0, stagflation 3.0.
- `sleeve_target = min(base · scale [+ rotated, §9], REAL_ASSET_SLEEVE_MAX = 0.45, remaining budget)`.
- Allocate `sleeve_target` across legs by inverse-vol weight, each leg capped by its §12 category cap
  (gold 25%, commodities 20%). The trend gate (§7.1) means a non-trending real asset is never held,
  regardless of the regime scale — the scale only raises the CEILING, the gate decides what fills it.

---

## 8. Re-entry Ladder (staged equity rebuild after a defensive period)

Caps the equity target during recovery so a sharp bounce can't whip the book straight back to full risk.
`REENTRY_STAGES` → equity ceiling: stage 0 → 0%, 1 → 25%, 2 → 50%, 3 → 75%, 4 → 100%.

Per-month conditions (booleans):
- `stage1 = (SPY > SMA(SPY,50)) AND breadth_improving AND vol_not_rising`
- `stage2 = (SPY>200d OR SPY>10m) OR (score > 40)`
- `stage3 = (#sectors above 200d ≥ REENTRY_STAGE3_SECTOR_COUNT=6) OR breadth_materially_improving`
- `stage4 = (score > REENTRY_STAGE4_SCORE[version]) AND credit_calm AND vol_calm`
- `defensive = confirmed_regime ∈ {Defensive, CapitalPreservation}`
- `deteriorating = (NOT credit_calm) OR (NOT vol_not_rising)`
- `sharp_recovery = (score > REENTRY_STAGE4_SCORE[version]) AND (SPY>200d OR SPY>10m)`
where `breadth_improving = breadth_pct > prev_month`, `breadth_materially_improving = breadth_pct >
prev_month + REENTRY_BREADTH_IMPROVE (0.05)`, `vol_not_rising = realized_vol ≤ prev_month`.
`REENTRY_STAGE4_SCORE`: Conservative 75, Balanced 65, Growth 55.

State machine (start stage = 4; `target = highest stage whose condition is met`):
```
if defensive:        stage = min(stage, target)          # fast de-risk; never raises stage
elif target > stage: stage += 1                          # rebuild ONE stage per month
elif target < stage and deteriorating: stage -= 1        # rollback one stage on deterioration
# MAX-LAG override: if stage<4 for >= REENTRY_MAX_LAG_MONTHS (6) periods AND sharp_recovery: stage = 4
clamp stage to [0,4]
```
`ladder_cap = REENTRY_STAGES[stage].equity_pct` (0 at stage 0). De-risk is fast; re-risk is staged.

> **Status note (2026-06-28, no config change).** Tightening re-entry by setting `REENTRY_MAX_LAG_MONTHS
> = 3` was TESTED and **HELD — NOT adopted**: it failed a per-episode safety gate (a risk-budget
> trade-off, not a free win — full detail in `VALIDATION.md` §4.4). `REENTRY_MAX_LAG_MONTHS` stays **6**.
> The OPEN lead is to make the `sharp_recovery` override fire only on **clean V-recoveries** (the
> residual re-entry whipsaw is the override firing in sideways grinds) — HIGH curve-fit risk, needs a
> principled trigger + OOS re-test before any change. Nothing in this section's parameters has changed.

---

## 9. Portfolio Assembly (order of operations per rebalance)

Inputs at signal date T: `regime` (confirmed, §2.3), `equity_target` (= `min(equity_target(scaled_band,
realized_vol, version), ladder_cap)` ), `equity_sleeve` (§6), `duration_decision` (§3.6, incl.
`macro_regime`), `defensive_ranking` (§4), `real_basket` (§7.1), `version`, `prev_weights`.
`scaled_band = regime band (§2.2) × CLIENT_VERSIONS[version].equity_allowance`.

```
macro = duration_decision.macro_regime
# --- (experimental, default OFF) risk-budget rotation ---
rotated = 0
if EQUITY_ROTATION_ENABLED and real_basket is not None:
    rotated = REAL_ASSET_EQUITY_ROTATION[macro] * equity_target   # inflation 0.25, stagflation 0.50
equity_effective = equity_target - rotated

# 1. Equity sleeve
for ticker, frac in equity_sleeve: weights[ticker] += frac * equity_effective
remaining = 1 - equity_effective

# 2. T-bill floor (regime + version): take FIRST
tbill_floor = min(remaining, max(caps.tbill.low, CLIENT_VERSIONS[version].tbill_floor))
weights[best_tbill] += tbill_floor ; remaining -= tbill_floor          # best_tbill = top-ranked cash-like
# best_tbill: highest defensive-ranked ticker in {SGOV,BIL,USFR,TFLO}, else BIL

# 3. Real-asset sleeve (§7.2)
if real_basket and remaining > 0:
    scale = REAL_ASSET_REGIME_SCALE[macro]
    base  = max(REAL_ASSET_STRATEGIC_FLOOR, REAL_ASSET_SLEEVE_TARGET[version])
    sleeve_target = min(base*scale + rotated, REAL_ASSET_SLEEVE_MAX, remaining)
    for leg in real_basket.legs:
        alloc = min(sleeve_target * leg.weight, leg.category_cap)       # gold 0.25 / commod 0.20
        weights[leg.ticker] += alloc ; remaining -= alloc

# 4. Whipsaw: incumbents (prev_weights>0) get +RANK_REPLACEMENT_THRESHOLD (10) added to their score
ranking = defensive_ranking ; ranking[held] += 10 ; sort descending

# 5. Fill the rest of the defense sleeve by rank, honoring duration bucket caps (% of TOTAL)
for ticker in ranking:
    if remaining <= 0: break
    bucket = bucket_of(ticker)                                          # tbill/short/intermediate/long
    room = caps[bucket].high - bucket_used[bucket]
    take = min(remaining, room) ; weights[ticker] += take ; remaining -= take ; bucket_used[bucket]+=take

# 6. Residual (all caps exhausted) -> cash; then normalize weights to sum 1
weights[best_tbill] += remaining ; weights /= sum(weights)
```
Result: three sleeves — equity / real assets / defense (cash+Treasuries) — summing to 1.

Bucket mapping (`bucket_of`): tbill = {SGOV,BIL,USFR,TFLO}; short = {SHY,VGSH}; intermediate = {IEF};
long = {TLT}.

Portfolio-level §12 caps (hard ceilings): sector 15%, gold 25%, commodities 20%, TIPS 20%, long
Treasury 25%, intermediate 40%.

---

## 10. Backtest mechanics

1. **Precompute (once, causally, on the full daily panel):** Market Health Score series → confirmed
   regime (hysteresis); duration signals (incl. macro_regime); defensive scores; realized vol.
2. **Signal dates:** last trading day of each month (per `REBALANCE_FREQUENCY`).
3. **Re-entry ladder:** build monthly conditions (§8) → ladder stage → ladder equity cap per month.
4. **Per signal date T:** regime = confirmed[T]; eq_target = min(vol-trim(scaled_band, rv[T], version),
   ladder_cap[T]); gather sector sleeve, duration decision, defensive ranking, real basket; build target
   weights (§9). **Execute at T+1** (first trading day after T).
5. **Daily simulation:** hold the most recently executed target weights; portfolio daily return =
   Σ wᵢ·rᵢ; weights drift with returns between rebalances. At each rebalance, charge a cost =
   `one_way_turnover · PER_TRADE_COST_BPS/10000` (`PER_TRADE_COST_BPS = 3.0`; one-way turnover =
   ½·Σ|w_new − w_drifted|). Optional `TAXABLE_MODE`: skip trading an asset whose target differs from
   its drifted weight by < `TURNOVER_BAND = 0.02` (no-trade band), then renormalize.
6. **Benchmarks** over the same window: SPY; 60/40 (daily-rebalanced 0.6·SPY+0.4·AGG); T-bills (BIL).

---

## 11. Macro data inputs & proxies (labeled real-vs-proxy)

- **10-year yield** (Engine 2): real US Treasury daily par yield if downloaded; else proxy = −IEF price.
- **Volatility** (Engine 1 stress): real CBOE VIX if downloaded; else SPY 63-day realized vol proxy.
- **Credit** (Engine 1 stress + Engine 2 deflation): **HYG/IEF ratio trend proxy** (high-yield vs
  intermediate Treasury — the falling ratio captures credit stress *and* the flight-to-quality that
  accompanies it). The denominator is configurable via `config.CREDIT_PROXY`. The real ICE BofA HY
  OAS is NOT freely available with usable history (FRED restricted ICE indices to a rolling 3-year
  window in April 2026; full history is commercial via ICE/Bloomberg/Refinitiv). A "purer" HYG/LQD
  proxy was tested and is WORSE for this purpose (the deflation filter wants the rate/flight-to-quality
  component HYG/LQD cancels).

Each macro series is reindexed to the trading calendar and forward-filled; all comparisons are trailing.

---

## 12. Client versions (the risk dial)

`ACTIVE_VERSION` ∈ {Conservative, Balanced, Growth}. Differences:

| Param | Conservative | Balanced | Growth |
|---|---|---|---|
| `equity_allowance` (× regime band) | 0.80 | 1.00 | 1.00 |
| `tbill_floor` (min cash) | 0.10 | 0.05 | 0.00 |
| target vol range | 8–10% | 10–12% | 12–15% |
| real-asset sleeve target | 10% | 15% | 20% |
| stage-4 re-entry score gate | 75 | 65 | 55 |

---

## 13. Metrics (report yardstick — risk first)

Computed from NAV series; risk-free = T-bill daily return (else 0).
- **Return:** CAGR = (NAV_end/NAV_start)^(365.25/days) − 1; annual volatility = std(daily)·√252.
- **Risk:** Max drawdown = min(NAV/cummax − 1); worst rolling 3m/12m/3y = min(NAV/NAV[t−63/252/756] − 1);
  downside deviation = √(mean(min(r−rf,0)²))·√252.
- **Risk-adjusted:** Sharpe = mean(r−rf)·252 / (std(r)·√252); Sortino = mean(r−rf)·252 / downside_dev;
  Calmar = CAGR / |max drawdown|.
- **Behavior vs SPY:** beta = cov(r, r_SPY)/var(r_SPY); up/down capture = ratio of geometric-MEAN MONTHLY
  returns on months SPY was up / down; longest underperformance = longest run (in months) the strategy's
  relative NAV (strategy/SPY) stays below its prior peak.
Compare strategy vs SPY, 60/40, T-bills on all of the above.

---

## 14. Complete parameter table (current values)

```
# Timing
EXECUTION_LAG_DAYS = 1 ; REBALANCE_FREQUENCY = "monthly" ; TRADING_DAYS_PER_MONTH = 21
# Regime engine
MA_SHORT_DAYS=50 ; MA_LONG_DAYS=200 ; MA_MONTHS=10 ; TREND_RETURN_MONTHS=6 ; SLOPE_LOOKBACK_DAYS=200
REGIME_BANDS: RiskOn 75-100/(.80,1.00); Narrowing 55-74/(.60,.80); Caution 40-54/(.35,.60);
              Defensive 25-39/(.10,.35); CapPres 0-24/(.00,.15)
REGIME_CONFIRMATION_DAYS=2 ; REGIME_MIN_THRESHOLD_CROSS=3 ; REGIME_IMMEDIATE_DROP_POINTS=20
# Duration engine
LONG_TSY_PERMISSION_MIN_PASSES=4 ; LONG_TSY_RETURN_MONTHS=3 ; LONG_TSY_VS_TBILL_MONTHS=3
LONG_TSY_MAX_DRAWDOWN=-0.10 ; LONG_TSY_DRAWDOWN_LOOKBACK_DAYS=252 ; TBILL_VS_TSY_LOOKBACKS_MONTHS=(3,6)
INFLATIONARY_INTERMEDIATE_CAP=0.15 ; DURATION_CAPS = (see §3.6 table)
STAGFLATION_LOOKBACK_DAYS=126 ; STAGFLATION_PERSISTENCE=0.70
# Defensive engine
DEFENSIVE_SCORE_WEIGHTS: return_3m 25, return_6m 20, abs_trend 20, rel_vs_tbill 15,
                         volatility_penalty 10, drawdown_penalty 10  (sum 100)
# Volatility multiplier
VOL_LOOKBACK_DAYS=63 ; VOL_BUCKET_MULTIPLIERS=(1.00,0.85,0.70)
TARGET_VOL_BY_VERSION: Conservative (.08,.10), Balanced (.10,.12), Growth (.12,.15)
# Sector engine (off by default)
SECTOR_TILT_PCT=0.0 ; SECTOR_RS_LOOKBACKS_MONTHS=(3,6) ; SECTOR_TREND_GATE_DAYS=200
SECTOR_MAX_WEIGHT=0.15 ; SECTOR_COUNT_WHEN_USED=(3,4)
# Real-asset sleeve
REAL_ASSET_SLEEVE_TARGET: Conservative .10, Balanced .15, Growth .20 ; REAL_ASSET_STRATEGIC_FLOOR=0.0
REAL_ASSET_BASKET={"gold":["GLDM","IAU"],"commodities":["PDBC"]} ; REAL_ASSET_VOL_LOOKBACK=252
REAL_ASSET_REGIME_SCALE: deflation 0.75, neutral 1.0, inflation 2.0, stagflation 3.0
REAL_ASSET_SLEEVE_MAX=0.45
EQUITY_ROTATION_ENABLED=False ; REAL_ASSET_EQUITY_ROTATION: inflation 0.25, stagflation 0.50  (experimental)
# Re-entry ladder
REENTRY_STAGES: 1→.25, 2→.50, 3→.75, 4→1.00 ; REENTRY_STAGE3_SECTOR_COUNT=6 ; REENTRY_MAX_LAG_MONTHS=6
REENTRY_STAGE4_SCORE: Conservative 75, Balanced 65, Growth 55 ; REENTRY_BREADTH_IMPROVE=0.05
# Client versions
CLIENT_VERSIONS: Conservative {equity_allowance .80, tbill_floor .10};
                 Balanced {1.00, .05}; Growth {1.00, .00} ; ACTIVE_VERSION="Balanced"
# Portfolio caps / frictions / whipsaw
CAP_MAX_SECTOR=.15 ; CAP_MAX_GOLD=.25 ; CAP_MAX_COMMODITIES=.20 ; CAP_MAX_TIPS=.20
CAP_MAX_LONG_TREASURY=.25 ; CAP_MAX_INTERMEDIATE=.40
PER_TRADE_COST_BPS=3.0 ; TAXABLE_MODE=False ; TURNOVER_BAND=0.02 ; RANK_REPLACEMENT_THRESHOLD=10
# Benchmarks
BENCHMARK_6040=("SPY","AGG") weights (0.60,0.40) ; BENCHMARK_TBILL="BIL"
```

---

## 15. Implementation order (to rebuild from scratch)

1. Data layer: load adjusted-close panel (inception-aware), macro series (yield/VIX/credit).
2. Engines (each independently testable, all causal): Regime → Duration → Defensive → Volatility →
   Sector → Real-asset basket → Re-entry ladder.
3. Portfolio assembly (§9). 4. Backtest loop (§10, T+1, costs). 5. Metrics (§13). 6. Report.
Verify NO LOOK-AHEAD: recomputing any signal on data truncated at T must reproduce its value at T;
and T+1 execution must yield different results than same-day execution.
