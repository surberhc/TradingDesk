"""
sector.py — Equity Leadership / Sector Engine (SATELLITE, optional). SPEC.md §5.

Broad beta (SPY/VTI/RSP, chosen above their 200-day/10-month trend) is the equity
core. Sector tilt is an OPTIONAL small overlay (config.SECTOR_TILT_PCT, default 0;
allowable 0-30% of the equity sleeve). When on, sectors are scored on a SIMPLE
basis only — 3-month and 6-month relative strength vs SPY — behind a 200-day trend
gate. No 8-factor score. Max single sector 15%; 3-4 sectors when used.

This engine returns how the EQUITY SLEEVE is split (fractions summing to 1):
broad beta plus, optionally, the selected sector tilts. The regime/volatility
engines decide how big the equity sleeve is; this only decides its internal mix.

Correctness (SPEC §3): relative-strength and trend windows are trailing, so a
date-T selection uses only data on/before T. No look-ahead.
"""

from __future__ import annotations

import pandas as pd

from strategies import config
from strategies.parts import _gates as gates

TRADING_DAYS_PER_MONTH = 21  # units conversion (see regime.py), not a tunable


def _trailing_return(series: pd.Series, months: int, asof: pd.Timestamp) -> float:
    """Total return over the trailing N months as of `asof` (causal)."""
    window = series.loc[:asof]
    lag = months * TRADING_DAYS_PER_MONTH
    if len(window) <= lag:
        return float("nan")
    return window.iloc[-1] / window.iloc[-1 - lag] - 1.0


def _above_trend(series: pd.Series, asof: pd.Timestamp, window: int) -> bool:
    """True if price is above its trailing `window`-day MA as of `asof`."""
    hist = series.loc[:asof]
    if hist.notna().sum() < window:
        return False
    return bool(hist.iloc[-1] > hist.tail(window).mean())


def _core_weights(prices: pd.DataFrame, asof: pd.Timestamp) -> pd.Series:
    """Weight the broad-beta members above their 200d trend; SPY fallback.

    Members are weighted by ``config.EQUITY_CORE_WEIGHTS``, RENORMALIZED over whichever
    members actually pass the trend gate on this date (so dropping a member re-splits its
    weight across the survivors in proportion, not equally). A core ticker with no entry in
    that map — or the map being absent/empty — falls back to EQUAL weight, which reproduces
    the pre-2026-08-24 behaviour exactly. See config.EQUITY_CORE for why the weights are
    not equal: they preserve the old three-fund sleeve's effective cap/equal mix after VTI
    was removed as a duplicate of SPY.
    """
    if getattr(config, "SECTOR_NEUTRAL_ENABLED", False):
        # STUDY ARM: the 11-sector drifting neutral REPLACES broad beta as the sleeve core.
        # With the Phase 2 flag on, the momentum overlay tilts the tactical slice on top of it;
        # the neutral core underneath is untouched either way.
        if getattr(config, "SECTOR_MOMENTUM_ENABLED", False):
            return momentum_overlay(prices, asof)
        return neutral_weights(prices, asof)
    core = [t for t in config.EQUITY_CORE if t in prices.columns]
    above = [t for t in core if gates.is_above_asof(prices[t], asof, buffer=config.trend_margin("sector"))]
    chosen = above or (["SPY"] if "SPY" in prices.columns else core[:1])
    raw = getattr(config, "EQUITY_CORE_WEIGHTS", None) or {}
    weights = pd.Series([float(raw.get(t, 1.0 / len(chosen))) for t in chosen], index=chosen)
    total = float(weights.sum())
    if total <= 0:  # degenerate map -> fall back to equal weight rather than divide by zero
        return pd.Series(1.0 / len(chosen), index=chosen)
    return weights / total


_NEUTRAL_CACHE: dict = {}


