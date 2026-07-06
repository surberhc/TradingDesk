# PRE-REGISTRATION — Short strangle: is there VRP ALPHA once the trade is delta-neutral?

**Registered:** 2026-07-06 (written and committed BEFORE any run — the timestamp is the point).
**Author:** desk research (Claude), on Andrew's explicit instruction ("run Tom's favorite trade", 2026-07-06).
**Status at registration:** hypothesis only. No strangle result exists yet.

> This is the DECISIVE VRP test on SPX. The managed iron condor is refuted (both tenors); the
> cash-secured put is refuted-as-alpha (its +P&L was long-equity beta, daily Sharpe ~0.00). The
> short strangle is the one vehicle left that is ~DELTA-NEUTRAL by construction (short OTM put +
> short OTM call, offsetting deltas), so it is NOT structurally long the market — there is almost
> no beta to explain its returns. Therefore any positive risk-adjusted return net of honest fills
> IS the volatility risk premium, cleanly isolated. Tom Sosnoff: "if I could trade only one
> strategy it'd be selling premium/strangles." A finding of "no alpha even here" comprehensively
> refutes mechanical SPX premium-selling and is a full, valid, headline outcome.

## 1. Hypothesis
Selling a ~16-delta SPX short strangle (short OTM put + short OTM call), weekly-laddered, actively
managed (close at 50% of credit or 21 DTE), harvests the volatility risk premium as **positive
risk-adjusted return that is NOT explained by equity beta** (because the position is ~delta-neutral),
net of honest half-spread fills, surviving the fat-tail losses of 2018-Q4 / 2020 / 2022.

**Honest doubt (the null):** net of realistic fills on two legs and after the uncapped crash losses
a naked short-vol book takes in 2018Q4/2020/2022, the calm-period premium is fully given back — the
daily-return alpha CI spans 0, the Sharpe is ~0, and "sell premium" is a cosmetic-high-win-rate
mirage (win most weeks, give it all back in the crashes). The corroborating independent 11-yr SPX
backtest of the tastytrade 16-delta formula (65% win rate, avg winner +10%, avg loser −24%, poor
net) is the prior we are testing against.

## 2. Chassis (frozen for this study)
- **Structure:** short strangle — sell short put ≈ target delta AND short call ≈ target delta,
  NO protective wings (undefined risk), via the clean-delta selection path. Cash/margin basis:
  reserved capital = **put_strike × 100** per open strangle (cash-secure the dominant put tail;
  chosen for consistency with the CSP study so returns are apples-to-apples — note Sharpe, beta,
  and the SIGN/t-stat of alpha are all basis-invariant, only the annualized alpha % scales).
