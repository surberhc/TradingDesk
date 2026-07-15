# SHORT STRANGLE — VRP ALPHA (delta-neutral) — RESULTS + VERDICT

**Run:** 2026-07-06  |  **Runtime:** 273s  |  pre-registered in `docs/PREREG_short_strangle_alpha_2026-07-06.md` (committed BEFORE this run).

## VERDICT (lead)

### **REFUTED — the VRP premium is EATEN: alpha <= 0 / CI spans 0 net of honest fills and uncapped crash losses. Mechanical SPX premium-selling shows no clean VRP alpha (condor + CSP + strangle all refuted).**

Headline: SPX short strangle, 16-delta, 45 DTE, managed (50%-target or 21-DTE), weekly ladder, f=0.5.

- **Annualized alpha intercept: -0.83%** (bootstrap 95% CI [-5.85%, +4.89%], SPANS 0), **beta 0.197**, R² 0.155, alpha t-stat -0.24.
- Delta-neutrality check: book behaves like **0.197×** SPX daily return (|beta|<0.15 => NOT neutral); avg net entry delta +0.000.
- Daily-return **Sharpe 0.16, Sortino 0.16**, maxDD -0.238, ann.ret +1.57%, ann.vol 10.13% (vs cash rf~3%).
- Total book P&L (managed): **$25,918**; hold-to-expiry $156,658; random-exit placebo mean $75,831 (95% [-37,915, 185,007]).

### Six pre-registered pass criteria

1. **Positive alpha, CI excl. 0, across mid->0.50 band:** FAIL — band alphas [-0.0069, -0.0077, -0.0083], headline CI [-5.85%,+4.89%].
2. **Genuinely delta-neutral (|beta|<0.15):** FAIL — beta 0.197.
3. **Beats cash risk-adjusted (Sharpe & Sortino > 0):** PASS — Sharpe 0.16, Sortino 0.16.
4. **OOS positive alpha in BOTH halves:** FAIL — train -2.72% (n=624), test +0.48% (n=1126).
5. **Plateau across delta x dte x fill:** FAIL — 0% of managed delta×dte×fill cells have positive alpha.
6. **Crisis survivability + mgmt beats hold+placebo:** FAIL — full-cycle alpha -0.83%; managed $25,918 <= hold $156,658; <= placebo 97.5% $185,007.

## Beta regression grid (alpha annualized) — delta × DTE × management × fill