def _neutral_history(prices: pd.DataFrame) -> "pd.Series":
    """Quarterly history of the drifting sector NEUTRAL, as {quarter_end -> weights}.

    Built from config.SECTOR_NEUTRAL_OBSERVED, a dated list of real observations of the
    50/50 cap/equal blend. Between observations the weights DRIFT FORWARD on each sector's
    realized return; on an observation date they SNAP to the observed vector. This is the
    memo section 3 quarterly-recalculation rule expressed directly.

    STRICTLY CAUSAL. A weight at T depends only on observations dated on or before T and on
    returns realized on or before T, so appending future bars — or adding a later observation
    — cannot change it. There is deliberately NO backward reconstruction: an earlier version
    de-drifted backward from a 2026 anchor and failed tests/test_no_lookahead.py, because that
    makes every historical weight a function of data that did not exist yet.

    The earliest entry's own provenance (an estimate, with disclosed error) is documented at
    config.SECTOR_NEUTRAL_OBSERVED. It is a frozen constant, not a per-date re-derivation.
    """
    obs = {pd.Timestamp(k): v for k, v in config.SECTOR_NEUTRAL_OBSERVED.items()}
    key = (tuple(sorted((str(k), tuple(sorted(v.items()))) for k, v in obs.items())),
           prices.index[0], prices.index[-1], len(prices.index))
    hit = _NEUTRAL_CACHE.get(key)
    if hit is not None:
        return hit

    idx = prices.index
    grid = [d for d in pd.date_range(idx[0], idx[-1], freq=config.SECTOR_NEUTRAL_REBUILD)]
    grid = sorted({idx[idx <= d][-1] for d in grid if (idx <= d).any()})
    # Every observation gets its own grid point so a re-anchor takes effect on its real date.
    for od in obs:
        if (idx <= od).any():
            grid = sorted(set(grid) | {idx[idx <= od][-1]})
    if not grid:
        raise ValueError("no usable quarter grid for the sector neutral")

    def _snap_for(t):
        """The latest OBSERVED vector dated on or before t, or None."""
        prior = [od for od in obs if (idx <= od).any() and idx[idx <= od][-1] <= t]
        if not prior:
            return None
        return obs[max(prior)]

    def _qret(t0, t1):
        out = {}
        for tk in secs:
            a = prices[tk].loc[:t0].iloc[-1] if (prices.index <= t0).any() else float("nan")
            b = prices[tk].loc[:t1].iloc[-1] if (prices.index <= t1).any() else float("nan")
            out[tk] = (b / a - 1.0) if (pd.notna(a) and pd.notna(b) and a > 0) else 0.0
        return pd.Series(out)

    all_tickers = sorted({t for v in obs.values() for t in v})
    secs = [t for t in all_tickers if t in prices.columns]

    hist: dict = {}
    prev_w = None
    for j, t in enumerate(grid):
        snap = _snap_for(t)
        if snap is not None and (prev_w is None or _snap_for(t) is not _snap_for(grid[j - 1])):
            w = pd.Series({k: float(snap[k]) for k in secs if k in snap})
            w = w / w.sum()
        elif prev_w is None:
            # Before the first observation there is nothing causal to say; hold the earliest
            # observed vector flat rather than invent one.
            first = obs[min(obs)]
            w = pd.Series({k: float(first[k]) for k in secs if k in first})
            w = w / w.sum()
        else:
            w = prev_w * (1.0 + _qret(grid[j - 1], t))
            w = w / w.sum()
        hist[t] = w
        prev_w = w

    out = pd.Series(hist).sort_index()
    _NEUTRAL_CACHE[key] = out
    return out


def neutral_weights(prices: pd.DataFrame, asof) -> pd.Series:
    """The drifting sector NEUTRAL in force on `asof` — the most recent quarterly rebuild
    on or before that date. Fractions over the 11 SPDR sectors, summing to 1.

    NOT trend-gated: this is the STRATEGIC core and stays fully invested by design (memo
    section 15). Deciding whether to own equity at all is the regime engine's job, and it
    sizes this sleeve from the outside.
    """
    asof = pd.Timestamp(asof)
    hist = _neutral_history(prices)
    prior = hist.index[hist.index <= asof]
    w = hist.loc[prior[-1]] if len(prior) else hist.iloc[0]
    w = w[w > 0]
    return w / w.sum()


MONTH_DAYS = TRADING_DAYS_PER_MONTH   # local alias; 21 trading days ~ 1 month


def _skip_month_return(series: "pd.Series", asof, months: int) -> float:
    """Total return over `months` ending SKIP_MONTHS ago (the classic 12-1 construction).

    The most recent month is excluded on purpose: very-short-horizon price action shows
    REVERSAL, not continuation, so including it dilutes the momentum signal. This is the one
    genuinely new element versus the refuted July study, which used un-skipped 3m/6m windows.
    """
    hist = series.loc[:asof].dropna()
    skip = config.SECTOR_SKIP_MONTHS * MONTH_DAYS
    lag = months * MONTH_DAYS
    if len(hist) <= lag + skip:
        return float("nan")
    end = hist.iloc[-1 - skip]
    start = hist.iloc[-1 - skip - lag]
    if not (pd.notna(end) and pd.notna(start)) or start <= 0:
        return float("nan")
    return end / start - 1.0


