# PRE-REGISTRATION — CSP: does short-put premium-selling produce ALPHA, or just equity BETA?

**Registered:** 2026-07-06 (written and committed BEFORE any run — the timestamp is the point).
**Author:** desk research (Claude), on Andrew's explicit instruction ("go, draft the CSP prereg and run it", 2026-07-06).
**Status at registration:** hypothesis only. No disciplined CSP result exists yet — the +$718k figure below is an un-interrogated benchmark arm from the S7 rebuild, NOT a validated edge.

> A short ATM put is mechanically ≈ (long ~0.5 delta of SPX) + (short volatility). The 2018→2026
> test window was a strong bull market. Therefore a large positive CSP P&L is EXPECTED even with
> zero volatility risk premium — it can be pure long-equity beta. This study exists to separate
> the two. A finding of "it's just beta" is a VALID, expected, headline outcome and will be
> reported as REFUTED-as-alpha, not quietly re-specified until it looks like edge.

## 1. Hypothesis
Selling the ATM (~50-delta) SPX cash-secured put, weekly-laddered and held to expiry, harvests
the volatility risk premium and delivers **positive risk-adjusted return that is NOT fully
explained by equity beta** — i.e. a positive alpha intercept after regressing its returns on
SPX, net of honest half-spread fills, across a full cycle including 2018-Q4 / 2020 / 2022.

**Honest doubt (the null we are actively trying to confirm):** the CSP's return is fully
explained by its long-delta exposure to a rising market (beta), the alpha intercept is ≈0 or
negative after fills, and a delta-matched SPX buy-and-hold matches or beats it — in which case
the CSP is "expensive beta in a costume," not an edge, and you would do as well or better simply
holding the index.

## 2. Chassis (frozen for this study)
- **Strategy:** sell the nearest-ATM (|delta|≈0.50, via the clean-delta path) SPX put; weekly
  ladder (one new put per calendar week); **hold to expiry**; cash-settled at intrinsic
  max(0, K − settle). 1 contract per weekly entry. DTE targets **{30, 45}**.
- **Capital / cash-secured:** each open put reserves **K × 100** dollars (cash-secured, no
  leverage). Return-on-capital uses the reserved capital, marked daily.
- **Fills — honest, never mid-only:** fraction f of the leg half-spread on both entry (sell →
  toward bid) and every daily mark / settle, f ∈ **{0.0=mid, 0.25, 0.50=HEADLINE, 1.0=cross}**.
  Reuse the S7 honest-fill helpers unchanged.
- **Marking:** each trading day, mark every open put at that day's EOD bid/ask at fill f (buy-back
  price) → a daily mark-to-market P&L series for the whole CSP book.
- **Data window:** warehouse EOD SPX chains 2018-06 → 2026-07. Corruption handling and the
  2020-08→2021-12 quote blackout are inherited unchanged from the S7 engine (blackout weeks are
  unquotable → skipped; report the covered window). 2020-2021 ATM selection uses the clean-delta
  BSM re-inversion (never the corrupt vendor delta).

## 3. The alpha-vs-beta test (the headline — three independent lenses)
1. **Beta regression (PRIMARY VERDICT):** form the CSP book's daily return series r_csp (daily
   mark-to-market P&L ÷ reserved capital). Regress r_csp on the SPX daily return r_spx:
   r_csp = alpha + beta·r_spx + e. Report **alpha (annualized), beta, R², and alpha's t-stat plus
   a stationary block-bootstrap 95% CI** (block length ≈ 20 trading days, ≥2000 resamples, fixed
   seed np.random.default_rng(20260706)). Alpha > 0 with a CI excluding 0 = evidence of edge
   beyond beta; alpha ≤ 0 (or CI spanning 0) = the return is beta, REFUTED-as-alpha.
2. **Delta-matched SPX buy-and-hold (INTUITIVE BENCHMARK ARM):** compute the CSP book's daily
   aggregate dollar-delta (Σ short-put exposure = Σ |put_delta|·spot·100 per open contract, using
   the clean delta). Build a long-SPX position sized to the book's **time-average dollar-delta**,
   buy-and-hold over the same window on the same reserved capital. Compare **Sharpe, Sortino, max
   drawdown, total return**. The CSP earns the "edge" label only if it delivers **comparable-or-
   better risk-adjusted return (Sharpe/Sortino) at less-or-equal beta** — i.e. it is NOT dominated
   by simply holding the matched index exposure.
3. **Capital-matched raw benchmark (context):** SPX buy-and-hold on the identical reserved-capital
   dollars (1:1), reported for context — the CSP is EXPECTED to trail this on raw total return in a
   bull market (it caps upside) but should win on risk-adjusted terms if the VRP is real. Raw-return
   underperformance alone is NOT a refutation; risk-adjusted underperformance IS.

## 4. Discipline (mirrors the S7 prereg)
- **OOS split:** train 2018-06→2021-12, test 2022-01→2026-07. Alpha must be positive in BOTH halves
  (not carried by one regime).
- **Per-crisis:** 2018-Q4, 2020-02→2020-04 (COVID), 2022 (bear) — reported separately (short puts
  are supposed to bleed here; the full-cycle alpha is the question).
- **Plateau, not peak:** results reported across DTE {30,45} × the full fill band; the verdict rests
  on a robust region, not one cell. No parameter is swept to maximize anything.
- **Fill honesty:** headline at f=0.50; the alpha claim must survive the mid→0.50 band. Positive only
  at mid = refuted.

## 5. Pass criteria (ALL required — else REFUTED-as-alpha)
1. **Positive alpha:** regression alpha > 0, annualized, with a bootstrap 95% CI excluding 0, at the
   headline config, **holding across the mid→0.50 fill band.**
2. **Not dominated by delta-matched SPX:** CSP Sharpe & Sortino ≥ the delta-matched SPX arm's
   (comparable-or-better risk-adjusted return at ≤ its beta).
3. **OOS survival:** positive alpha in BOTH train and test halves.
4. **Plateau:** the positive-alpha finding holds across DTE {30,45} and the fill band — not one cell.
5. **Crisis survivability:** full-cycle beta-adjusted return stays positive through 2018Q4/2020/2022.

Fail any → the honest verdict is one of: "CSP return is equity beta, not VRP alpha (REFUTED-as-alpha)";
or "CSP has alpha but is dominated by holding the matched index"; or "alpha exists only at unrealistic
fills." **A clean 'it's just beta' result is a full, valid, headline finding** and directly answers
Andrew's question. If instead the alpha is real, robust, and not dominated — that GRADUATES the CSP to
a pre-registered deployment study and anchors the broader tastytrade premium suite.

## 6. Deliverables
- Engine additions in backtester/s7_income_condor.py (or a sibling backtester/csp_alpha_beta.py that
  imports it): daily book mark-to-market, book dollar-delta series, beta regression + block bootstrap,
  delta-matched + capital-matched SPX benchmarks. No tuning to data.
- Report backtester/output/csp_alpha_vs_beta_2026-07-06.md: LEAD WITH THE ALPHA-VS-BETA VERDICT, then
  the regression table (alpha/beta/t/CI per DTE × fill), the three benchmark comparisons, OOS, per-
  crisis, and the 5-criteria PASS/FAIL walk with numbers.
- Tests: extend backtester/tests/test_s7_income_condor.py — a CSP book mark-to-market is causal
  (no look-ahead) and cost-charged; a synthetic-data sanity check that a pure-beta series yields
  alpha≈0 (the regression correctly reports NO edge when there is none).
- Per-trade + daily-series CSVs to backtester/output/s7_research/. Frozen config untouched, warehouse
  read-only.
