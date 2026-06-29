# Session handoff — 2026-06-29

*A clean pickup doc for the next session. Research / PAPER only. Nothing in this session was committed; nothing live was touched.*

---

## Orientation (one paragraph)

This session **finished S4 (SPX Vol-Control Fund)** end-to-end — built, SEC-anchored, cost-modeled, and re-entry-lag-characterized — and then **designed, prototyped, and EOD-validated S5 (Financed Convexity Overlay)**. S5's spec was drafted, its make-or-break V-bottom event study ran and **passed** (the edge is the *passive always-on uncapped tail*, not a discretionary bottom-call), a standalone EOD prototype was built, a 16-cell tail-size sweep was run, and finally the tail-size sweep was **re-priced on REAL market skew** from the now-complete EOD SPXW chain. Result: **S5's defensive / tail half is now validated on real prices** — the design survives honest skew pricing, the frontier shape is unchanged, and the validated default tail shifts to **~0.50 notional / 20–25% OTM**. The **only open piece of S5 is the offensive / harvest (income) half**, which is gated on the intraday 0DTE SPXW pull (running, ~48h from full).

---

## S4 — SPX Vol-Control Fund: DONE

**What it is.** A standalone, single-risk-asset volatility-control fund (FIA/RILA/annuity vol-control replica): SPX (via SPY) + a cash/T-bill leg, scaled **daily** so `exposure = min(leverage_cap, target_vol / realized_vol)` using an asymmetric **max(fast, slow)** realized-vol estimator (de-risk fast, re-risk slow). No regime engine, no bonds — vol-targeting *is* the whole mechanism. It is **not** the rejected vol-trim overlay (that was subordinate to S0's regime band; S4 has no band underneath, so the rejection doesn't apply).

**Where the code / reports live.**
- Strategy: `strategies/strategies/spx_vol_control.py` (`SpxVolControl(StrategyBase)`).
- Runner + sweep: `backtester/s4_vol_control.py` (daily TR/ER runner + 2-D target-vol × leverage-cap sweep; flags `--target-vol / --leverage-cap / --estimator / --cash / --sweep / --report / --cost-bps / --borrow-spread-bps`).
- Reports: `backtester/output/s4_vol_control_20260628.md`, `backtester/output/s4_vol_control_net_of_costs_20260628.md`, `backtester/output/s4_reentry_lag_20260628.md`.

**Validated numbers.**
- **SEC sanity = near-bullseye** (5%-DRC, 5yr→2024-04, cap 1.5): SPX-TR 14.74% → ours 14.93%; DRC-5%-TR 5.68% → 5.70%; DRC-5%-ER 3.55% → 3.75%. No bug; vol-targeting genuinely hits target.
- **Trade-off (full SPY hist 2007–2026) at 10% / 1.5×:** CAGR ≈ **7.5% TR** (vs SPY 10.70%), but **maxDD −20.9%** (vs SPY −55.2%), 2008 −12.7% vs −36.8%; Sharpe/Sortino beat buy-and-hold across the whole surface.
- **Costs:** net drag just **2–15 bp/yr** across the surface (turnover, not financing, is the bigger bite; cap 1.0 borrow drag = 0). 10%/1.5× loses ~5 bp. Gross conclusion survives net-of-costs intact.
- **Re-entry lag:** V-bottom cost at 10%/1.5× ≈ **79 pp** summed over 4 crashes, but **dominated by COVID's sharp V** (~46 pp) + 2018 (~17). It's a V-bottom-*specific* toll, not a uniform drag. **Verdict: NOT worth fixing** — faster-re-entry rules are an overfit trap (catch 2020, misfire on 2008/2022).

**Role.** A conservative **vol dial / bond-alternative at low settings** — smoother risk-adjusted path and far shallower drawdown, *not* raw-CAGR alpha (it structurally cannot beat SPX on CAGR or catch the V-bottom). The exact V-bottom gap S4 cannot close is what S5 attacks structurally.

---

## S5 — Financed Convexity Overlay: designed + EOD-validated, harvest half pending

