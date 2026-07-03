# Re-entry scale-in ladder — pre-registered structural experiment (2026-07-03)

**Verdict: FAIL.** The pre-registered 3-rung re-entry ladder does not clear the bar. It
costs CAGR with no drawdown offset, makes the two target sideways/whipsaw episodes (2011,
2015-16) *worse* rather than better, and — decisively — **loses to a beta-matched placebo
on 5 of 6 episodes**, which proves any effect is a beta artifact, not re-entry timing.
Per the anti-curve-fit rule (the REENTRY_MAX_LAG 6→3 lesson), it is reported as an honest
failure and **NOT tuned to pass**.

---

## What was built

All research-only; the shared brain (`strategies/`) and `config.py` are untouched.

| File | Role |
|------|------|
| `backtester/src/reentry_ladder.py` | The overlay. `LadderedAllWeather(AdaptiveAllWeather)` overrides `on_data` to cap realized equity = `m × engine_target`. Default OFF (control) ⇒ byte-identical to production. Also the flat placebo arm + `run_laddered_backtest(mode=...)`. |
| `backtester/tests/test_reentry_ladder.py` | 8 tests: the ladder state-machine mechanics, exit-override, realized≤engine, and the **OFF-parity safety net** (control == production exactly). |
| `backtester/reentry_ladder_experiment.py` | Control / ladder / placebo evaluation over the established episodes + OOS split. Writes `output/_reentry_ladder_results.json`. |

**Mechanism (exactly as pre-registered, nothing swept).** A re-entry event = the frozen
engine's own equity target (`all_weather.on_data`'s `eq_target` — regime band × vol trim,
capped by the frozen re-entry ladder) rising out of a de-risked (~0) state. For the first
3 monthly rebalances after a re-entry, realized = `m × engine_target` with `m` stepping
**1/3 → 2/3 → 1**; after rung 3 the ladder is inactive until the next re-entry. Realized
never exceeds `engine_target`. **Exits override:** if the engine target falls at any
rebalance, follow it that same month and abort the ladder (scaling OUT is never slowed —
crash protection is preserved by construction). The cap is applied by rebuilding the same
rebalance with a reduced `equity_target` through the identical portfolio assembler and the
identical sleeve/duration/defensive/real inputs, so composition is unchanged and
"realized = m × engine_target" is literal.

The ladder fired at **20 rebalances** across all the expected re-entries (2009, 2010, 2012,
2016, 2019, 2020, 2022) with the correct 1/3 → 2/3 → 1 progression — it is genuinely
active, not a silent no-op.

---

## OFF-parity (the safety net) — PASS

With the overlay OFF (`mode="control"`), the backtest is **byte-identical** to production
(`backtest.run_backtest` with the stock `AdaptiveAllWeather`):
`test_off_parity_byte_identical_to_production` asserts exact equality of NAV, daily
returns, executed target weights, and the regime/score/equity-target/ladder-stage paths.
Passes.

---

## Headline (full sample, 2007 → present, Balanced)

| metric | CONTROL | LADDER | PLACEBO |
|--------|--------:|-------:|--------:|
| CAGR | 7.81% | **7.50%** | 7.68% |
| Max drawdown | −9.66% | −9.66% | −9.29% |
| Calmar | 0.808 | 0.776 | 0.826 |
| Sortino | 1.156 | 1.118 | 1.163 |
| Annual vol | 7.82% | 7.71% | 7.62% |

The ladder gives back 31 bp of CAGR and **buys no drawdown protection** (headline maxDD
identical — de-risk timing is unchanged, only scale-in is slowed). The placebo — the same
average equity removed but spread flat — actually *improves* maxDD and keeps more CAGR.

## Per-episode (return / maxDD; Δ LADDER vs CONTROL, bp)

| episode | C ret | L ret | Δret bp | C maxDD | L maxDD | ΔmaxDD bp |
|---------|------:|------:|--------:|--------:|--------:|----------:|
| GFC 2008-09 | 23.3% | 21.8% | −152 | −8.8% | −7.9% | +87 |
| 2011 euro | 8.2% | 6.9% | **−129** | −4.7% | −4.7% | −0 |
| 2015-16 grind | 2.4% | 2.0% | **−37** | −7.4% | −7.7% | −31 |
| 2018-Q4 | −0.9% | −0.7% | +24 | −8.6% | −8.6% | 0 |
| COVID 2020 | 5.7% | 5.2% | −50 | −9.7% | −9.7% | 0 |
| 2022 bear | 7.2% | 7.5% | +31 | −6.9% | −5.8% | +104 |

