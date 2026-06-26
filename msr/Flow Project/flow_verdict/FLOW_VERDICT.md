# Flow Signal — Technical Findings & Keep/Drop Verdict

**Status:** complete. **Verdict: DROP the newsletter's proprietary flow signal for directional use.**
The only function with demonstrable value (de-risking into drawdowns) is reproducible for free
from price + realized vol and does **not** require the subscription.

**Scope of this package:** evidence and reproducible code to *put the flow question to bed*. The
actual decision on which gates/signals/levels to wire into the backtester is made over in the
backtester project — Section 7 lays out the candidates and knobs for that work; it does not
prescribe the final book.

- **Analyst:** Claude (Opus 4.8) session, 2026-06-25.
- **Data inputs:** vendor `_msr_flow_research.csv` (281 daily rows, 2025-05-01 → 2026-06-18);
  Tiingo SPY adjClose snapshot `data/spy_hist_2008_2026.csv` (4,648 rows, 2008-01-02 → 2026-06-24).
- **Reproduce everything:** `py reproduce_findings.py` → console + `results/findings_output.txt`.
  No API token needed (uses the cached SPY snapshot). All numbers below are copied from that run.

---

## 1. The question

The newsletter sells three **Tier-C (non-reproducible)** regime labels — `regime_flow_risk`
∈{Bullish,Neutral,Bearish}, `regime_strategic`, `regime_pvband_rr`. A first-pass scan suggested
`regime_flow_risk` separated forward SPX returns and added value beyond the reproducible gamma
signal. The job: decide **keep vs drop**, after breaking the three confounds that made the first
pass only a hypothesis — (a) one bull regime (+~33% drift), (b) thin episodes (38 flow runs),
(c) no bear market in the labelled window.

## 2. Data

| Input | Rows | Span | Notes |
|---|---|---|---|
| `_msr_flow_research.csv` (vendor) | 281 | 2025-05-01 → 2026-06-18 | labels + `spx_gamma_state`, `spx_above_flip`, `gex_throttle`, precomputed `fwd_ret_{1,5,10,20}d` |
| `data/spy_hist_2008_2026.csv` (Tiingo) | 4,648 | 2008-01-02 → 2026-06-24 | daily `adjClose` (split+dividend adjusted) used for returns; covers GFC, 2011, 2015-16, 2018-Q4, 2020 COVID, 2022 bear |

**Data-quality note (vendor):** `gex_throttle` contains three corrupt values vs its normal −37…+26
range — `669.0` (2025-07-16), `66.0` (2025-11-20), `620.0` (2025-12-17). Treated as errors; they do
not affect the flow analysis (throttle is not an input here) but the backtester should winsorize/clip
`gex_throttle` if it is ever used as a feature.

## 3. Methodology (the discipline that makes this credible)

Three deliberate choices, because naive analysis of this data is badly misleading:

1. **Drift removal.** The whole era drifts up, so "Bullish → positive" is partly just drift. Two
   defenses: (i) **demean** each forward return against the sample mean at that horizon (so we ask
   "beats the *average* day", not "beats zero"); (ii) the **Bullish−Bearish spread**, which is
   drift-neutral by construction (both labels are drawn from the same drifting tape).

2. **Overlapping-return autocorrelation → circular-shift permutation test.** Consecutive days share
   almost all of an h-day forward window, so the *effective* sample is far smaller than the row count
   and ordinary t-stats are wildly overstated. We test a label↔return association by **circularly
   shifting the label series against the returns**: this preserves the autocorrelation of both series
   and the run-length structure of the labels, and breaks only their alignment. `perm_p` = fraction
   of shifts whose |spread| ≥ |observed|. (Implementation: `analytics.circular_shift_pvalue`.)

3. **Risk-adjusted overlay as the decisive test.** Sharpe and max-drawdown are drift-normalized, so a
   long/flat or sized overlay driven by the signal isolates *risk* value from the bull drift.
   (Implementation: `analytics.strategy_perf`, acting on **next-day** returns — no look-ahead.)

