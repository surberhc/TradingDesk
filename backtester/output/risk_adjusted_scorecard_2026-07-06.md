# RISK-ADJUSTED SCORECARD — premium-selling vehicles vs SPX (drawdown-avoidance lens)

**Run:** 2026-07-06 | RE-ANALYSIS of already-committed session results through a RISK lens. No new strategy, no param tuning, no curve-fit surface. Window 2018-06-04 .. 2026-07-02.

## VERDICT (lead)

### **NO. Not one vehicle beats a risk-matched, de-risked index — on vol-match OR drawdown-match — unlevered. "Just hold less SPX and keep the rest in cash" wins at every risk level.**

Andrew's reframe was the right question: forget alpha (already refuted), does any vehicle deliver a **genuinely smoother ride worth the complexity, especially on drawdown?** Tested honestly against the trivial alternative — de-risk the index itself with cash to the same vol or the same max-drawdown — the answer is **no on all five vehicles, on both lenses**:

- **Vol-match:** every vehicle's Sharpe (−0.29 to +0.07) is far below SPX's Sharpe on the matched calendar (**0.46**). Equivalently, at each vehicle's own vol, a `w·SPX + (1−w)·cash` blend earns **+6.5% to +12.3%/yr**; the vehicles earn **−0.0% to +5.2%/yr**. The blend wins every time.
- **Drawdown-match:** every vehicle's Calmar (−0.07 to +0.045) is far below SPX's Calmar on the matched calendar (**0.32–0.42**). At each vehicle's own max-drawdown, a de-risked-index blend earns **+7.5% to +12.1%/yr**; the vehicles earn **−0.0% to +5.2%/yr**. The blend wins every time.

**The one genuine, honest win worth flagging:** the strangles (and especially the regime-gated strangle) DO avoid the deep crisis holes — dramatically. In the 2022 bear SPX fell −25%; the ungated strangle drew down just −5.4%, the managed −4.8%. In 2018-Q4 SPX −20% vs gated −3.6%. That shallow-crisis behaviour is real and is exactly what a short-vol book with defined reserved capital should do. **But it is not enough return to beat holding less index** — the drawdown-matched blend already captures that shallow-drawdown budget and pairs it with more return. Smoothness alone, with ~zero excess return, loses to cash-plus-index. There is no free risk-management lunch here.

Bottom line: these are **beta-diluted, capital-inefficient index substitutes**, not smoother-ride machines. Complexity is not paid for.

---

## Master scorecard (daily book returns on reserved capital; rf = 3.0%)

SPX row = full 1:1 buy-and-hold on the full window. Each vehicle row uses its own daily series; the risk-matched section below compares each against SPX **on that vehicle's exact aligned calendar** (so the denominators differ slightly from this SPX row — see note).

| vehicle | n | CAGR | ann.ret | ann.vol | maxDD | Sharpe | Sortino | Calmar | Ulcer | beta | total ret |
|---|---|---|---|---|---|---|---|---|---|---|---|
| **SPX buy-and-hold (1:1)** | 2030 | **13.31%** | 14.39% | 19.41% | **−33.9%** | **0.59** | **0.56** | **0.39** | 8.4 | 1.00 | +173.7% |
| CSP 45DTE hold (f0.5) | 1750 | −2.00% | −0.04% | 20.10% | −27.4% | −0.15 | −0.15 | −0.07 | 14.5 | 0.55 | −13.1% |
| CSP 30DTE hold (f0.5) | 1750 | +1.14% | +5.17% | 30.09% | −33.4% | +0.07 | +0.09 | +0.03 | 14.5 | 0.53 | +8.2% |
| Strangle 16d/45 UNGATED hold | 1750 | −0.44% | +0.33% | 12.75% | −26.4% | −0.21 | −0.23 | −0.02 | 7.1 | 0.20 | −3.0% |
| Strangle 16d/45 UNGATED managed | 1750 | +1.07% | +1.57% | 10.13% | −23.8% | −0.14 | −0.13 | +0.04 | 4.2 | 0.20 | +7.6% |
| Strangle 16d/45 REGIME-GATED hold | 672 | +0.16% | +0.52% | 8.55% | **−13.9%** | −0.29 | −0.28 | +0.01 | 4.1 | 0.32 | +0.4% |
| _S7 condor headline (completeness)_ | _324 tr_ | _neg_ | _−$73,715 total_ | — | _−$116k_ | _−0.51_ | — | — | — | — | _negative_ |

