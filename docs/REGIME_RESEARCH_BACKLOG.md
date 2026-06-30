# Regime engine — research backlog

Durable list of open/parked ideas for the S0 regime engine. Nothing here is adopted; the
production config is **frozen** (no tuning without an explicit blessing — project rule #1,
no curve-fit). Each lead carries its curve-fit risk and the bar it must clear.

Context that governs all of these (from the 2026-06-28/29 regime exploration): the crisis
**exits are good — leave them alone**; the real bleed is **re-entry lag + shallow-dip
whipsaws**; the existing config knobs are a **robust plateau** (a dead end — a real fix is
**structural**); and at the de-risk moment a noise-dip and a real-crash first leg look the
**same** across depth, gamma, vol, and term structure (so no exit overlay can separate them).
Any structural change is core surgery — gate with the full battery: pre-registered hypothesis
+ success metric, robustness plateau (not one magic threshold), walk-forward OOS, and preserve
the 2008 / 2020 / 2022 episode drawdowns.

---

## OPEN LEADS (untested or active)

### 1. Breadth-thrust as the re-engagement (re-entry) trigger  — NEW, high curve-fit risk
**Source:** "Navigating Lost Decades" (see [docs/reference/README.md](reference/README.md)) —
its core mechanical claim is that **market breadth leads price**, and it names *re-engagement
after a crash* as the hard problem (the same re-entry lag our own diagnostics flag as the #1
bleed).

**Idea:** use a **breadth thrust** (a sudden surge in market participation — e.g. % of stocks
above their MA, or advance/decline momentum crossing a threshold) as a **forward-looking
re-entry signal** off a bottom, instead of (or alongside) the fixed `REENTRY_MAX_LAG_MONTHS`
timeout. The hope: re-risk *earlier* than a fixed timeout in genuine recoveries, **without**
the bear-rally trap.

**Why it's on-point:** re-entry lag is the documented #1 bleed (2022 sat out +29.6% over 9mo;
2015-16 ~23mo rebuild). The `MAX_LAG` knob is only a risk-budget trade-off (held, not a free
win). A real *signal* for the bottom turn is the missing structural piece.

**The bar it must clear (this is the whole ballgame):**
- It must **distinguish a true bottom from a bear-market rally.** This is exactly where the
  "dumb price-recovery" re-entry flag FAILED (it re-risked into the Mar-2022 bear rally,
  worsening 2008 +65bp / 2022 +224bp). Breadth-thrust has to prove it doesn't do the same.
- It must add re-entry **speed the free `MAX_LAG` knob didn't** — our Stage-B finding was that
  2022's lag was "a timeout, not a signal, problem," so breadth-thrust must earn its keep on
  cases **beyond** 2022, OOS.
- Pre-registered rules + metric; robustness plateau; preserve 2008/2020/2022 DD; beat BOTH
  plain S0 and the held `MAX_LAG=3` trade-off.

**Caveats / unknowns:** breadth data sourcing + availability across 2007–2026 needs checking;
adds a new *dimension* to the regime engine (core surgery, own validation); HIGH curve-fit risk
(the paper's own caveat). **Status: unvetted lead, no code. Revisit further down the road.**
Related memory: `regime-engine-tuning`, `intraday-gamma-early-exit`.

### 2. Exit "option 2" — gradual / laddered de-risk cuts
Since **no signal** can predict whether a dip becomes a crash, don't try to avoid the exit —
make it **less costly when wrong**: cap per-step exit depth / ladder the de-risk down so a
whipsaw round-trip bleeds less. Deeper regime-band surgery, **not yet tested**. Tradeoff risk:
gradual exits may dull real-crash protection — gate by preserving 2008/2020/2022 DD. From
`regime-engine-tuning` (the remaining untested structural lead).

### 3. Intraday-gamma early-exit / faster re-entry overlay  — data-gated, revisit now
Andrew's idea: sample dealer gamma at 1-min resolution as an early-warning overlay on the
monthly core for a more timely exit and faster re-entry. Gated on the SPXW 1-min data (landing
~2026-06-30). Legitimate hypothesis, **HIGH curve-fit risk** — a daily gamma overlay was
already tested→rejected, and a faster signal usually *increases* whipsaw; only ~3–4 yrs of
1-min history (no 2008/2022). Start at **alert-only**, climb the invasiveness dial only if
evidence earns it. Full kill-criteria in memory `intraday-gamma-early-exit`.

---

## PARKED / BANKED NEGATIVES (don't re-litigate without a new angle)

- **`sharp_recovery` clean-V refinement — CLOSED 2026-06-29.** A principled clean-V filter was
  built and worked as designed (2015-16 whipsaw −150bp→0bp) but still fails the per-episode gate
  at GFC 2008-09 (−118bp), and that failure is **filter-independent** (nothing to overfit to).
  At production `MAX_LAG=6` the override fires exactly once in 20yrs, so the "fires in sideways
  grinds" premise is false for the current config. Banked negative. Evidence:
  `backtester/output/regime_sharp_recovery_test_20260629.md`.
- **`REENTRY_MAX_LAG_MONTHS` 6→3 — HELD (not adopted).** A risk-budget trade-off, not a free
  win: wins 2009/2011 episode-NAV but worsens the 2015-16 sideways grind −152bp and fails the
  hard per-episode gate. Stays at **6**.
- **Exit overlays (drawdown-depth gate, gamma, realized vol, VIX/VIX3M term structure) —
  REJECTED.** None can separate a whipsaw from a real-crash first leg at the de-risk moment.
  Curve-fit-PREVENTING result, not a to-do.
