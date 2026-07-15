# SHORT STRANGLE + REGIME GATE — RESULTS + VERDICT

**Run:** 2026-07-06  |  **Runtime:** 271s  |  pre-registered in `docs/PREREG_strangle_regime_gate_2026-07-06.md` (committed BEFORE this run, hash 0121362).

## VERDICT (lead)

### **REFUTED — the gate beats its placebo on P&L but fails a robustness criterion (alpha CI / sample power / OOS / plateau). Regime timing does not deliver clean, robust calm-regime VRP alpha.**

**The decisive number — does each gate beat the 97.5th percentile of a RANDOM same-duty-cycle placebo on total P&L?**

| gate | on-weeks | total P&L $ | alpha_ann | 95% CI | Sharpe | beta | P&L placebo pctile | alpha placebo pctile | BEATS placebo (>97.5)? |
|---|---|---|---|---|---|---|---|---|---|
| regime (solo) | 162 | 338,350 | -6.40% | [-12.5%,+0.4%] | -0.42 | 0.417 | 100.0 | 3.2 | YES |
| contango (solo) | 283 | 288,532 | -6.98% | [-13.6%,-0.1%] | -0.42 | 0.273 | 99.4 | 0.0 | YES |
| ivr (solo) | 181 | -99,615 | -3.61% | [-10.8%,+5.8%] | -0.07 | 0.194 | 1.6 | 14.4 | NO |
| COMPOSITE (headline) | 56 | 188,490 | -2.55% | [-9.3%,+4.8%] | 0.06 | 0.322 | 100.0 | 29.0 | YES |

Headline config: SPX short strangle, 16-delta, 45 DTE, hold-to-expiry, weekly ladder, f=0.5. Composite gate = frozen S0 regime risk-on AND VIX/VIX3M contango AND IVR>=50. Random-duty-cycle placebo: 500 draws, seed 20260706.

- Composite gate P&L **$188,490** vs random-gate null mean **$26,038** (97.5th pct **$145,889**); gate percentile rank **100.0** on P&L, **29.0** on alpha.
- Composite annualized alpha **-2.55%** (CI [-9.27%, 4.76%]), beta 0.322, Sharpe 0.06.
- **Read the two placebo columns together:** the gate clears the P&L null but sits near the BOTTOM of the ALPHA null (composite alpha pctile 29.0; regime-solo 3.2; contango-solo 0.0). That is the tell: a risk-on/calm gate concentrates entries in higher-drift weeks, so its edge over a RANDOM same-duty gate is BETA (the gate raises beta 0.20→0.32-0.42), not VRP alpha. The gate makes MORE money by being MORE long-the-market, not by harvesting premium better. Every gated alpha is NEGATIVE. The P&L-placebo pass is a beta artifact.

### Five pre-registered pass criteria (composite, deployed/gated role)

1. **Gated-on alpha > 0, CI excl. 0, across mid→0.50 band:** FAIL — band alphas [-0.0234, -0.0245, -0.0255], headline CI [-0.0927,0.0476].
2. **Beats random same-duty placebo (>97.5 pct P&L) — composite AND regime-solo:** PASS — composite pctile 100.0, regime-solo pctile 100.0.
3. **Adequate sample (>= 50 on-weeks):** PASS — composite fires on 56 entries.
4. **OOS positive alpha in BOTH halves:** FAIL — train -1.41% (n=259), test -2.26% (n=413).
5. **Plateau across delta×dte×IVR:** FAIL — 25% of composite grid cells have positive alpha.

## Plateau grid — COMPOSITE gate, hold, f=0.50 — delta × DTE × IVR threshold

| delta | dte | IVR>= | on-weeks | n_trades | total P&L $ | alpha_ann | beta | Sharpe | win% |
|---|---|---|---|---|---|---|---|---|---|
| 0.16 | 30 | 0 | 158 | 158 | 250,787 | -1.85% | 0.153 | -0.03 | 77 |
| 0.16 | 30 | 25 | 101 | 101 | 252,055 | -1.44% | 0.148 | 0.04 | 89 |
| 0.16 | 30 | 50 | 56 | 56 | 163,471 | +0.71% | 0.163 | 0.29 | 93 |
| 0.16 | 30 | 75 | 14 | 14 | 52,706 | +10.35% | 0.208 | 1.93 | 100 |
| 0.16 | 45 | 0 | 158 | 158 | 401,126 | -2.40% | 0.252 | 0.08 | 80 |
| 0.16 | 45 | 25 | 101 | 101 | 330,234 | -3.19% | 0.275 | 0.04 | 89 |
| 0.16 | 45 | 50 | 56 | 56 | 188,490 | -2.55% | 0.322 | 0.06 | 89 |
| 0.16 | 45 | 75 | 14 | 14 | 52,477 | -2.96% | 0.468 | -0.02 | 93 |
| 0.2 | 30 | 0 | 158 | 158 | 253,447 | -2.36% | 0.179 | -0.05 | 70 |
| 0.2 | 30 | 25 | 101 | 101 | 269,969 | -2.47% | 0.174 | -0.07 | 80 |
| 0.2 | 30 | 50 | 56 | 56 | 170,598 | +0.30% | 0.185 | 0.20 | 80 |
| 0.2 | 30 | 75 | 14 | 14 | 55,277 | +10.43% | 0.242 | 1.67 | 86 |
| 0.2 | 45 | 0 | 158 | 158 | 415,838 | -2.48% | 0.287 | 0.11 | 74 |
| 0.2 | 45 | 25 | 101 | 101 | 350,104 | -3.32% | 0.309 | 0.07 | 81 |
| 0.2 | 45 | 50 | 56 | 56 | 208,142 | -2.19% | 0.352 | 0.12 | 80 |
| 0.2 | 45 | 75 | 14 | 14 | 54,240 | -2.22% | 0.509 | 0.05 | 86 |