_S7 condor headline has a **negative** total P&L (−$73,715, Sharpe −0.51 at trade level); risk-adjusted ranking is moot — a negative-return sleeve cannot beat cash on any drawdown-adjusted basis. Listed for completeness only._

**Sharpe/Sortino use rf=3% (they go negative because most vehicles under-return cash).** Calmar uses raw CAGR/|maxDD| (no rf). The strangles' **Ulcer index is genuinely low** (4.1–7.1 vs SPX 8.4) — the smoothest *shapes* on the board — but low ulcer with sub-cash return is not a win.

## THE RISK-MATCHED VERDICT (unlevered — de-risk the INDEX with cash)

For each vehicle: choose `w` so a `w·SPX + (1−w)·cash` blend matches the vehicle's **vol** (then its **maxDD**), on that vehicle's exact aligned SPX calendar. Vehicle wins iff its ann.return > the matched blend's. `vol_cap`/`dd_cap` flags mark cases where matching would need w>1 (leverage) — clamped to w=1, never levered.

| vehicle | veh ann.ret | veh vol | veh maxDD | **VOL-match**: w | blend ret | vehicle wins? | **DD-match**: w | blend ret | vehicle wins? |
|---|---|---|---|---|---|---|---|---|---|
| CSP 45DTE hold | −0.04% | 20.1% | −27.4% | 0.99 | +12.18% | **NO** | 0.79 | +10.28% | **NO** |
| CSP 30DTE hold | +5.17% | 30.1% | −33.4% | 1.00¹ | +12.26% | **NO** | 0.98 | +12.08% | **NO** |
| Strangle UNGATED hold | +0.33% | 12.8% | −26.4% | 0.63 | +8.82% | **NO** | 0.75 | +9.98% | **NO** |
| Strangle UNGATED managed | +1.57% | 10.1% | −23.8% | 0.50 | +7.62% | **NO** | 0.68 | +9.26% | **NO** |
| Strangle REGIME-GATED hold | +0.52% | 8.6% | −13.9% | 0.54 | +6.55% | **NO** | 0.69 | +7.52% | **NO** |

¹ CSP 30DTE vol (30.1%) exceeds SPX vol (~20%); vol-match would require leverage, so w clamped to 1.0 (full SPX). Its own vol is *higher* than the index — it is not a smoother ride at all; it's a more volatile one that also under-returns.

**Both ways, both lenses: no vehicle clears the bar.** Equivalent Sharpe/Calmar comparison (same conclusion, transparent):

| vehicle | veh Sharpe | SPX Sharpe (aligned) | veh Calmar | SPX Calmar (aligned) |
|---|---|---|---|---|
| CSP 45DTE hold | −0.15 | 0.46 | −0.07 | 0.32 |
| CSP 30DTE hold | +0.07 | 0.46 | +0.03 | 0.32 |
| Strangle UNGATED hold | −0.21 | 0.46 | −0.02 | 0.32 |
| Strangle UNGATED managed | −0.14 | 0.46 | +0.04 | 0.32 |
| Strangle REGIME-GATED hold | −0.29 | 0.42 | +0.01 | **0.42** |

The gated strangle is the closest call — its Calmar (0.011) is being compared to an aligned-SPX Calmar of 0.42 over the same 672 gated-on days. Even its best feature (shallow drawdown) is dominated: the de-risked index at −13.9% maxDD earns +7.5%/yr; the gate earns +0.5%/yr.

## DRAWDOWN-AVOIDANCE DETAIL (Andrew's priority) — max drawdown WITHIN each crisis

This is the honest bright spot for the strangles, shown in full. Vehicle maxDD measured within the crisis window (equity re-based at window start) vs SPX over the same dates.

| crisis | SPX | CSP 45DTE | CSP 30DTE | Strangle UNGATED hold | Strangle UNGATED managed | Strangle GATED hold |
|---|---|---|---|---|---|---|
| 2018-Q4 (Oct–Dec) | **−19.6%** | −9.8% | −11.6% | −4.4% | −6.5% | **−3.6%** |
| COVID (Feb 15–Apr 30 2020) | **−33.9%** | −27.4% | −30.6% | −20.1% | −17.8% | _stood down (no book)_ |
| 2022 bear (full year) | **−25.4%** | −17.4% | −18.7% | **−5.4%** | −4.8% | _stood down (no book)_ |