def _percentile_scores(raw: dict) -> dict:
    """Rank a {sector -> value} map into 0-100 percentile scores (best = 100).

    NaN entries score 0 rather than being dropped: a sector we cannot measure must not be
    silently treated as mid-pack, and it must still be RANKED so the eligibility cut sees a
    complete field of 11.
    """
    usable = {k: v for k, v in raw.items() if pd.notna(v)}
    out = {k: 0.0 for k in raw}
    if not usable:
        return out
    order = sorted(usable, key=lambda k: usable[k])
    n = len(order)
    for i, k in enumerate(order):
        out[k] = 100.0 if n == 1 else 100.0 * i / (n - 1)
    return out


def _trend_score(series: "pd.Series", asof) -> float:
    """Absolute-trend score (memo section 9): 100 both tests, 50 one, 0 neither.

    Test A: price above its 10-month moving average. Test B: 50-day MA above the 200-day.
    Unlike the relative metrics this is ABSOLUTE - it asks whether the sector is healthy on
    its own terms, not merely healthier than its peers, which is what stops the model from
    buying the best-performing loser in a broad bear market.
    """
    hist = series.loc[:asof].dropna()
    ma10m = config.MA_MONTHS * MONTH_DAYS
    score = 0.0
    if len(hist) >= ma10m and hist.iloc[-1] > hist.tail(ma10m).mean():
        score += 50.0
    if len(hist) >= 200 and hist.tail(50).mean() > hist.tail(200).mean():
        score += 50.0
    return score


def composite_scores(prices: "pd.DataFrame", asof) -> "pd.Series":
    """The 0-100 composite momentum score per sector (prereg section 5).

    Four metrics: 12-1 and 6-1 skip-month relative momentum, 6-month risk-adjusted momentum,
    and absolute trend. Breadth is absent by design - no constituent data exists - and its
    weight was redistributed pro rata in config, not reallocated by judgement.

    Causal: every window is trailing, so a date-T score uses only data on/before T.
    """
    asof = pd.Timestamp(asof)
    secs = [t for t in config.SECTORS if t in prices.columns]
    w = config.SECTOR_SCORE_WEIGHTS

    r12 = {t: _skip_month_return(prices[t], asof, 12) for t in secs}
    r6 = {t: _skip_month_return(prices[t], asof, 6) for t in secs}
    ra = {}
    for t in secs:
        daily = prices[t].loc[:asof].pct_change().dropna().tail(6 * MONTH_DAYS)
        vol = float(daily.std() * (252 ** 0.5)) if len(daily) > 2 else float("nan")
        ra[t] = (r6[t] / vol) if (pd.notna(r6[t]) and pd.notna(vol) and vol > 0) else float("nan")

    s12, s6, sra = _percentile_scores(r12), _percentile_scores(r6), _percentile_scores(ra)
    strend = {t: _trend_score(prices[t], asof) for t in secs}

    return pd.Series({
        t: (w["mom_12_1"] * s12[t] + w["mom_6_1"] * s6[t]
            + w["risk_adj"] * sra[t] + w["trend"] * strend[t])
        for t in secs
    }).sort_values(ascending=False)


def _above_10m(series: "pd.Series", asof) -> bool:
    hist = series.loc[:asof].dropna()
    ma = config.MA_MONTHS * MONTH_DAYS
    return bool(len(hist) >= ma and hist.iloc[-1] > hist.tail(ma).mean())