**Thesis.** Hold a long-SPX core that *permanently carries a financed tail hedge*. Never buy protection after the spike (that's S4's re-entry lag in disguise) — own it always, cheaply, when calm, and finance its theta with calm-day 0DTE SPXW selling. In a crash you already own the hedge; the long-put delta has already auto-de-risked the core all the way down, and on the recovery the same delta auto-re-risks you — **no cash→equity re-entry decision to lag on.** Where S4 dials **delta**, S5 dials **convexity**.

**Resolved design forks.**
- **Constant core + uncapped tail (one coupled decision).** Keep the core constant — the long-put delta *is* the variable exposure, auto-de-risking convexly with no signal/no re-entry timing. This is valid *only if* the tail stays **uncapped** (a capped put-spread's delta flattens at the short strike → auto-de-risk stalls → forces a manual core flex → reinherits S4's timing problem).
- **Laddered ledger waterfall (Tier 1 → Tier 2 → reserve → upside).** Harvest fills a strict priority sequence: **Tier 1** = mandatory deep uncapped tail (always-on floor, funded first); **Tier 2** = optional income-gated put-spread nearer the money (saturating); **Reserve** = mandatory T-bill float carrying Tier-1 carry through a harvest drought (replenish-first, hysteresis); **Surplus → upside** OTM-call barbell, banked-surplus-only, grind-higher-gated (self-timing, self-throttling, house-money).
- **Self-funding ledger.** Protection spending is constrained to what the ledger holds — the hedge budget is *endogenous*. Good times grow the pot → more firepower into the next rough patch; lean times spend less → can't bleed into hedges it can't afford. Cold-start seeded with a small upfront insurance budget.

**Key results.**
- **V-bottom gate PASSED.** Owning the hedge in advance removes the re-entry decision: at each crash bottom S4 sits at ~0.16–0.38× while passive-tail S5 is ~1.0–1.3× (recovery capture GFC ~118% / COVID ~123% / 2022 ~82% vs S4 ~29% / ~21% / ~57%). **Edge = the passive always-on uncapped tail**; the *active* monetize→redeploy trigger was tested and **demoted to Phase-2** (it monetized on dead-cat bounces and levered into the continued drop → deeper drawdown).
- **EOD prototype ≈ SPY return at half the drawdown, keeps upside.** S5 full CAGR ≈ 10.0% / maxDD −28.3% / Calmar 0.35 / Sharpe 0.65, vs SPY 10.70% / −55.2% / 0.19 and S4 7.5% / −20.9%. Melt-up 2023–24 capture 90% vs S4's 64%. Ledger never negative, reserve full 98% of days.
- **Tail validated on real skew.** Measured 63-DTE put skew ≈ +0.71 vol-pts per 1% OTM; the flat-BSM model under-charged the deep tail by 8–12 vol-pts. Honest pricing *trims* (≈ −0.24% CAGR at the new sweet spot) but does **not** break the design — frontier shape identical, smallest tails still a trap, edge still the passive tail. **Validated default: ~0.50 notional / 20–25% OTM, 63 DTE.** Reserve grows to ~0.7–2.1% of NAV (still non-binding). Window caveat: direct real-skew is 2018+; the 2008 GFC view is a calibrated approximation.

**Where the code / reports live.**
- Spec: `docs/S5_SPEC.md` (now includes §1.2 real-skew validation + the validated default).
- Prototype engine: `backtester/s5_convexity_overlay.py`; V-bottom study `backtester/s5_vbottom_eventstudy.py`; sweeps `backtester/s5_tail_sweep.py` + `backtester/s5_tail_sweep_realskew.py`; real-skew table builder `backtester/s5_realskew_build_table.py` → `backtester/output/s5_realskew_table.parquet`.
- Reports: `backtester/output/s5_vbottom_eventstudy_20260628.md`, `s5_prototype_20260628.md`, `s5_tail_sweep_20260628.md`, `s5_tail_sweep_realskew_20260628.md`.

---

## Data warehouse status