- **Grid:** short delta target **{0.16 (Tom's canonical), 0.20}** × DTE **{30, 45}** × management
  **{managed = 50%-target-or-21-DTE, hold-to-expiry (control)}** × fill fraction
  **{0.0=mid, 0.25, 0.50=HEADLINE, 1.0=cross}**.
- **Entry cadence:** weekly ladder (first trading day of each ISO week), concurrent independent book,
  1 strangle per weekly entry.
- **Settlement:** cash-settled at UNCAPPED intrinsic — put side max(0, K_put − S), call side
  max(0, S − K_call). No wing cap (this is the undefined-risk realism a naked strangle actually has).
- **Fills — honest, never mid-only:** fraction f of each leg half-spread on entry (sell → toward bid),
  every daily mark (buy-back → toward ask), and management close; propagated through the profit-target
  trigger. Reuse the S7 honest-fill helpers unchanged.
- **Data window:** warehouse EOD SPX chains 2018-06 → 2026-07. Corruption handling + the
  2020-08→2021-12 quote blackout inherited unchanged (blackout weeks unquotable → skipped; report the
  covered window). 2020-2021 delta selection uses the clean-delta BSM re-inversion (never vendor delta).

## 3. The alpha test (headline — same three lenses as the CSP study, DAILY-return based)
1. **Beta regression (PRIMARY VERDICT):** daily mark-to-market P&L of the whole open-strangle book ÷
   reserved capital = r_str. Regress r_str = alpha + beta·r_spx + e on SPX daily returns. Report
   **annualized alpha, beta, R², alpha t-stat, and a stationary block-bootstrap 95% CI** (block ≈ 20d,
   ≥2000 resamples, fixed seed np.random.default_rng(20260706)). CONFIRM the position is actually
   delta-neutral: **beta should be ≈ 0** (report it). If beta ≈ 0, then a positive-alpha CI-excluding-0
   IS clean VRP alpha; if alpha ≤ 0 / CI spans 0, the premium is eaten and it is REFUTED.
2. **Cash / T-bill benchmark:** annualized return, Sharpe, Sortino, max drawdown vs risk-free — since
   the book is ~delta-neutral, THIS (not a delta-matched SPX arm) is the relevant benchmark: does the
   strangle beat cash on a risk-adjusted basis at all, net of honest fills?
3. **Management vs hold vs placebo:** the managed arm must beat hold-to-expiry on TOTAL P&L AND beat a
   random-exit-matched-holding placebo (block bootstrap of exit days matching the managed holding-period
   distribution, ≥200 seeds, fixed seed) on TOTAL P&L. Prior desk work found management cosmetic — retest.

## 4. Discipline
- **OOS split:** train 2018-06→2021-12, test 2022-01→2026-07 — alpha positive in BOTH halves.
- **Per-crisis:** 2018-Q4, 2020-02→2020-04 (COVID), 2022 (bear), reported separately — this is where a
  naked short-vol book is SUPPOSED to bleed; the full-cycle alpha is the question.
- **Plateau, not peak:** reported across delta {16,20} × DTE {30,45} × fill band; verdict rests on a
  robust region, not one cell. No parameter swept to maximize anything.
- **Fill honesty:** headline at f=0.50; the alpha claim must survive mid→0.50. Positive only at mid = refuted.

## 5. Pass criteria (ALL required — else REFUTED)
1. **Positive alpha:** annualized regression alpha > 0 with bootstrap 95% CI excluding 0, at the headline
   config, holding across the mid→0.50 fill band.
2. **Genuinely delta-neutral:** |beta| small (the isolation is real, not a contaminated directional book).
3. **Beats cash risk-adjusted:** positive daily-return Sharpe & Sortino net of honest fills.
4. **OOS survival:** positive alpha in BOTH train and test halves.
5. **Plateau:** positive alpha across delta {16,20} × DTE {30,45} and the fill band — not one cell.
6. **Crisis survivability:** full-cycle alpha stays positive through 2018Q4/2020/2022; AND (if the managed
   arm is the headline) management beats hold + the random-exit placebo on TOTAL P&L.

Fail 1, 3, 4, 5, or 6 → **REFUTED** (mechanical SPX premium-selling produces no clean VRP alpha — a
comprehensive, valid, headline refutation across condor + CSP + strangle). If it PASSES robustly, the
strangle GRADUATES to a pre-registered deployment study and anchors the diversified tastytrade premium
suite (short strangle across a high-IV-rank universe) once the snapshot download lands. **A clean
refutation is a full result** and will be reported as the headline.

## 6. Deliverables
- Engine: backtester/short_strangle.py (imports and reuses s7_income_condor.py + the CSP regression/
  bootstrap/benchmark code; a strangle = wingless condor with uncapped intrinsic settlement). No tuning.
- Report: backtester/output/short_strangle_alpha_2026-07-06.md — LEAD WITH THE VERDICT (alpha or eaten?),
  regression table (alpha/beta/t/CI per delta × DTE × fill), cash benchmark, management-vs-hold-vs-placebo,
  OOS, per-crisis, and the 6-criteria PASS/FAIL walk with numbers.
- Tests: extend backtester/tests/ — strangle daily mark is causal (no look-ahead) + cost-charged;
  uncapped-intrinsic settlement is correct (a deep crash produces a large uncapped put-side loss);
  reuse the alpha-detector sanity guard (pure-beta series → alpha CI spans 0).
- Per-trade + daily-series CSVs to backtester/output/s7_research/. Frozen config untouched, warehouse read-only.