**Read:** The strangles genuinely avoided the deep holes — the ungated strangle's −5.4% in the 2022 bear vs SPX's −25.4% is a striking 5× shallower ride, and the managed strangle cut COVID roughly in half (−17.8% vs −33.9%). The regime gate did its job structurally: it **stood down entirely** through COVID and most of the 2022 bear (no open book → no crisis loss to report), and posted the single shallowest 2018-Q4 drawdown (−3.6%). So on the narrow question "did they avoid the deep holes," **yes, clearly.** The problem is only revealed by the risk-match: that drawdown avoidance is not scarce — you can buy the same shallow-drawdown budget by simply holding less index, and get MORE return for it. The strangles trade the crash-hole for near-zero return the rest of the time; the de-risked index trades it for a positive drift.

## Why (mechanism, not mystery)

Every vehicle here is **long-beta-diluted premium selling**: betas 0.20–0.55, returns compressed toward zero, drawdowns softened. That is mathematically a blend of index and cash — which is *exactly* the benchmark it's being tested against, but built the expensive way (weekly option ladders, fills, management, gates) instead of the free way (buy less SPX, hold T-bills). With **zero clean VRP alpha** (the committed studies already established this: CSP is beta not alpha, strangle alpha CI spans 0, the gate's edge is beta not premium-harvesting), there is no premium left to make the complex path win. Smoothness with no excess return is a cash allocation in disguise.

## Method / honesty notes

- **Daily-return based throughout** — never trade-level ×√52. Each vehicle is its committed engine's daily book-return series on reserved capital.
- **Reused:** CSP-45 / strangle-managed / regime-gated daily series from the committed engine dumps (`output/s7_research/*_headline_daily.csv`), reproduced to the committed grid metrics exactly. **Reconstructed** (byte-identically, via the committed engines' own `analyze_cell` with their frozen window constants): CSP-30DTE hold and strangle-UNGATED hold daily series — total P&L and n_trades matched the committed grid to the dollar ($431,523 / $156,658) and Sharpe/maxDD to the grid CSV. SPX daily series = warehouse `underlying_price` (continuous across the 2020-08→2021-12 NBBO blackout since only quotes were zeroed); validated against the committed capital-matched-SPX benchmark (Sharpe 0.605, maxDD −33.9%, +102.96% — exact match).
- **rf = constant 3.0%** annualized (single-number 3-mo T-bill proxy; the committed strangle cash benchmark used ~3%). Ranking is rf-robust: all vehicles and both blends share the same rf, so the *comparison* is invariant to the rf level. (A time-varying rf — ~0% in 2020-21, ~5% in 2023-24 — would raise the de-risked blend's cash leg return in the high-rate years, making the blend *even harder* to beat; the verdict cannot flip against the vehicles.)
- **Blackout coverage:** the 2020-08→2021-12 NBBO blackout means the option books skip ~104 entry-weeks; the daily marks flat-line through gaps (visible in the figure). SPX itself is continuous, so risk-matching is done on each vehicle's open-book days only. n=1750 (CSP/ungated strangle) / n=672 (gated, 18% duty cycle).
- **No leverage introduced anywhere** — where a vehicle's vol/maxDD exceeds SPX's, w is clamped to 1.0 and flagged; we report the ratio (Sharpe/Calmar) comparison instead.
- Reusable metric helpers (`max_drawdown`, `sortino`, `calmar`, `ulcer`, risk-match blend solvers) live in `risk_adjusted_scorecard.py`, covered by `tests/test_risk_adjusted_scorecard.py` (10 hand-checked tests, all green).

## Outputs

- This report.
- Figure: `output/s7_research/risk_scorecard_2026-07-06.png` (top: normalized log-equity all vehicles + SPX; bottom: underwater drawdown; crisis windows shaded; per-vehicle metrics in legend).
- Machine-readable: `output/s7_research/risk_scorecard_2026-07-06.csv` (one row per vehicle, all metrics + risk-match results).