- **EOD grab DONE** (completed 2026-06-27): **50 roots × 2018-01-01→2026-06-26**, ~33 GB, rich 41-col schema (per-strike greeks / IV / OI / bid-ask on SPXW). SPY & XSP now fully filled. This is what unblocked the S5 real-skew tail pricing.
- **Intraday SPXW pull RUNNING UNATTENDED** via `spxw_1m_supervisor.py` (Windows Scheduled Task, self-healing, survives session close). **~35% done** (414/1170 weekday-days), pulled newest-first, contiguous 2024-10-29→2026-06-26 on disk, 1-min, all expirations/strikes/rights, zero errors. **ETA ~2026-06-30 ~21:00 CT** (~48h) to reach 2022-01-01.
- **Subscription clock:** ThetaData sub started ~2026-06-25 → **1-month clock lapses ~2026-07-25** (~25 days left). The intraday pull's ETA lands comfortably in-window — risk cleared, no babysitting needed.

---

## WHAT'S LEFT (next steps, prioritized)

1. **Build the S5 real harvest engine** once the intraday SPXW pull completes (~48h, ETA ~2026-06-30). This is the offensive / income half — the only remaining open piece of S5. Replace the EOD daily-condor *proxy* with the real 1-min 0DTE SPXW path to get the **real harvest rate**, the realized surplus that funds Tier-2 + the upside barbell, and the *real* gated calm-day count + loss distribution (the 50% haircut is currently a guess).
2. **Phase-2 active monetization (laddered, intraday).** This is the **only lever that bends the rebound frontier** — passive tail sizing only moves you *along* it (cushion vs rebound trade-off, no cell dominates both). Done right it must be **slow, partial, laddered, gated on intraday data** — never the all-in early surge that was tested and failed.
3. **Dynamic-throttle layer.** Gamma / vol / skew / VRP throttling of tail distance + size and 0DTE harvest aggressiveness. The harvest side is low-risk (0DTE flat nightly); needs intraday SPXW data.
4. **Self-funding-ledger vs fixed-hedge-budget test.** Run as its own experiment (kept clean, separate from the V-bottom study): does the endogenous waterfall-ordered budget actually reduce twitchy-market bleed and improve full-cycle results vs a flat fixed hedge budget?

---

## WHAT'S OPEN (decisions / risks)

- **The remaining-window data grab.** ~3.5 weeks of paid full-OPRA access remain after the intraday SPXW finish (~06-30), and the standing principle is "grab everything now, never re-subscribe." Candidates: intraday **SPY + XSP** (S2/S3 want them alongside SPXW), **extending intraday history further back** than 2022 (check 0DTE-expiration availability pre-2022), and other condor-relevant roots. Worth scoping a "remaining-window grab plan."
- **The standing NDX re-probe BEFORE any subscription cancel.** NDX history doesn't exist at our tier (only ~the last 7 weeks); QQQ is the historical proxy. Re-probe NDX once more before cancelling the sub in case upstream coverage changed — user flagged "don't drop."
- **Whether to git-commit this session's work.** S4 + S5 code, spec, and reports are all uncommitted (S0/config/parts/paperbot untouched, zero diff). Decision pending.
- **Whether S4 is a product to run or just S5's conservative anchor.** S4 is a finished, validated conservative vol dial / bond-alt; open question whether it's run standalone or serves only as S5's conservative anchor / signal brain.

---

## How to pick up

The durable trail, in priority order:

1. **The four memory notes (auto-loaded every session):** `s4-spx-vol-control-fund`, `s5-financed-convexity-overlay`, `options-warehouse`, `worker-watch-never-leave-behind`. These are comprehensive and current — start here.
2. **`docs/S5_SPEC.md`** — the full S5 design with the §1.1 V-bottom verdict and the new §1.2 real-skew validation.
3. **The `backtester/output/*_2026*.md` reports** — the numbers behind every claim above (S4 vol-control / cost / re-entry-lag; S5 V-bottom / prototype / tail-sweep / real-skew sweep).
4. **`datacollector/STRATEGIES.md`** — the roster (S0–S5) with current status lines.

**Standing ops note:** if any background workers are outstanding, re-arm the 5-minute worker-watch sweep (one cron, cancel-before-re-arm) per `worker-watch-never-leave-behind`. Do **not** disturb the running intraday collector.
