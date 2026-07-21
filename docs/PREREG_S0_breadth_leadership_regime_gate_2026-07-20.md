> **SHELVED / NOT REGISTERED (2026-07-20).** This draft was never signed off or run. Andrew halted the exercise: the test would be proxy-on-proxy — no constituent-level breadth data exists (the warehouse is ETF-only, so no true "% of stocks above 200d" or advance-decline), the full S0 chassis can't run pre-2015, and the equity sleeve is a small regime-throttled slice — so any result would be uninterpretable, not useful information. Kept for the design reasoning only. Do NOT treat as a live pre-registration.

# PRE-REGISTRATION — S0: is there a NARROW-vs-BROAD leadership regime, and does gating equity-sleeve breadth on it beat static broad-beta (especially at the re-entry off a bottom)?

**Registered:** 2026-07-20 (written and committed BEFORE any run — the timestamp is the point).
**Author:** desk research (Claude), on Andrew's explicit instruction ("write it up as the next pre-registration", 2026-07-20).
**Status at registration:** hypothesis only. Direct follow-on to the completed broaden-equity-sleeve study (prereg `bbd4f6d`; result report `555fc2a`) — a robust NO on a STATIC breadth tilt. That study's failure MECHANISM — 2015–2026 was an abnormally narrow, mega-cap-led bull in which cap-weight beat breadth — is the hypothesis generator here.