| delta | dte | mgmt | f | n_days | total P&L $ | net entry Δ | alpha_ann | 95% CI | beta | R² | t(alpha) | Sharpe | Sortino | maxDD | win% |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 0.16 | 30 | managed | 0.0 | 1750 | 56,912 | 0.000 | -0.37% | [-5.1%,+5.0%] | 0.158 | 0.105 | -0.10 | 0.16 | 0.17 | -0.203 | 67 |
| 0.16 | 30 | managed | 0.25 | 1750 | 41,495 | 0.000 | -0.37% | [-5.1%,+5.1%] | 0.159 | 0.105 | -0.10 | 0.16 | 0.17 | -0.205 | 65 |
| 0.16 | 30 | managed | 0.5 | 1750 | 25,841 | 0.000 | -0.35% | [-5.1%,+5.2%] | 0.160 | 0.104 | -0.10 | 0.16 | 0.17 | -0.207 | 63 |
| 0.16 | 30 | managed | 1.0 | 1750 | -4,720 | 0.000 | -0.33% | n/a | 0.161 | 0.104 | -0.09 | 0.16 | 0.18 | -0.210 | 61 |
| 0.16 | 30 | hold | 0.0 | 1750 | -34,572 | 0.000 | +2.82% | [-6.6%,+16.8%] | 0.128 | 0.015 | 0.35 | 0.21 | 0.38 | -0.243 | 73 |
| 0.16 | 30 | hold | 0.25 | 1750 | -41,870 | 0.000 | +2.74% | [-6.7%,+16.7%] | 0.129 | 0.015 | 0.34 | 0.20 | 0.38 | -0.244 | 73 |
| 0.16 | 30 | hold | 0.5 | 1750 | -49,167 | 0.000 | +2.65% | [-6.8%,+16.7%] | 0.129 | 0.015 | 0.33 | 0.20 | 0.37 | -0.245 | 73 |
| 0.16 | 30 | hold | 1.0 | 1750 | -63,762 | 0.000 | +2.48% | n/a | 0.131 | 0.016 | 0.31 | 0.19 | 0.36 | -0.247 | 73 |
| 0.16 | 45 | managed | 0.0 | 1750 | 58,627 | 0.000 | -0.69% | [-5.6%,+5.0%] | 0.195 | 0.155 | -0.20 | 0.17 | 0.17 | -0.234 | 70 |
| 0.16 | 45 | managed | 0.25 | 1750 | 42,632 | 0.000 | -0.77% | [-5.7%,+4.9%] | 0.196 | 0.155 | -0.22 | 0.16 | 0.17 | -0.236 | 69 |
| 0.16 | 45 | managed | 0.5 | 1750 | 25,918 | 0.000 | -0.83% | [-5.8%,+4.9%] | 0.197 | 0.155 | -0.24 | 0.16 | 0.16 | -0.238 | 69 |
| 0.16 | 45 | managed | 1.0 | 1750 | -4,180 | 0.000 | -0.98% | n/a | 0.198 | 0.155 | -0.28 | 0.14 | 0.15 | -0.243 | 68 |
| 0.16 | 45 | hold | 0.0 | 1750 | 171,606 | 0.000 | -1.89% | [-7.9%,+5.4%] | 0.198 | 0.100 | -0.41 | 0.04 | 0.05 | -0.259 | 74 |
| 0.16 | 45 | hold | 0.25 | 1750 | 164,132 | 0.000 | -2.00% | [-8.0%,+5.3%] | 0.199 | 0.100 | -0.44 | 0.03 | 0.04 | -0.261 | 74 |
| 0.16 | 45 | hold | 0.5 | 1750 | 156,658 | 0.000 | -2.12% | [-8.2%,+5.2%] | 0.200 | 0.101 | -0.46 | 0.03 | 0.03 | -0.264 | 74 |
| 0.16 | 45 | hold | 1.0 | 1750 | 141,711 | 0.000 | -2.34% | n/a | 0.202 | 0.102 | -0.51 | 0.01 | 0.01 | -0.268 | 74 |
| 0.2 | 30 | managed | 0.0 | 1750 | 50,255 | 0.001 | -0.40% | [-5.6%,+5.6%] | 0.172 | 0.104 | -0.10 | 0.16 | 0.17 | -0.219 | 65 |
| 0.2 | 30 | managed | 0.25 | 1750 | 33,799 | 0.001 | -0.31% | [-5.6%,+5.7%] | 0.173 | 0.104 | -0.08 | 0.17 | 0.18 | -0.221 | 65 |
| 0.2 | 30 | managed | 0.5 | 1750 | 13,480 | 0.001 | -0.30% | [-5.6%,+5.8%] | 0.174 | 0.104 | -0.08 | 0.17 | 0.19 | -0.223 | 62 |
| 0.2 | 30 | managed | 1.0 | 1750 | -23,620 | 0.001 | -0.26% | n/a | 0.176 | 0.103 | -0.06 | 0.17 | 0.19 | -0.226 | 60 |
| 0.2 | 30 | hold | 0.0 | 1750 | -93,237 | 0.001 | +3.17% | [-6.9%,+17.7%] | 0.143 | 0.017 | 0.38 | 0.22 | 0.39 | -0.253 | 68 |
| 0.2 | 30 | hold | 0.25 | 1750 | -101,903 | 0.001 | +3.08% | [-7.0%,+17.6%] | 0.144 | 0.017 | 0.37 | 0.22 | 0.38 | -0.254 | 68 |
| 0.2 | 30 | hold | 0.5 | 1750 | -110,570 | 0.001 | +2.98% | [-7.1%,+17.5%] | 0.145 | 0.017 | 0.35 | 0.21 | 0.38 | -0.255 | 68 |
| 0.2 | 30 | hold | 1.0 | 1750 | -127,902 | 0.001 | +2.79% | n/a | 0.147 | 0.018 | 0.33 | 0.20 | 0.37 | -0.257 | 68 |
| 0.2 | 45 | managed | 0.0 | 1750 | 32,660 | 0.000 | -0.86% | [-6.3%,+5.3%] | 0.212 | 0.152 | -0.22 | 0.16 | 0.17 | -0.252 | 68 |
| 0.2 | 45 | managed | 0.25 | 1750 | 17,838 | 0.000 | -0.91% | [-6.4%,+5.2%] | 0.213 | 0.152 | -0.23 | 0.15 | 0.16 | -0.255 | 67 |
| 0.2 | 45 | managed | 0.5 | 1750 | -4,740 | 0.000 | -0.98% | [-6.5%,+5.2%] | 0.214 | 0.152 | -0.25 | 0.15 | 0.16 | -0.257 | 67 |
| 0.2 | 45 | managed | 1.0 | 1750 | -40,135 | 0.000 | -1.17% | n/a | 0.216 | 0.152 | -0.30 | 0.13 | 0.14 | -0.261 | 66 |
| 0.2 | 45 | hold | 0.0 | 1750 | 110,011 | 0.000 | -2.22% | [-8.9%,+5.5%] | 0.215 | 0.099 | -0.45 | 0.03 | 0.04 | -0.273 | 70 |
| 0.2 | 45 | hold | 0.25 | 1750 | 101,568 | 0.000 | -2.35% | [-9.0%,+5.4%] | 0.216 | 0.100 | -0.47 | 0.02 | 0.03 | -0.274 | 70 |
| 0.2 | 45 | hold | 0.5 | 1750 | 93,126 | 0.000 | -2.49% | [-9.2%,+5.3%] | 0.217 | 0.101 | -0.50 | 0.01 | 0.01 | -0.276 | 70 |
| 0.2 | 45 | hold | 1.0 | 1750 | 76,241 | 0.000 | -2.75% | n/a | 0.219 | 0.102 | -0.55 | -0.00 | -0.01 | -0.279 | 69 |