## Fill-band robustness — COMPOSITE gate headline (delta/dte, hold)

| f | on-weeks | total P&L $ | alpha_ann | 95% CI | beta | Sharpe |
|---|---|---|---|---|---|---|
| 0.0 | 56 | 190,817 | -2.34% | [-9.0%,+5.0%] | 0.318 | 0.08 |
| 0.25 | 56 | 189,653 | -2.45% | [-9.2%,+4.9%] | 0.320 | 0.07 |
| 0.5 | 56 | 188,490 | -2.55% | [-9.3%,+4.8%] | 0.322 | 0.06 |

## Managed(50%/21DTE) secondary — COMPOSITE gate headline

| arm | on-weeks | total P&L $ | alpha_ann | beta | Sharpe |
|---|---|---|---|---|---|
| managed | 56 | 64,270 | -1.44% | 0.319 | 0.17 |
| hold (headline) | 56 | 188,490 | -2.55% | 0.322 | 0.06 |

## OOS split (composite headline) — alpha must be positive in BOTH halves

| half | window | n_days | alpha_ann | beta |
|---|---|---|---|---|
| train | 2018-06→2021-12 | 259 | -1.41% | 0.184 |
| test | 2022-01→2026-07 | 413 | -2.26% | 0.374 |

## Gate duty cycles (on the quoted weekly ladder)

| gate | on-weeks | quoted weeks | duty cycle |
|---|---|---|---|
| regime | 162 | 319 | 0.51 |
| contango | 283 | 319 | 0.89 |
| ivr | 181 | 319 | 0.57 |
| composite | 56 | 319 | 0.18 |

## The gate signals (frozen, reused AS-IS)

- **Regime:** `strategies.parts.regime.market_health_score` + `apply_hysteresis` on S0's bt_data inputs (SPY/RSP/sectors + HYG/IEF credit proxy + VIX + HY-OAS). On = confirmed regime in ['RiskOn', 'RiskOnNarrowing'] (equity-allowance ≥ 0.80 band). ZERO new knobs; the frozen regime engine is untouched.
- **Contango:** VIX / VIX3M < 1.0 using `_vix.parquet` + `_vix3m.parquet` (VIX3M causally ffilled; a day whose VIX3M is unknown reads False = stand down).
- **IVR:** trailing-252-day percentile of the VIX close, ≥ 50 at headline (swept [0, 25, 50, 75]). min_periods=252 ⇒ trailing-only.
- All three read AS-OF the entry day (most-recent value on/before it) ⇒ strictly causal; a future VIX/VIX3M/regime print cannot change a past on/off decision.

## Data window & coverage

- Trading days in window: 2111 (2018-06-01..2026-07-03).
- Weekly ladder entries: 423; genuinely quoted: 319.
- SPX daily-return series: 2030 days (2018-06-04..2026-07-02).
- Strangle chassis, honest fills, clean-delta selection, price-map cache, forward-walk management, uncapped-intrinsic settlement all REUSED from `short_strangle.py` (which reuses `s7_income_condor.py`); no strategy knob tuned.

## Method notes

- Gate applied at ENTRY only, causally (a weekly strangle opens iff the gate is on that week per data through the entry day). Open positions manage to normal exit.
- Random-duty-cycle placebo: 500 draws, each turning on exactly the gate's on-week count at random over the quoted weekly entries (seed 20260706). Null distribution of total P&L + alpha; gate percentile rank reported. A gate that merely trades fewer weeks lands ~median; a real regime edge exceeds the 97.5th pct.
- Regression r_str = alpha + beta·r_spx + e; alpha annualized ×252. 95% CI via stationary block bootstrap (block≈20d, 2000 resamples, seed 20260706).
- Frozen S0 config + regime engine untouched. Warehouse + bt_data read-only.