> **Honest framing / the big risk.** This is a REGIME-SWITCH study — the single most overfitting-prone kind of research there is: with hindsight you can always find a switch that "would have helped." The ONLY thing separating a real regime from a curve-fit story is REGIME REPLICATION — the gate must work across MULTIPLE INDEPENDENT broad AND narrow regimes, walk-forward, on data it was not chosen on. A very likely and fully VALID outcome is **"we do not have enough independent broad-leadership regimes to prove this"** — that is an honest finding, not a failure to be tuned away. Judged on net merit; the anti-curve-fit gates are hard and non-negotiable (CLAUDE.md rule #1).

## 0. What the last study established (the setup)
- Static broadening of S0's equity sleeve (small/mid + momentum sectors) is a small CONSISTENT DRAG over 2015–2026 and fails on the bull thesis. NOT adopted; ships default-OFF.
- The failure was REGIME, not idea: cap-weight beat equal-weight / small-cap / sector rotation because leadership was extraordinarily narrow (a handful of mega-caps carried the index).
- Key gap it exposed: S0's regime engine measures market HEALTH (how MUCH equity to hold — risk-on vs risk-off). It is BLIND to leadership STRUCTURE (narrow vs broad). That missing axis is the whole subject of this study.

## 1. Hypothesis
There is a measurable, persistent LEADERSHIP-BREADTH regime — narrow (a few mega-caps carry the index) vs broad (gains are widely shared). When leadership is BROAD, broadening the equity sleeve (small/mid + sectors) adds return; when NARROW, it drags (as the last study showed). A gate that turns the breadth tilt ON only in broad regimes — and especially one that tilts the RE-ENTRY off a market bottom toward broad participation — beats BOTH static broad-beta AND the always-on tilt, across multiple independent regimes.

**Honest doubt (the null we actively try to confirm):** (a) breadth "regimes" are not persistent/predictive enough to trade — by the time a simple signal flips, the broad phase is already over (whipsaw), so the gate adds nothing net; (b) there are too few independent BROAD regimes in testable data to distinguish a real gate from luck; (c) even a PERFECT gate moves S0's total return/drawdown by only a rounding-error fraction, because equity is a small, regime-throttled sleeve (portfolio beta ~0.22); (d) any apparent win comes from look-ahead in how the regime is labeled. Any of these → "no usable gate," reported plainly.

## 2. The CEILING CHECK first (cheap; do this before building any signal)
Before constructing any breadth signal, BOUND THE PRIZE. Using a PERFECT-HINDSIGHT breadth oracle (tilt ON in every broad regime, OFF in every narrow one — look-ahead allowed ON PURPOSE, as an upper bound only), run S0 full-chassis 2015→present plus the equity-sleeve-only long history, and report the MAXIMUM possible improvement in total-portfolio CAGR and drawdown. If even the cheating oracle adds only a few bps at the PORTFOLIO level, then the realistic (non-cheating) gate cannot be worth heavy engineering — we say so and STOP. This directly answers Andrew's instruction to size the payoff before investing heavily, and it can end the study in one cheap run.

## 3. Data plan (the hard part — stated honestly)
The breadth evidence that matters most is largely PRE-2015, and S0's defensive/real-asset ETFs do not reach back that far — so this is an EQUITY-SLEEVE-ONLY study (broad-beta core vs breadth-gated sleeve), NOT full-chassis, except the 2015→ ceiling check where the full chassis exists.
- **Phase A (data we already have or can pull cheaply, 2007→present):** the ETF era already spans at least one BROAD regime (2009–2013 recovery — small-cap and equal-weight led) and multiple NARROW ones (2015–2021, 2023–2025), plus the 2020 and 2022 stress. Enough for a FIRST both-sided read. Fail fast here before any big data project.
- **Phase B (CONDITIONAL — only if Phase A is promising):** extend to ~1990 with index / older-fund proxies (e.g. S&P 500 cap-weight vs S&P Equal-Weight total-return indices as the RSP/SPY proxy pre-ETF; a long small-cap index; a long breadth series) to add INDEPENDENT regimes — broad: 1991–98, 2003–07, 2009–13; narrow: 1998–2000, 2015–21, 2023–25. Acquiring and QC-ing this history is itself a work item and a possible blocker; it is named here so extending scope is a DELIBERATE decision, not silent creep.

## 4. The leadership-breadth signal (pre-specified and SIMPLE — the curve-fit firewall)
To avoid inventing a tuned composite, the regime is read from a SMALL, pre-committed set of established, individually-simple measures:
- **Equal-weight vs cap-weight TREND:** sign of the trailing (6–12m) return of S&P Equal-Weight minus S&P cap-weight (RSP−SPY; index proxy pre-ETF). Broad = equal-weight leading.
- **Market BREADTH:** % of index members above their 200-day moving average (or the closest available breadth proxy). Broad = high participation.
- **LEADERSHIP concentration:** top-10 (or top-5) index weight / a simple concentration ratio. Broad = de-concentrating.
Each is a plain, decades-old measure. The regime LABEL is a SIMPLE combination (e.g. majority vote, or sign of the average z-score), NOT an optimized weighting. Thresholds are SWEPT for a plateau, never tuned to a peak. Walk-forward: the label at date T uses only data available on or before T.

## 5. The gate + the re-entry variant
- **Full-cycle gate:** when regime = BROAD, allow the last study's breadth tilt (momentum-gated small/mid + sectors, the same RS-vs-SPY + 200d-trend basis); when NARROW, force broad-beta (tilt OFF). "WHICH names" reuses the last study's already-blessed momentum gate; this study only adds "WHETHER to tilt," supplied by the breadth regime.
- **Re-entry-targeted variant (Andrew's strongest intuition — likely the headline):** apply the breadth tilt ONLY during S0's re-entry ladder off a bottom, and only when leadership is broadening — capturing early-cycle breadth thrusts on a GROWING equity base, then reverting to broad-beta once re-entry completes. This is where the payoff caveat in §1(c) is weakest, because the equity base is being rebuilt exactly then, and it ties into machinery S0 already owns.

## 6. Test arms and controls (separating a real gate from a hindsight story)
1. **STATIC BROAD-BETA** (baseline; the last study's winner).
2. **ALWAYS-ON tilt** (the last study's loser).
3. **GATED tilt — full-cycle**, and **3b. GATED tilt — re-entry-only** (the candidates). To count, a gate must beat BOTH (1) and (2): add (2)'s broad-regime upside WITHOUT its narrow-regime drag.
4. **PERFECT-HINDSIGHT oracle** (look-ahead on purpose) — the ceiling/upper bound from §2.
5. **PLACEBO / shuffled regime** (randomly relabelled regimes, fixed seed `np.random.default_rng(20260720)`) — the gate must beat its OWN placebo, or the "regime" is just noise.
Per-regime attribution: report each arm's return SEPARATELY within every labelled broad and narrow episode, so the gate's claim ("on in broad, off in narrow") is checked episode by episode, not just in aggregate.

## 7. Discipline
- **Regime replication is the whole game:** the gate must help in BROAD and not-hurt in NARROW across MULTIPLE INDEPENDENT episodes, walk-forward, including episodes outside any window used to choose thresholds.
- **Walk-forward labeling:** no look-ahead in the regime signal (except the deliberately-cheating oracle arm, which is labelled as such).
- **Plateau, not peak:** results across a contiguous threshold region and across the signal-combination choices; no value tuned to maximize anything.
- **Simplicity cap:** only the pre-specified small signal set — no expanding the indicator zoo to rescue a null.
- **Placebo must fail; oracle bounds the prize.**
- Frozen production config untouched; study-only flags default-OFF; warehouse read-only; reuse `run_backtest()` and the last study's `parts/equity_tilt.py` harness.

## 8. Pass / adopt criteria — net-merit, with hard anti-curve-fit gates (same philosophy as the last study, signed off by Andrew)
**A. HARD gates — decide whether it is REAL (non-negotiable, CLAUDE.md rule #1):**
- **Regime replication:** the gate adds return in broad regimes AND does not drag in narrow regimes across ≥2 INDEPENDENT instances of EACH, walk-forward, out-of-sample.
- **Beats the placebo:** the gated arm beats its shuffled-regime placebo.
- **Plateau:** holds across a contiguous threshold / combination region, not one lucky cell.
**B. The adopt decision — Andrew's net-merit call; NO hard floor:** given the §2 ceiling, weigh the REALISTIC total-portfolio payoff (return and drawdown) against the added complexity. A gate that is "real but worth ~5 bps to the whole book" may be a legitimate NO on complexity grounds — Andrew decides on balance.
**Valid, honest outcomes (any is a full finding):** "not enough independent broad regimes to prove it" (under-powered — real); "real but immaterial at the portfolio level" (ceiling too low — §2 kills it); "whipsaws — the signal flips after the broad phase is over" (no tradable edge); or a clean, replicated, material gate → graduates to a full-chassis deployment study, still default-OFF until Andrew explicitly arms it.

## 9. Deliverables
- **Ceiling-check script + result FIRST** (§2) — may end the study early; its output is the lead of the report.
- Study-only breadth-regime module + gate (default-OFF), reusing `parts/equity_tilt.py`; a walk-forward regime labeler with unit tests (causal / no look-ahead; the placebo correctly reduces to ≈ baseline).
- Report `backtester/output/S0_breadth_regime_gate_2026-07-20.md`: **LEAD WITH THE CEILING** (max possible payoff), then per-regime attribution across all episodes, the 5-arm comparison, walk-forward OOS across independent regimes, the plateau and placebo checks, and the net-merit exchange rate for Andrew. Makes NO adopt/reject call.
- A Phase-B data-acquisition note (sources + QC plan) only if Phase A warrants it. Frozen config untouched; warehouse read-only.