## Cash / risk-free benchmark (headline book, daily returns on reserved capital)

| arm | Sharpe | Sortino | maxDD | ann. return | ann. vol | total return |
|---|---|---|---|---|---|---|
| Strangle 16d 45DTE managed f0.5 | 0.16 | 0.16 | -0.238 | +1.57% | 10.13% | +7.64% |
| Cash / T-bill (~3% rf) | n/a | n/a | 0.000 | +3.00% | 0.00% | — |

_Because the strangle is ~delta-neutral, CASH (not a delta-matched SPX arm) is the relevant benchmark: does it beat the risk-free rate on a risk-adjusted basis at all, net of honest two-leg fills and uncapped crash losses? Sharpe/Sortino use rf=0._

## Management vs hold vs random-exit placebo (headline delta/dte/fill, TOTAL P&L)

| arm | total book P&L $ |
|---|---|
| Managed (50%-target or 21-DTE) | 25,918 |
| Hold-to-expiry (control) | 156,658 |
| Random-exit placebo (mean of 300 seeds) | 75,831 [95% -37,915, 185,007] |

_Placebo draws a random holding period matching the managed arm's realized holding-period distribution and exits the HOLD-arm trades there. Management earns credit only if it beats BOTH hold and the placebo's 97.5% percentile — i.e. the timing is skill, not luck._

## OOS split (headline) — alpha must be positive in BOTH halves

| half | window | n_days | alpha_ann | beta |
|---|---|---|---|---|
| train | 2018-06→2021-12 | 624 | -2.72% | 0.250 |
| test | 2022-01→2026-07 | 1126 | +0.48% | 0.137 |

## Per-crisis (headline strangle daily-return compounded total over each window)

| window | n_days | strangle total return |
|---|---|---|
| 2018Q4 | 63 | -3.60% |
| COVID | 62 | +1.16% |
| 2022 | 250 | +3.31% |
_A naked short-vol book is SUPPOSED to bleed here; the full-cycle alpha is the question, not any single crisis._

## Data window & coverage

- Trading days in window: 2111 (2018-06-01..2026-07-03).
- SPX daily-return series: 2030 days (2018-06-04..2026-07-02), source = warehouse `underlying_price` (continuous across the NBBO blackout).
- Weekly ladder entries: 423; genuinely quoted: 319; blackout-skipped weeks: 104 (2020-08-13→2021-12-31 NBBO blackout).
- Strangle = wingless condor; UNCAPPED intrinsic settlement max(0,K_put−S)+max(0,S−K_call). Reuses S7 honest-fill helpers (_sell_price/_buy_price), clean-delta selection, price-map cache, forward-walk; strictly causal.

## Method notes

- Daily book mark-to-market: equity(d) = Σ premium collected − Σ current two-leg buy-back liability at fill f; expiry marks use UNCAPPED settled intrinsic. Daily return = Δequity ÷ reserved capital (Σ put_strike·100 over open strangles that day).
- Regression r_str = alpha + beta·r_spx + e on aligned daily returns; alpha annualized ×252. 95% CI via stationary block bootstrap (block≈20d, 2000 resamples, seed 20260706). Placebo seed 20260706.
- No parameter tuned to the data. Warehouse read-only. Frozen config untouched.
