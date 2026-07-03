"""
reentry_ladder.py — Re-entry SCALE-IN ladder OVERLAY (backtester-level, research).

A pre-registered structural experiment on S0's RE-ENTRY behavior. Like gamma_overlay
and flow_overlay, this sits on TOP of S0 inside the backtester and does NOT edit the
shared strategy brain (strategies/) or config. It is a no-op unless explicitly enabled,
so with the overlay OFF the S0 backtest is BYTE-IDENTICAL to production.

THE PRE-REGISTERED MECHANISM (ONE ladder — nothing is swept):
  * Detect a RE-ENTRY event on the frozen engine's own equity target: a transition
    from a de-risked state (engine_target ~ 0) to a positive, rising target. The engine
    target is the regime band x volatility trim, already capped by the frozen re-entry
    ladder (all_weather.on_data's `eq_target`) — the same graded exposure source Step 0
    established (clean 0.00 -> >=0.25 break).
  * For the FIRST 3 monthly rebalances after a re-entry, cap REALIZED equity exposure
    with a multiplier m stepping 1/3 -> 2/3 -> 1:  realized = m * engine_target.
    After rung 3, realized = engine_target (ladder inactive until the next re-entry).
    Realized NEVER exceeds engine_target.
  * EXITS ARE IMMEDIATE AND OVERRIDE. If engine_target falls at any rebalance (de-risk),
    follow it that same rebalance and ABORT the ladder. The ladder only ever slows
    scaling IN, never scaling OUT — crash protection is preserved by construction.

HOW THE CAP IS APPLIED (composition-preserving):
  The realized-equity multiplier m is applied by rebuilding the SAME rebalance with a
  reduced equity_target (m * engine_target) fed to the identical portfolio assembler,
  with the identical sleeve / duration / defensive / real-asset inputs. So the mix S0
  chose is untouched; only the equity/defense split shifts, exactly as if the engine
  had targeted m * engine_target this month. This makes "realized = m * engine_target"
  literal, not an approximation, and keeps the frozen engine functions untouched.

Strictly causal: the ladder state at a rebalance depends only on the sequence of engine
targets at/<= that signal date, processed in signal-date order. No look-ahead.

Also implements the PLACEBO arm (a beta-matched, flat/uniform haircut) so a ladder
benefit can be proven TIMING-specific, not merely "holds less equity on average".
"""

from __future__ import annotations

import pandas as pd

from strategies import config
from strategies.all_weather import AdaptiveAllWeather, _scaled_band
from strategies.base import MarketState, TargetWeights
from strategies.parts import duration, portfolio, real_assets, regime, sector, volatility

# A de-risked state is an engine target at (essentially) zero. The frozen engine emits
# exact 0.0 in CapitalPreservation / stage-0 ladder, so a tiny epsilon is only defensive
# against float dust; it is NOT a tunable and is not swept.
DERISK_EPS = 1e-9

# The ONE pre-registered ladder: three rungs, realized = m * engine_target.
LADDER_MULTIPLIERS = (1.0 / 3.0, 2.0 / 3.0, 1.0)


