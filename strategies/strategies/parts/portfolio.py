"""
portfolio.py — Combine engine outputs into target weights for one rebalance.

Build to SPEC.md §11 (order of operations) and §12 (caps/floors). This is a PURE
assembly function: it takes already-computed engine outputs (regime, equity
target, equity-sleeve mix, duration decision, defensive ranking, real-asset pick)
and produces final target weights + reason codes. It does no data access and no
look-ahead reasoning itself — the backtest computes the (causal) engine signals
and feeds them here. Keeping it pure makes it directly unit-testable.

Order of operations (SPEC §11):
  1-2. equity target (regime band x volatility trim) — done by caller, passed in.
  3.   equity holdings (broad beta + optional sector tilt) — passed in as a sleeve.
  4.   duration permission/caps — passed in as a duration decision.
  5.   rank defensive + real-asset candidates, fill the defense sleeve (T-bills
       default), respecting per-bucket duration caps (% of TOTAL portfolio).
  6.   apply T-bill minimums (regime floor and version floor) and the §12 caps.
  7.   whipsaw control: incumbents get a config.RANK_REPLACEMENT_THRESHOLD bonus
       so a holding is only displaced by a decisively better challenger
       (current-holding tie-break).
  8.   emit weights (sum to 1) + reason codes.
"""

from __future__ import annotations

from collections import defaultdict

import pandas as pd

from strategies import config

# Which duration cap bucket each defensive ticker draws from (SPEC §6).
# T-bill bucket is the cash-like sleeve: T-bills + floating-rate (ultra-short).
_BUCKET_OF: dict[str, str] = {
    **{t: "tbill" for t in config.TBILLS + config.FLOATING_RATE},
    **{t: "short" for t in config.SHORT_TREASURIES},
    **{t: "intermediate" for t in config.INTERMEDIATE_TREASURIES},
    **{t: "long" for t in config.LONG_TREASURIES},
}


def _best_tbill(defensive_ranking: pd.Series) -> str:
    """Highest-scoring cash-like ticker; the benchmark T-bill as a safe default."""
    cash = [t for t in defensive_ranking.index if _BUCKET_OF.get(t) == "tbill"]
    if cash:
        return defensive_ranking[cash].idxmax()
    return config.BENCHMARK_TBILL