def momentum_overlay(prices: "pd.DataFrame", asof) -> "pd.Series":
    """Core + tactical overlay over the 11 sectors (memo sections 4, 12, 13, 14).

    SECTOR_CORE_PCT always sits at neutral weights - the core is PERMANENT, so no sector is
    ever fully exited. The tactical remainder goes only to sectors that are BOTH top-N by
    composite AND above their 10-month MA, split in proportion to
    ``strategic_weight x composite`` so momentum picks the winners while the strategic weight
    stops a tiny sector from ballooning. Each sector's tactical ADD is capped at the lesser of
    SECTOR_TACTICAL_MAX_ADD_PTS and SECTOR_TACTICAL_MAX_ADD_MULT x its strategic weight.

    Any tactical budget that cannot be placed - too few eligible sectors, or caps binding -
    returns PRO RATA to the core (memo section 15). It never goes to cash: whether to own
    equity at all is the regime engine's decision, not this function's.
    """
    asof = pd.Timestamp(asof)
    neutral = neutral_weights(prices, asof)
    core_pct = float(config.SECTOR_CORE_PCT)
    tactical_budget = max(0.0, 1.0 - core_pct)
    weights = neutral * core_pct

    if tactical_budget <= 1e-12:
        return weights / weights.sum()

    scores = composite_scores(prices, asof)
    ranked = [t for t in scores.index if t in neutral.index]
    top = set(ranked[: int(config.SECTOR_ELIGIBLE_TOP_N)])
    eligible = [t for t in ranked if t in top and _above_10m(prices[t], asof)]

    placed = 0.0
    if eligible:
        caps = {t: min(float(config.SECTOR_TACTICAL_MAX_ADD_PTS),
                       float(config.SECTOR_TACTICAL_MAX_ADD_MULT) * float(neutral[t]))
                for t in eligible}
        add = {t: 0.0 for t in eligible}
        remaining = tactical_budget
        # CASCADE. Allocate by strategic_weight x composite, then RE-OFFER whatever the caps
        # rejected to the eligible sectors that still have room, and repeat until either the
        # budget is placed or every eligible sector is at its cap.
        #
        # Why this differs from the memo (section 15 sends unplaced budget straight back to the
        # strategic core): with the section 14 caps binding on most eligible sectors, that rule
        # pushes tactical money PRO RATA across all eleven — including the six that just FAILED
        # the eligibility screen — and hands the largest share to the biggest strategic weight,
        # which works against the memo's own objective of reducing mega-cap dependence.
        # Measured 2026-08-21: 4.8 of 30 points were unplaceable, 2.03 of which went to
        # non-eligible sectors. Cascading keeps tactical money inside the sectors that passed
        # both tests. It raises NO cap and adds NO parameter.
        for _ in range(len(eligible) + 1):
            room = [t for t in eligible if caps[t] - add[t] > 1e-12]
            if remaining <= 1e-12 or not room:
                break
            factor = {t: float(neutral[t]) * float(scores[t]) for t in room}
            total = sum(factor.values())
            if total <= 0:
                break
            placed_this_pass = 0.0
            for t in room:
                want = remaining * factor[t] / total
                take = min(want, caps[t] - add[t])
                add[t] += take
                placed_this_pass += take
            if placed_this_pass <= 1e-15:
                break
            remaining -= placed_this_pass
        for t in eligible:
            weights[t] += add[t]
            placed += add[t]

    leftover = tactical_budget - placed
    if leftover > 1e-12:
        # Every eligible sector is at its cap. Only NOW does the residual return to the core,
        # pro rata (memo section 15). It never goes to cash — whether to own equity at all is
        # the regime engine's decision, not this function's.
        weights = weights + neutral * leftover
    return weights / weights.sum()


def select_sectors(
    prices: pd.DataFrame,
    asof,
    tilt_pct: float = config.SECTOR_TILT_PCT,
) -> pd.Series:
    """
    Build the equity-sleeve weights for a signal date (fractions summing to 1).

    With tilt_pct <= 0 (default), returns broad beta only. Otherwise allocates
    `tilt_pct` (clamped to 0-30%) of the sleeve across the top 3-4 sectors that
    (a) pass the 200-day trend gate and (b) rank highest on combined 3m+6m
    relative strength vs SPY, each capped at config.SECTOR_MAX_WEIGHT. If no
    sector passes the gate, the tilt reverts to broad beta.
    """
    asof = pd.Timestamp(asof)
    tilt_pct = max(0.0, min(tilt_pct, 0.30))
    core = _core_weights(prices, asof)

    if tilt_pct <= 0.0 or "SPY" not in prices.columns:
        return core

    spy = prices["SPY"]
    lb_short, lb_long = config.SECTOR_RS_LOOKBACKS_MONTHS
    spy_short = _trailing_return(spy, lb_short, asof)
    spy_long = _trailing_return(spy, lb_long, asof)

    # Score sectors that are trading and above the 200-day trend gate.
    scores: dict[str, float] = {}
    for sec in config.SECTORS:
        if sec not in prices.columns:
            continue
        if not _above_trend(prices[sec], asof, config.SECTOR_TREND_GATE_DAYS):
            continue
        rs_short = _trailing_return(prices[sec], lb_short, asof) - spy_short
        rs_long = _trailing_return(prices[sec], lb_long, asof) - spy_long
        if pd.isna(rs_short) or pd.isna(rs_long):
            continue
        scores[sec] = (rs_short + rs_long) / 2.0

    if not scores:
        return core  # nothing leads -> stay in broad beta

    count = config.SECTOR_COUNT_WHEN_USED[1]  # use the high end (4) when available
    ranked = sorted(scores, key=scores.get, reverse=True)[:count]
    per_sector = min(tilt_pct / len(ranked), config.SECTOR_MAX_WEIGHT)

    sleeve = (core * (1.0 - tilt_pct)).to_dict()
    for sec in ranked:
        sleeve[sec] = sleeve.get(sec, 0.0) + per_sector

    weights = pd.Series(sleeve)
    return weights / weights.sum()  # renormalize (cap may leave a small residual)