**The flow proxy (Section 4)** lets us test the *mechanism* the vendor claims to capture across 18
years, since the vendor label itself only exists for the 281-row window.

## 4. The flow proxy (free, reproducible — `flow_proxy.py`)

The vendor's "Systematic Flow Risk" claims to estimate vol-control / CTA / risk-parity positioning.
Those mechanisms are reconstructable from price alone:

- **vol-control / risk-parity** target constant vol ⇒ exposure ∝ 1/realized-vol; they de-risk when
  vol spikes → captured by `volpct` = causal trailing-252d percentile rank of 21d realized vol.
- **CTA / trend** are long in uptrends, flat/short in downtrends → captured by price vs 200d MA and
  12m-minus-1m momentum.

**Classification rule** (Bearish evaluated first; de-risk dominates):

```
Bearish : px < MA200            OR  rvol_rank > 0.80          (downtrend or vol spike)
Bullish : px > MA200 AND mom>0  AND rvol_rank < 0.70          (uptrend AND calm)
Neutral : otherwise
```

Default knobs (all sweepable in the backtester): `ma_len=200`, momentum = `px[t-21]/px[t-252]−1`,
`rvol_win=21`, `vol_rank_win=252`, `vol_top=0.80`, `vol_calm=0.70`. Every feature is point-in-time.

## 5. Results

### 5A. Vendor 281-row set (in-sample, one bull regime)

**Drift baseline (all days):** mean forward return +0.11% (1d) / +0.53% (5d) / +1.02% (10d) /
**+2.04% (20d)**, with 78% of all 20-day windows positive. This is the confound.

**By `regime_flow_risk`, 20-day (raw | demeaned | hit%):**

| state | raw | demeaned | hit% | n |
|---|---|---|---|---|
| Bullish | +3.11% | **+1.07%** | 95.8% | 71 |
| Neutral | +2.23% | +0.19% | 76.9% | 78 |
| Bearish | +1.23% | **−0.81%** | 67.9% | 112 |

The first-pass separation reproduces and *survives demeaning in sign*. **But it is not significant.**
Bullish−Bearish spread with circular-shift perm p:

| horizon | spread | perm_p |
|---|---|---|
| 1d | +0.16% | 0.132 |
| 5d | +0.61% | 0.274 |
| 10d | +0.98% | 0.352 |
| 20d | +1.88% | 0.291 |

Why so weak when hit rates look clean? The 71 "Bullish" days are only **10 episodes**; within a run
the overlapping 20-day returns are nearly identical, so the 95.8% hit is ~10 clustered bets that
mostly landed in up-drift. Episode-level confirms (independent entry-day 20d return): Bullish +2.60%
(n=10), Neutral +1.68% (n=16), Bearish +0.64% (n=9) — directionally consistent, zero statistical
power.

**Incremental-to-gamma (2-way, demeaned 20d).** Within every gamma state Bullish>Bearish, and the
best-populated cell (Positive gamma: Bullish n=55 +0.78 vs Bearish n=41 −1.19) is suggestive — but
the cleanest incremental piece, *Bearish-while-Positive-gamma* underperforming (−1.74% demeaned 20d),
only reaches perm_p≈0.15 — suggestive, sub-threshold. Also note **redundancy**:
55 of 71 Bullish-flow days sit in Positive gamma, so the label is partly collinear with the
(reproducible) gamma signal.

**The in-sample stress test it failed.** The window now contains a real correction (Feb–Apr 2026,
SPX 6978→6344 ≈ −9%). Through it, `regime_flow_risk` flip-flopped
Bearish→Neutral→Bearish→**Bullish (mid-selloff, 2026-02-19 @ 6881)**→Neutral→Bearish, and printed
merely **Neutral at the April bottom**. It did not cleanly lead or avoid the drawdown — the central
"does Bearish protect?" claim failed its one real in-sample test.