**The pre-registered hypothesis is refuted on its own target cases.** The ladder was
supposed to *help* the sideways/whipsaw episodes (2011, 2015-16); instead it degrades both
(2011 −129 bp return; 2015-16 −37 bp return **and −31 bp deeper drawdown**). Crash drawdowns
are preserved as designed (2008/2020/2022 all flat-or-better on maxDD — exits are
untouched), so it does no harm there, but the whole point was the grinds, and it hurts them.
The two episodes where the ladder helps returns (2018-Q4 +24, 2022 +31) are the ones where
holding *less* equity into a still-falling tape helped — i.e. a beta/de-risk effect, not
timed re-entry — which the placebo captures for free.

## Placebo check (per-episode return: LADDER vs a flat beta-matched haircut)

The placebo is a flat multiplier of **0.9665** (the ladder's own average realized/engine
equity ratio) applied to *every* rebalance — same average equity removed, spread uniformly
instead of concentrated at re-entry.

| episode | LADDER ret | PLACEBO ret | LADDER − PLACEBO bp |
|---------|-----------:|------------:|--------------------:|
| GFC 2008-09 | 21.8% | 22.7% | **−95** |
| 2011 euro | 6.9% | 8.3% | **−137** |
| 2015-16 grind | 2.0% | 2.5% | **−50** |
| 2018-Q4 | −0.7% | −0.8% | +9 |
| COVID 2020 | 5.2% | 6.0% | **−78** |
| 2022 bear | 7.5% | 7.1% | +44 |

**Decisive kill: the ladder loses to the placebo on 5 of 6 episodes**, including both target
grinds (2011, 2015-16). Concentrating the same equity reduction at re-entry is *worse* than
smearing it flat. The timing has no value; if anything it is anti-informative.

## Out-of-sample (walk-forward split @ 2019-12-31)

| half | CONTROL CAGR | LADDER CAGR | PLACEBO CAGR | CONTROL maxDD | LADDER maxDD | PLACEBO maxDD |
|------|------------:|-----------:|------------:|-------------:|------------:|-------------:|
| TRAIN (≤2019) | 7.51% | 7.18% | 7.37% | −8.82% | −8.82% | −8.67% |
| TEST (>2019) | 8.39% | 8.13% | 8.30% | −9.66% | −9.66% | −9.29% |

The CAGR cost and the zero-drawdown-benefit hold in **both** halves — this is not a
train-only artifact. The ladder underperforms control and placebo in-sample and
out-of-sample alike.

## 2008 cold-start caveat

GFC 2008-09 is the first episode with no prior de-risk/re-entry state; the 2009 re-entry
firing is genuine but sits at the very start of history, so its point estimate is the
least reliable of the six. It is reported around, not fixed. It does not change the verdict:
the ladder fails independently on 2011 and 2015-16, which are mid-sample and well-conditioned.

---

## Honest verdict

**The pre-registered ladder FAILS the gate — at every point that matters:**

1. It **degrades the two target sideways/whipsaw episodes** (2011, 2015-16) it was built to
   help — the opposite of the hypothesis.
2. It **loses to a beta-matched placebo on 5 of 6 episodes**, so whatever small help appears
   (2018-Q4, 2022) is a *beta artifact* — "holds less equity into a falling tape" — not
   re-entry timing. Smearing the same haircut flat is strictly better.
3. It **costs CAGR with no drawdown offset**, full-sample and in both OOS halves.

Economically this is unsurprising: S0 already de-risks fast and rebuilds through the frozen
staged re-entry ladder (25/50/75/100% caps) plus the MAX-LAG V-recovery override. Adding a
second, slower scale-in on top only strands the book further below a target that was already
conservative — it double-counts the very caution the engine encodes, and it does so
*symmetrically* whether the recovery is a clean V or a grind, which is why the placebo (no
timing) wins.

Per project rule #1 and the REENTRY_MAX_LAG 6→3 precedent, this is left as a recorded
negative result. Nothing in `config.py` or the shared brain is changed; the overlay ships
OFF (byte-identical) and is not tuned to manufacture a pass.

---

## Verification

- New tests: `tests/test_reentry_ladder.py` — **8 passed** (incl. OFF-parity byte-identity).
- Full backtester suite: **199 passed** (191 prior + 8 new), `pytest -q`.
- Causality guard: `tests/test_no_lookahead.py` — **2 passed**.
- Machine-readable results: `output/_reentry_ladder_results.json`.