def build_target_weights(
    *,
    regime: str,
    equity_target: float,
    equity_sleeve: pd.Series,
    duration_decision: dict,
    defensive_ranking: pd.Series,
    real_basket: dict | None = None,
    version: str = config.ACTIVE_VERSION,
    prev_weights: pd.Series | None = None,
) -> dict:
    """
    Assemble final target weights + reason codes for one rebalance date (SPEC §11).

    Returns a dict with:
      weights        : Series ticker -> weight (sums to 1.0)
      regime, equity_target
      sleeves        : {"equity", "defense", "real_asset"} fractions
      real_asset     : the held hedge ticker or None
      reasons        : human-readable reason codes for the month
    """
    weights: dict[str, float] = defaultdict(float)
    reasons: list[str] = [f"regime={regime}", f"equity_target={equity_target:.0%}"]

    macro = duration_decision.get("macro_regime", "neutral")

    # --- Risk-budget rotation (experimental): in inflation/stagflation, let real
    # assets SUBSTITUTE for some equity (rotate within the risk budget toward what's
    # trending). Equity down by `rotated` = real-asset budget up by the same amount,
    # so total risk-asset exposure and the defense sleeve are unchanged. Gated on real
    # assets actually trending (basket present). Off until validated. ---
    rotated = 0.0
    if config.EQUITY_ROTATION_ENABLED and real_basket is not None:
        rot_frac = config.REAL_ASSET_EQUITY_ROTATION.get(macro, 0.0)
        rotated = rot_frac * equity_target
    equity_effective = equity_target - rotated
    if rotated > 0:
        reasons.append(f"rotated {rotated:.0%} equity -> real assets ({macro})")

    # --- 3. Equity sleeve (broad beta + optional sector tilt) ---
    for ticker, frac in equity_sleeve.items():
        weights[ticker] += frac * equity_effective

    defense_budget = max(0.0, 1.0 - equity_effective)
    remaining = defense_budget
    caps = duration_decision["caps"]
    bucket_used: dict[str, float] = defaultdict(float)
    reasons.extend(duration_decision.get("reasons", []))

    best_tbill = _best_tbill(defensive_ranking)

    # --- 6 (floor first). T-bill minimum: max of regime floor and version floor ---
    version_floor = config.CLIENT_VERSIONS.get(version, {}).get("tbill_floor", 0.0)
    tbill_floor = min(remaining, max(caps["tbill"][0], version_floor))
    if tbill_floor > 0:
        weights[best_tbill] += tbill_floor
        bucket_used["tbill"] += tbill_floor
        remaining -= tbill_floor
        reasons.append(f"T-bill floor {tbill_floor:.0%} -> {best_tbill}")

    # --- Real-asset sleeve (its OWN leg, not the defense budget): a deliberate,
    # version-scaled target spread across a DIVERSIFIED basket (gold + commodities),
    # inverse-vol weighted, taken only when a leg passed the trend gate (SPEC §6).
    # The §12 category caps are per-leg ceilings; anything NOT used here flows on to
    # the genuine defensive sleeve (T-bills/Treasuries) below.
    real_alloc = 0.0
    real_tickers: list[str] = []
    if real_basket is not None and remaining > 1e-9:
        # Dynamic cap: scale the version target by the detected macro regime (lean
        # into real assets in inflation/stagflation, dial down in deflation), then
        # add any equity rotated in, and clamp to the hard ceiling. The (larger)
        # defense budget from the reduced equity funds the rotated portion.
        scale = config.REAL_ASSET_REGIME_SCALE.get(macro, 1.0)
        base = max(config.REAL_ASSET_STRATEGIC_FLOOR,
                   config.REAL_ASSET_SLEEVE_TARGET.get(version, 0.15))
        sleeve_target = min(base * scale + rotated, config.REAL_ASSET_SLEEVE_MAX, remaining)
        if macro != "neutral":
            reasons.append(f"real-asset cap {macro} x{scale:g} -> {sleeve_target:.0%}")
        for leg in real_basket["legs"]:
            leg_alloc = min(sleeve_target * leg["weight"], leg["cap"])
            if leg_alloc > 1e-12:
                weights[leg["ticker"]] += leg_alloc
                real_alloc += leg_alloc
                real_tickers.append(leg["ticker"])
        remaining -= real_alloc
        if real_tickers:
            reasons.append(
                f"real-asset sleeve {real_alloc:.0%} -> {'+'.join(real_tickers)} "
                f"(inverse-vol basket, target {sleeve_target:.0%})"
            )

    # --- 7. Whipsaw: give incumbents a replacement-threshold bonus ---
    ranking = defensive_ranking.astype(float).copy()
    if prev_weights is not None:
        held = prev_weights[prev_weights > 0].index
        ranking.loc[ranking.index.isin(held)] += config.RANK_REPLACEMENT_THRESHOLD
    ranking = ranking.sort_values(ascending=False)

    # --- 5/6. Fill the rest of the defense sleeve by rank, honoring bucket caps ---
    for ticker in ranking.index:
        if remaining <= 1e-9:
            break
        bucket = _BUCKET_OF.get(ticker)
        if bucket is None:
            continue
        room = caps[bucket][1] - bucket_used[bucket]  # cap is % of TOTAL portfolio
        if room <= 1e-9:
            continue
        take = min(remaining, room)
        weights[ticker] += take
        bucket_used[bucket] += take
        remaining -= take

    # Anything still unplaced (all caps exhausted) parks in cash — the ultimate
    # fallback (SPEC §7). Rare, and only when bucket caps bind tightly.
    if remaining > 1e-9:
        weights[best_tbill] += remaining
        reasons.append(f"residual {remaining:.0%} parked in cash ({best_tbill})")
        remaining = 0.0

    series = pd.Series(weights, dtype=float)
    series = series[series > 1e-9]
    total = series.sum()
    if total > 0:
        series = series / total  # normalize away any float dust

    real_frac = real_alloc / total if (real_tickers and total > 0) else 0.0
    return {
        "weights": series.sort_values(ascending=False),
        "regime": regime,
        "equity_target": equity_target,
        "sleeves": {
            "equity": float(equity_effective),
            "defense": float(defense_budget - real_alloc),
            "real_asset": float(real_frac),
        },
        "real_asset": "+".join(real_tickers) if real_tickers else None,
        "reasons": reasons,
    }