**Side signals.** `regime_pvband_rr` "Long" is a 1-day dip-bounce (Long−rest +0.45%, perm_p=0.039 at
1d) that evaporates by 10d (−0.03%, p=0.952) — mean reversion, not a durable edge. `regime_strategic`
has only 7 episodes — untestable.

### 5B. Multi-regime proxy test (SPY 2009-2026, 4,376 days, 5+ real bears)

**By proxy state, 20-day (raw | demeaned | hit%):**

| state | raw | demeaned | hit% | n |
|---|---|---|---|---|
| Bullish | +1.03% | **−0.24%** | 69.1% | 2,698 |
| Neutral | +1.28% | +0.01% | 75.6% | 546 |
| Bearish | +1.85% | **+0.59%** | 67.1% | 1,112 |

**The directional sign REVERSES out-of-sample.** Bull−Bear spread is now *negative*:

| sample | 20d spread | perm_p |
|---|---|---|
| Vendor labels (281 rows, 5A) | **+1.88%** | 0.291 |
| Proxy, full 2009-2026 | **−0.83%** | 0.132 |
| Proxy, excluding vendor era | **−0.68%** | 0.214 |

Neither is significant, **and the sign flips.** Mechanistically obvious: the de-risked / high-vol /
below-trend state clusters at *bottoms*, so its forward returns are *high* (rebound ahead). As a
return predictor the flow mechanism is contrarian, not trend-confirming — the opposite of the vendor
label's in-sample behaviour. The 281-row "Bullish→more upside" was the bull-drift confound.

**Decisive risk-adjusted overlay (drift-normalized), 2009-2026:**

| strategy | CAGR | vol | Sharpe | **max DD** | time in mkt |
|---|---|---|---|---|---|
| Buy & Hold | 15.4% | 17.7% | 0.90 | **−33.7%** | 100% |
| Flat when proxy=Bearish | 10.0% | 10.8% | 0.94 | **−20.5%** | 74% |
| Sized 1 / 0.5 / 0 | 8.8% | 9.9% | 0.91 | **−14.5%** | 68% |

De-risking on the signal cuts max drawdown by **39% (flat) to 57% (sized)** with **equal-or-better
Sharpe**. *This* is the real, robust, multi-regime value of a flow/positioning signal — and it
matches what the MSR methodology spec said all along: a **volatility/regime gate, not a directional
engine**. Crucially it is produced from **Tiingo price + realized vol only (Tier A/B, free)**.

**Bear-market timing (honest limits).** P(proxy=Bearish | next-20d < −10%) = **47.5%** — it flags
about half of severe declines in advance. It misses *fast-from-calm* crashes: the worst forward
windows (Feb-2020 COVID) were preceded by Bullish/Neutral because vol had not yet spiked and price was
at highs. No trend/vol signal catches those — a limitation the vendor's model almost certainly shares.

### 5C. Validity check — does the proxy actually proxy the vendor? (the key caveat)

On the 281-day overlap, proxy vs `regime_flow_risk` agree only **38.1%** of the time (n=189
non-neutral; worse than a 2-class coin flip). The vendor printed "Bearish" on 120 days; the proxy
called 104 of those "Bullish" — and the tape kept rising through most of them.

```
confusion (rows = vendor flow_risk, cols = proxy)
                 Bearish  Bullish  Neutral
   Bearish          16      104        0
   Bullish          13       56        2
   Neutral          15       69        6
```

**Implication:** the proxy is *not* a faithful replica of the vendor's black box, so a null/reversed
result on the proxy cannot, by itself, prove the vendor's *specific* model has zero directional alpha
in an unseen bear market. It does two things that are enough for the decision: (1) shows the
reproducible mechanism delivers the *valuable* (risk-gating) part for free, and (2) shows that during
the one comparable window the proprietary label was the *noisier* of the two.

## 6. Verdict — DROP (for directional use)

Three findings, in order of weight:

1. **Directional edge does not survive and reverses sign** out-of-sample (5B). The vendor's
   own 281-row directional result is sub-significant (perm_p 0.29) and **failed its one in-sample
   stress test** (5A). The reason to pay for it does not hold up.
