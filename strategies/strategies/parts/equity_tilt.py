"""
equity_tilt.py — STUDY-ONLY equity-sleeve broadening tilt.

Pre-registration: docs/PREREG_S0_equity_sleeve_broadening_2026-07-20.md.

This is a research overlay on the equity sleeve's INTERNAL composition, default
OFF (config.EQUITY_TILT_ENABLED). When off, `select_equity_sleeve` is a pure
pass-through to the production sector engine (`sector.select_sectors` on the
frozen `config.SECTOR_TILT_PCT`), so S0 behaves byte-for-byte as it does today.

When ON, a static `config.EQUITY_TILT_PCT` fraction of the equity sleeve is
carved out for the strongest momentum-leaders among a broadened candidate set —
the two size funds (IJH/IJR by default, VO/VB behind the swap flag) PLUS the 11
SPDR sectors — gated on the SAME simple basis the sector engine already uses
(SPEC §5): a candidate is eligible only if it is (a) above its 200-day trend AND
(b) beating SPY on relative strength (score = mean(RS_3m, RS_6m), RS_k =
ret(asset,k) - ret(SPY,k) > 0). The top N by score are held, each weighted
min(TILT_PCT / N, SECTOR_MAX_WEIGHT); any unfilled tilt budget (too few eligible)
falls back to broad beta. The remaining sleeve stays broad beta (SPY/VTI/RSP)
exactly as production.

Reuses the sector engine's own gate helpers (`_above_trend`, `_trailing_return`,
`_core_weights`) — it invents NO new indicator. Causality: every window is
trailing, so a date-T selection uses only data on/before T (SPEC §16). Selection
is asserted causal in the tests.
"""

from __future__ import annotations

import pandas as pd

from strategies import config
from strategies.parts import sector


def tilt_candidates() -> list[str]:
    """The study candidate universe: the two active size funds + the 11 sectors.

    Order is deterministic (size funds first, then config.SECTORS) so a score tie
    breaks the same way every run. The size pair is IJH/IJR by default, VO/VB when
    the pre-registered swap flag (EQUITY_TILT_USE_ALT_SIZE) is set."""
    size = (config.EQUITY_TILT_SIZE_FUNDS_ALT if config.EQUITY_TILT_USE_ALT_SIZE
            else config.EQUITY_TILT_SIZE_FUNDS)
    return list(size) + list(config.SECTORS)


def _score(prices: pd.DataFrame, ticker: str, asof: pd.Timestamp,
           spy_short: float, spy_long: float) -> float | None:
    """RS score = mean(RS_3m, RS_6m) vs SPY, or None if not computable/NaN.

    RS_k = trailing-k-month return(ticker) - trailing-k-month return(SPY). The
    same simple basis (and the same helper) the sector engine uses — no new score.
    """
    lb_short, lb_long = config.SECTOR_RS_LOOKBACKS_MONTHS
    rs_short = sector._trailing_return(prices[ticker], lb_short, asof) - spy_short
    rs_long = sector._trailing_return(prices[ticker], lb_long, asof) - spy_long
    if pd.isna(rs_short) or pd.isna(rs_long):
        return None
    return (rs_short + rs_long) / 2.0


def select_equity_sleeve(prices: pd.DataFrame, asof) -> pd.Series:
    """Equity-sleeve weights (fractions summing to 1) for a signal date.

    STUDY OFF (default) -> exactly the production path:
        sector.select_sectors(prices, asof, config.SECTOR_TILT_PCT)
    so the byte-for-byte production behaviour is preserved.

    STUDY ON -> broad beta for (1 - filled) of the sleeve, plus the momentum-gated
    tilt for the filled portion (see module docstring). If nothing is eligible, or
    tilt_pct <= 0, or SPY is absent, it degrades to the production path.
    """
    if not config.EQUITY_TILT_ENABLED:
        return sector.select_sectors(prices, asof, config.SECTOR_TILT_PCT)

    asof = pd.Timestamp(asof)
    tilt_pct = max(0.0, min(float(config.EQUITY_TILT_PCT), 0.30))
    core = sector._core_weights(prices, asof)

    if tilt_pct <= 0.0 or "SPY" not in prices.columns:
        return core

    spy = prices["SPY"]
    lb_short, lb_long = config.SECTOR_RS_LOOKBACKS_MONTHS
    spy_short = sector._trailing_return(spy, lb_short, asof)
    spy_long = sector._trailing_return(spy, lb_long, asof)

    # Eligible = above the 200d trend gate AND positive RS vs SPY (beating the index).
    scores: dict[str, float] = {}
    for cand in tilt_candidates():
        if cand not in prices.columns:
            continue
        if not sector._above_trend(prices[cand], asof, config.SECTOR_TREND_GATE_DAYS):
            continue
        sc = _score(prices, cand, asof, spy_short, spy_long)
        if sc is None or sc <= 0.0:      # must BEAT SPY on RS (prereg §2/§5)
            continue
        scores[cand] = sc

    if not scores:
        return core  # nothing leads -> stay fully in broad beta

    n = int(config.EQUITY_TILT_COUNT)
    ranked = sorted(scores, key=lambda t: (scores[t], t), reverse=True)[:n]
    per_asset = min(tilt_pct / n, config.SECTOR_MAX_WEIGHT)
    filled = per_asset * len(ranked)     # unfilled (n>eligible, or cap) reverts to beta

    # Broad beta keeps (1 - filled): the unfilled tilt budget falls back to beta,
    # so the sleeve sums to exactly 1 with no renormalization needed.
    sleeve = (core * (1.0 - filled)).to_dict()
    for cand in ranked:
        sleeve[cand] = sleeve.get(cand, 0.0) + per_asset
    return pd.Series(sleeve)