class LadderedAllWeather(AdaptiveAllWeather):
    """S0 with the pre-registered 3-rung re-entry scale-in ladder layered on.

    Overlay modes (mutually exclusive; default OFF -> plain S0, byte-identical):
      * ladder_enabled=True  : the pre-registered timing ladder.
      * placebo_haircut=<f>   : a flat multiplier m=f applied to EVERY rebalance's
                                 realized equity (spread uniformly, NOT concentrated at
                                 re-entry) — the beta-matched control.

    The base class (overlay OFF) is used unchanged for CONTROL, so control == production.
    """

    def __init__(
        self,
        *args,
        ladder_enabled: bool = False,
        placebo_haircut: float | None = None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.ladder_enabled = ladder_enabled
        self.placebo_haircut = placebo_haircut
        # Live ladder state, walked in signal-date order across on_data calls.
        self._prev_engine_target: float | None = None
        # _next_rung is the index into LADDER_MULTIPLIERS to APPLY this rebalance while
        # the ladder is active: 0->1/3, 1->2/3, 2->1. None = ladder inactive.
        self._next_rung: int | None = None
        # Per-rebalance diagnostics the base runner does not carry through (engine target
        # vs realized target, and the applied multiplier), keyed by signal date.
        self.overlay_log: dict = {}

    # -- ladder bookkeeping ---------------------------------------------------
    def _ladder_multiplier(self, engine_target: float) -> float:
        """Realized-equity multiplier m for THIS rebalance, updating ladder state.

        Rules (pre-registered):
          * Re-entry event = prior engine_target de-risked (~0) AND this one positive
            and rising -> start the ladder at rung 1 (m=1/3).
          * While the ladder is active and the engine target keeps rising (scaling in),
            advance one rung per rebalance: 1/3 -> 2/3 -> 1, then deactivate.
          * A flat month (target neither rising nor falling) HOLDS the current rung's
            cap without advancing — we only advance a rung when actually scaling in.
          * If the engine target FALLS at any point (de-risk), follow the engine (m=1)
            and ABORT the ladder immediately — exits are never slowed.
        """
        prev = self._prev_engine_target
        self._prev_engine_target = engine_target

        # First observation: no prior state, ladder cannot have started. Follow engine.
        if prev is None:
            self._next_rung = None
            return 1.0

        rising = engine_target > prev + DERISK_EPS
        falling = engine_target < prev - DERISK_EPS

        # Exit / de-risk (engine target fell): follow it, abort any active ladder.
        if falling:
            self._next_rung = None
            return 1.0

        # Ladder already active (mid re-entry).
        if self._next_rung is not None:
            if rising:
                # Scaled in this month -> apply THIS rung's cap, then advance (or finish).
                m = LADDER_MULTIPLIERS[self._next_rung]
                self._next_rung = self._next_rung + 1
                if self._next_rung >= len(LADDER_MULTIPLIERS):
                    self._next_rung = None  # ladder complete
                return m
            # Flat (neither rising nor falling): hold the LAST-applied rung's cap and do
            # not advance — we did not scale in this month. _next_rung points at the next
            # rung to apply, so the last applied one is _next_rung - 1.
            return LADDER_MULTIPLIERS[self._next_rung - 1]

        # Re-entry event: rising OUT of a de-risked (~0) state -> arm the ladder at
        # rung 1 (apply 1/3 now, next rebalance applies 2/3).
        if prev <= DERISK_EPS and engine_target > DERISK_EPS and rising:
            self._next_rung = 1  # rung 1's 1/3 applied now; next call applies index 1
            return LADDER_MULTIPLIERS[0]

        # Not in a re-entry (e.g. fully invested, or rising but not out of de-risk):
        # follow the engine unchanged.
        return 1.0

    # -- decision -------------------------------------------------------------
    def on_data(self, state: MarketState) -> TargetWeights:
        """Assemble target weights, applying the realized-equity cap for this rebalance.

        Mirrors AdaptiveAllWeather.on_data exactly, then (only when the overlay is on)
        substitutes a reduced equity_target = m * engine_target into the SAME portfolio
        assembler with the SAME sleeve/duration/defensive/real inputs.
        """
        if self._signals is None:
            raise RuntimeError("call warmup() before on_data()")
        s = self._signals
        t = state.as_of
        prev_weights = state.positions
        version = self.version

        reg = s["confirmed_regime"].loc[t]
        band = _scaled_band(reg, version)
        engine_target = min(
            volatility.equity_target(band, s["realized"].loc[t], version),
            float(s["ladder_cap"].loc[t]),
        )

        # --- the overlay: choose the realized equity multiplier m ---
        if self.ladder_enabled:
            m = self._ladder_multiplier(engine_target)
        elif self.placebo_haircut is not None:
            m = self.placebo_haircut  # flat, every rebalance (beta-matched control)
        else:
            m = 1.0  # overlay OFF -> byte-identical to production

        eq_target = m * engine_target
        # Realized must NEVER exceed engine_target (m in [0,1] guarantees this).
        eq_target = min(eq_target, engine_target)
        self.overlay_log[t] = {
            "engine_equity_target": engine_target,
            "realized_equity_target": eq_target,
            "ladder_multiplier": m,
        }

        sleeve = sector.select_sectors(s["prices"], t, config.SECTOR_TILT_PCT)
        ddec = duration.duration_decision(s["dur_signals"].loc[t], reg)
        def_rank = s["def_scores"].loc[t].dropna().sort_values(ascending=False)
        real = real_assets.select_real_basket(s["prices"], t) if self.use_real_assets else None

        built = portfolio.build_target_weights(
            regime=reg,
            equity_target=eq_target,
            equity_sleeve=sleeve,
            duration_decision=ddec,
            defensive_ranking=def_rank,
            real_basket=real,
            version=version,
            prev_weights=prev_weights,
        )
        return TargetWeights(
            weights=built["weights"],
            as_of=t,
            sleeves=built["sleeves"],
            real_asset=built["real_asset"],
            reasons=built["reasons"],
            extras={
                "regime": reg,
                "equity_target": eq_target,
                "engine_equity_target": engine_target,
                "ladder_multiplier": m,
                "score": float(s["score_df"].loc[t, "score"]),
                "ladder_stage": int(s["ladder_stage"].loc[t]),
            },
        )


def run_laddered_backtest(
    prices,
    yield_10y,
    hyg,
    vix,
    hy_oas,
    *,
    start: str,
    version: str = "Balanced",
    mode: str = "control",
    placebo_haircut: float | None = None,
):
    """Run the S0 backtest with the ladder overlay swapped in via LadderedAllWeather.

    mode:
      * "control" : overlay OFF (LadderedAllWeather with both flags off == AdaptiveAllWeather).
      * "ladder"  : the pre-registered 3-rung timing ladder.
      * "placebo" : a flat placebo_haircut multiplier on every rebalance.

    Implemented by monkeypatching backtest.AdaptiveAllWeather to our subclass in-process
    and restoring it in a finally — the SAME technique _sharp_recovery_test.py uses. The
    canonical run_backtest is otherwise untouched.
    """
    from src import backtest

    ladder_enabled = mode == "ladder"
    haircut = placebo_haircut if mode == "placebo" else None

    made: list = []  # capture the instance the runner builds so we can read its log

    def _factory(*a, **k):
        obj = LadderedAllWeather(
            *a, ladder_enabled=ladder_enabled, placebo_haircut=haircut, **k
        )
        made.append(obj)
        return obj

    orig = backtest.AdaptiveAllWeather
    try:
        backtest.AdaptiveAllWeather = _factory
        result = backtest.run_backtest(prices, yield_10y, hyg, vix, hy_oas,
                                       start=start, version=version)
    finally:
        backtest.AdaptiveAllWeather = orig

    # Fold the overlay's per-rebalance diagnostics (engine vs realized target, m) into
    # the monthly frame so downstream code can read them. Byte-identity of NAV/weights
    # is unaffected — these are added columns only.
    if made and made[0].overlay_log:
        log = pd.DataFrame(made[0].overlay_log).T
        log.index.name = "signal_date"
        monthly = result["monthly"]
        for col in ("engine_equity_target", "realized_equity_target", "ladder_multiplier"):
            result["monthly"][col] = log[col].reindex(monthly.index)
    return result


def average_under_exposure(prices, yield_10y, hyg, vix, hy_oas, *, start, version="Balanced"):
    """The LADDER's average realized under-exposure vs the engine target, as a fraction.

    Returns the ratio r = mean(realized_equity) / mean(engine_equity) over all
    rebalances where the engine target is positive — the beta-matched multiplier the
    PLACEBO applies flat/uniformly. Computed from the ladder run's own extras so the
    placebo removes EXACTLY the same average equity the ladder did, just spread evenly.
    """
    r = run_laddered_backtest(prices, yield_10y, hyg, vix, hy_oas,
                              start=start, version=version, mode="ladder")
    m = r["monthly"]
    eng = m["engine_equity_target"]
    real = m["realized_equity_target"]
    pos = eng > DERISK_EPS
    if not pos.any():
        return 1.0
    return float(real[pos].sum() / eng[pos].sum())