2. **The valuable function — drawdown protection — is real but free** (5B overlay; −39% to −57% max
   DD at equal-or-better Sharpe across GFC/2018/2020/2022), built from price + realized vol with no
   subscription.
3. **The vendor label is not even a good version of that free gate** (5C; 38% agreement, noisier).

**The single caveat, stated plainly:** because the proxy only matches the vendor 38% of the time, we
cannot *directly* prove the vendor's exact label is directionally worthless in a real bear market — we
only have their bull-era labels. The one test that would settle it directly is **obtaining historical
`regime_flow_risk` labels spanning 2018/2020/2022**. Short of that, the economics already decide it:
do not pay for an unproven directional signal whose one demonstrable use-case is reproducible for
free and which it does not even track well.

## 7. Hand-off to the backtester — candidate gates, levels, and knobs

This is input for the backtester decision, **not** a prescribed final book.

**Carry forward (free, reproducible):**
- **Risk gate G1 (long/flat):** exposure = 0 when `proxy == Bearish`, else 1. → ~−39% max DD, Sharpe
  ≥ buy&hold. Defined entirely by `px<MA200 OR rvol_rank>0.80`.
- **Sizing overlay G2:** exposure = 1.0 / 0.5 / 0.0 for Bullish / Neutral / Bearish. → ~−57% max DD;
  larger return give-up. Choose G1 vs G2 by the book's return-vs-drawdown preference.

**Do NOT carry forward:**
- The vendor **directional** flow call (unproven, reverses out-of-sample).
- `regime_pvband_rr` as a multi-day signal (1-day bounce only).
- `regime_strategic` (untestable, 7 episodes).

**Knobs to sweep when you test levels over there:**
- Trend filter `ma_len` ∈ {50, 100, 200}; momentum window; replace SMA with EMA.
- Vol-spike threshold `vol_top` ∈ {0.75, 0.80, 0.85, 0.90} and the calm gate `vol_calm`.
- `vol_rank_win` ∈ {126, 252} (how much history defines "high vol").
- **Interaction with the gamma gate** (from the MSR spec: deep drawdowns were 100% in negative
  gamma): test G1 ∧ gamma-gate vs each alone — they may be partly redundant (5A redundancy note) or
  complementary at the tails.
- **Asymmetry / hysteresis:** entering vs exiting the de-risk state on different thresholds to cut
  whipsaw (the flat strategy turns over on every vol-rank cross of 0.80).
- Apply on the actual instrument(s) of the momentum book, not just SPY; re-fit nothing on the vendor
  era.

**Reality checks before trusting any swept result:** transaction costs / turnover on the gate; the
fast-from-calm crash blind spot (G1 will not catch a COVID-style gap); SPY adjClose is total-return
(swap to the book's instrument); thresholds here were sensible defaults, lightly informed by the full
sample — re-validate any tuned level on a holdout.

## 8. Files in this package

| File | Purpose |
|---|---|
| `FLOW_VERDICT.md` | this memo |
| `flow_proxy.py` | the reusable proxy (features + classification) — the drop-in for the backtester |
| `analytics.py` | drift-robust helpers: circular-shift perm test, strategy_perf, by-state tables, episodes |
| `reproduce_findings.py` | regenerates every number above → `results/findings_output.txt` |
| `pull_data.py` | optional: refresh the SPY snapshot from Tiingo (reads token from `.env` at runtime; never stores it) |
| `data/spy_hist_2008_2026.csv` | Tiingo SPY adjClose snapshot (the cached input; no token needed to reproduce) |
| `results/findings_output.txt` | full saved console transcript of the reproduction run |

**Provenance:** SPY history pulled from Tiingo on 2026-06-25. Token lives only in
`C:\Users\andre\backtester\.env` (outside Drive, by design) and is not stored anywhere in this
package. Python 3.14, pandas 3.0, numpy 2.4 (scipy/statsmodels not required — resampling tests only).
