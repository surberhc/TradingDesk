"""
base_detector.py — a DETERMINISTIC O'Neil base/pivot detector for the CAN SLIM replica.

Encodes the PUBLISHED base geometry from canslim/research/canslim_oneil_spec.md (§3) as
code: prior-uptrend precondition, per-pattern depth & duration bounds, the pivot =
pattern-resistance-high + $0.10, handle rules, and volume dry-up where volume is present.

Patterns implemented: cup-with-handle, flat base, double-bottom ("W"), and a generic
consolidation base (square/box). These are WEEKLY-CHART patterns, so detection runs on
WEEKLY bars (daily resampled to W-FRI); the pivot and volume dry-up are refined on daily
bars.

HARD CAUSALITY RULE (the desk's no-lookahead guard): given an as-of date, only bars on or
before the as-of week may inform detection. detect_base() slices the frame at as_of BEFORE
doing anything else; nothing downstream can see a future bar.

DO NOT tune these bounds to fit the advisor's picks. Every numeric bound below is cited to
a line/section of the spec. Where the advisor's effective tolerance differs, that is a
FINDING for the validation report — not a reason to move a bound.

Public API
----------
    detect_base(daily_df, as_of, *, symbol=None) -> BaseResult
        daily_df: DataFrame indexed by DatetimeIndex (daily), columns open/high/low/close,
                  optional volume. Unadjusted-for-dividends but split-adjusted price (chart
                  price), matching how the advisor reads pivots.
        as_of:    date/Timestamp — the week he flagged/bought. Detection uses bars <= as_of.

    BaseResult fields: found(bool), pattern(str|None), base_start, base_end, pivot(float|None),
        depth_pct, duration_weeks, prior_uptrend_pct, volume_dryup(bool|None), notes, and
        candidates (all patterns considered, for failure analysis).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional
import datetime as dt

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Published O'Neil bounds (canslim_oneil_spec.md §3). Cited, NOT fitted.
# ---------------------------------------------------------------------------
PIVOT_OFFSET = 0.10          # §3: pivot = resistance high + $0.10 (ten cents)

# General base preconditions (§3 "General rules"):
PRIOR_UPTREND_MIN_PCT = 0.30   # §3.double-bottom "prior uptrend ideally >= ~30%"; also a
                               # sane leader-precondition proxy for all bases (buy leaders).
PRIOR_UPTREND_LOOKBACK_WK = 30 # weeks back over which to measure the prior advance into the base.

# Cup-with-handle (§3 Cup-with-handle):
CUP_MIN_WEEKS = 7              # "minimum 7 weeks"
CUP_MAX_WEEKS = 65            # "commonly 7-65 weeks"
CUP_DEPTH_MIN = 0.12          # "12-33% from left rim to bottom"
CUP_DEPTH_MAX = 0.33          # (bear-market up to ~40-50% is riskier; textbook cap = 33%)
CUP_DEPTH_MAX_LOOSE = 0.50    # violent-bear tolerance (spec: "up to ~40-50% can still work")
HANDLE_MAX_WEEKS = 5          # handles are short (days to a few weeks)
HANDLE_MIN_WEEKS = 1
HANDLE_DEPTH_MAX = 0.15       # "8-12% ... up to ~15% in choppy markets"
HANDLE_UPPER_HALF = True      # handle must form in upper half of the cup
HANDLE_WITHIN_OF_HIGH = 0.15  # handle within ~15% of the old high

# Flat base (§3 Flat base):
FLAT_MIN_WEEKS = 5            # "minimum ~5 weeks"
FLAT_DEPTH_MAX = 0.15         # "corrects no more than ~15%"

# Double-bottom (§3 Double-bottom):
DB_MIN_WEEKS = 7             # "minimum ~7 weeks"
DB_DEPTH_MAX = 0.40          # "up to ~40% peak-to-second-low; typically 20-30%"
DB_UNDERCUT = True           # "second low undercuts the first low"

# Generic consolidation / square (§3 Square/consolidation):
CONSOL_MIN_WEEKS = 5         # treated as flat-base-like sideways range (least specified)
CONSOL_DEPTH_MAX = 0.20      # box tolerance a touch looser than a strict flat base

# Volume dry-up (§3/§4): breakout volume >= +40-50% vs 50d avg; base contraction = dry-up.
# We flag dry-up when the base's recent-weeks avg daily volume < its 50d avg (contraction).
VOL_DRYUP_RATIO = 1.0        # base-window avg vol < 50d avg vol => contraction present


@dataclass
class Candidate:
    pattern: str
    found: bool
    pivot: Optional[float] = None
    base_start: Optional[pd.Timestamp] = None
    base_end: Optional[pd.Timestamp] = None
    depth_pct: Optional[float] = None
    duration_weeks: Optional[float] = None
    prior_uptrend_pct: Optional[float] = None
    reject_reason: Optional[str] = None


@dataclass
class BaseResult:
    symbol: Optional[str]
    as_of: pd.Timestamp
    found: bool
    pattern: Optional[str] = None
    pivot: Optional[float] = None
    base_start: Optional[pd.Timestamp] = None
    base_end: Optional[pd.Timestamp] = None
    depth_pct: Optional[float] = None
    duration_weeks: Optional[float] = None
    prior_uptrend_pct: Optional[float] = None
    volume_dryup: Optional[bool] = None
    notes: str = ""
    candidates: list = field(default_factory=list)

    def as_dict(self):
        return dict(
            symbol=self.symbol, as_of=str(self.as_of.date()),
            found=self.found, pattern=self.pattern,
            pivot=(round(self.pivot, 2) if self.pivot is not None else None),
            base_start=(str(self.base_start.date()) if self.base_start is not None else None),
            base_end=(str(self.base_end.date()) if self.base_end is not None else None),
            depth_pct=(round(self.depth_pct, 4) if self.depth_pct is not None else None),
            duration_weeks=(round(self.duration_weeks, 1) if self.duration_weeks is not None else None),
            prior_uptrend_pct=(round(self.prior_uptrend_pct, 4) if self.prior_uptrend_pct is not None else None),
            volume_dryup=self.volume_dryup, notes=self.notes,
            n_candidates_found=sum(1 for c in self.candidates if c.found),
        )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _to_weekly(daily: pd.DataFrame) -> pd.DataFrame:
    """Resample daily OHLCV to weekly (Fri-anchored) bars — the base is a weekly pattern."""
    agg = {"open": "first", "high": "max", "low": "min", "close": "last"}
    if "volume" in daily.columns:
        agg["volume"] = "sum"
    w = daily.resample("W-FRI").agg(agg).dropna(subset=["close"])
    return w


def _slice_asof(daily: pd.DataFrame, as_of) -> pd.DataFrame:
    as_of = pd.Timestamp(as_of)
    return daily.loc[daily.index <= as_of]


def _prior_uptrend_pct(weekly: pd.DataFrame, base_start_idx: int) -> float:
    """% advance into the base: low in the LOOKBACK window before base start -> price at base start."""
    lo_i = max(0, base_start_idx - PRIOR_UPTREND_LOOKBACK_WK)
    window = weekly.iloc[lo_i:base_start_idx + 1]
    if len(window) < 3:
        return 0.0
    trough = window["low"].min()
    peak_at_start = weekly.iloc[base_start_idx]["high"]
    if trough <= 0:
        return 0.0
    return float(peak_at_start / trough - 1.0)


def _volume_dryup(daily: pd.DataFrame, base_start: pd.Timestamp, base_end: pd.Timestamp) -> Optional[bool]:
    if "volume" not in daily.columns or daily["volume"].fillna(0).sum() == 0:
        return None
    ref = daily.loc[daily.index <= base_end]
    if len(ref) < 55:
        return None
    avg50 = ref["volume"].iloc[-50:].mean()
    base_vol = daily.loc[(daily.index >= base_start) & (daily.index <= base_end), "volume"]
    if len(base_vol) < 5 or avg50 <= 0:
        return None
    # contraction: last third of the base (where handle/tightening lives) is quieter than the 50d avg
    tail = base_vol.iloc[-max(5, len(base_vol) // 3):]
    return bool(tail.mean() < VOL_DRYUP_RATIO * avg50)


def _pivot(daily: pd.DataFrame, resistance_high: float) -> float:
    return round(float(resistance_high) + PIVOT_OFFSET, 2)


# ---------------------------------------------------------------------------
# pattern detectors — each returns a Candidate. Operate on weekly bars up to as_of.
# The base is the CONSOLIDATION ending at (or just before) the as-of week; we look for the
# most recent completed base whose right side is near as_of.
# ---------------------------------------------------------------------------
def _find_flat(weekly: pd.DataFrame, daily: pd.DataFrame) -> Candidate:
    """Flat base: tight sideways range >= 5 weeks, depth <= 15%, right edge near as_of."""
    n = len(weekly)
    best = Candidate("flat_base", False, reject_reason="no qualifying window")
    for dur in range(FLAT_MIN_WEEKS, min(20, n) + 1):
        win = weekly.iloc[n - dur:n]
        hi = win["high"].max(); lo = win["low"].min()
        if hi <= 0:
            continue
        depth = (hi - lo) / hi
        if depth <= FLAT_DEPTH_MAX:
            start_i = n - dur
            up = _prior_uptrend_pct(weekly, start_i)
            cand = Candidate("flat_base", True,
                             pivot=_pivot(daily, hi),
                             base_start=win.index[0], base_end=win.index[-1],
                             depth_pct=depth, duration_weeks=float(dur),
                             prior_uptrend_pct=up)
            if up < PRIOR_UPTREND_MIN_PCT:
                cand.found = False; cand.reject_reason = f"prior uptrend {up:.0%}<30%"
            # prefer the longest valid flat window (a real flat base tends to be the longer tight range)
            if cand.found and (not best.found or dur > (best.duration_weeks or 0)):
                best = cand
            elif not best.found:
                best = cand
    return best


def _find_consolidation(weekly: pd.DataFrame, daily: pd.DataFrame) -> Candidate:
    """Generic box/consolidation: sideways range >= 5 weeks, depth <= 20%."""
    n = len(weekly)
    best = Candidate("consolidation", False, reject_reason="no qualifying window")
    for dur in range(CONSOL_MIN_WEEKS, min(30, n) + 1):
        win = weekly.iloc[n - dur:n]
        hi = win["high"].max(); lo = win["low"].min()
        if hi <= 0:
            continue
        depth = (hi - lo) / hi
        if depth <= CONSOL_DEPTH_MAX:
            start_i = n - dur
            up = _prior_uptrend_pct(weekly, start_i)
            cand = Candidate("consolidation", True,
                             pivot=_pivot(daily, hi),
                             base_start=win.index[0], base_end=win.index[-1],
                             depth_pct=depth, duration_weeks=float(dur),
                             prior_uptrend_pct=up)
            if up < PRIOR_UPTREND_MIN_PCT:
                cand.found = False; cand.reject_reason = f"prior uptrend {up:.0%}<30%"
            if cand.found and (not best.found or dur > (best.duration_weeks or 0)):
                best = cand
            elif not best.found:
                best = cand
    return best


def _find_cup_with_handle(weekly: pd.DataFrame, daily: pd.DataFrame) -> Candidate:
    """Cup-with-handle: U-shaped decline+recovery >=7wk, depth 12-33%, handle in upper half,
    handle depth <=15% drifting down, pivot = handle high + $0.10."""
    n = len(weekly)
    best = Candidate("cup_with_handle", False, reject_reason="no qualifying cup")
    # search cup windows ending anywhere from ~5 weeks ago (leave room for a handle) to now
    for handle_len in range(0, HANDLE_MAX_WEEKS + 1):
        cup_end = n - handle_len            # cup right rim index (exclusive end for handle)
        if cup_end < CUP_MIN_WEEKS:
            continue
        for dur in range(CUP_MIN_WEEKS, min(CUP_MAX_WEEKS, cup_end) + 1):
            start_i = cup_end - dur
            if start_i < 0:
                continue
            cup = weekly.iloc[start_i:cup_end]
            if len(cup) < CUP_MIN_WEEKS:
                continue
            left_rim = cup["high"].iloc[0]
            right_rim = cup["high"].iloc[-1]
            bottom = cup["low"].min()
            bottom_i = cup["low"].idxmin()
            if left_rim <= 0 or right_rim <= 0:
                continue
            top = max(left_rim, right_rim)
            depth = (top - bottom) / top
            # rims roughly equal (within 12%); U-shape => bottom in the middle third-ish
            rims_equal = abs(left_rim - right_rim) / top <= 0.12
            pos = cup.index.get_loc(bottom_i) / max(1, len(cup) - 1)
            u_shaped = 0.2 <= pos <= 0.8
            if not (CUP_DEPTH_MIN <= depth <= CUP_DEPTH_MAX_LOOSE and rims_equal and u_shaped):
                continue
            textbook_depth = depth <= CUP_DEPTH_MAX
            # handle (if any)
            handle = weekly.iloc[cup_end:n]
            handle_ok = True; handle_hi = right_rim; hnote = "no-handle"
            if handle_len >= HANDLE_MIN_WEEKS and len(handle) >= 1:
                handle_hi = handle["high"].max()
                handle_lo = handle["low"].min()
                hdepth = (handle_hi - handle_lo) / handle_hi if handle_hi > 0 else 1
                in_upper_half = handle_lo >= bottom + 0.5 * (top - bottom)
                within_high = (top - handle_hi) / top <= HANDLE_WITHIN_OF_HIGH
                # handle should drift down (its high shouldn't exceed the right rim by much)
                drifts_down = handle_hi <= right_rim * 1.02
                handle_ok = (hdepth <= HANDLE_DEPTH_MAX and in_upper_half and within_high and drifts_down)
                hnote = f"handle {handle_len}wk depth {hdepth:.0%}"
            pivot_high = handle_hi
            start_i2 = start_i
            up = _prior_uptrend_pct(weekly, start_i2)
            cand = Candidate("cup_with_handle", True,
                             pivot=_pivot(daily, pivot_high),
                             base_start=cup.index[0], base_end=weekly.index[-1],
                             depth_pct=depth, duration_weeks=float(dur + handle_len),
                             prior_uptrend_pct=up)
            reasons = []
            if not handle_ok:
                reasons.append("handle-defect")
            if not textbook_depth:
                reasons.append(f"deep({depth:.0%}>33%)")
            if up < PRIOR_UPTREND_MIN_PCT:
                reasons.append(f"prior uptrend {up:.0%}<30%")
            if reasons:
                cand.found = False; cand.reject_reason = "; ".join(reasons)
            else:
                cand.reject_reason = hnote
            # prefer a valid, deeper/rounder cup; keep the first VALID one found (longest handle search order)
            if cand.found:
                return cand
            if not best.found:
                best = cand
    return best


def _find_double_bottom(weekly: pd.DataFrame, daily: pd.DataFrame) -> Candidate:
    """Double-bottom W: two lows, 2nd undercuts 1st, middle peak = pivot point.
    >=7 weeks, depth <=40%, pivot = mid-'W' high + $0.10."""
    n = len(weekly)
    best = Candidate("double_bottom", False, reject_reason="no qualifying W")
    for dur in range(DB_MIN_WEEKS, min(40, n) + 1):
        start_i = n - dur
        win = weekly.iloc[start_i:n]
        if len(win) < DB_MIN_WEEKS:
            continue
        lows = win["low"].values
        highs = win["high"].values
        # first low = min of first half; second low = min of second half
        half = len(win) // 2
        i1 = int(np.argmin(lows[:half])) if half >= 1 else 0
        i2 = half + int(np.argmin(lows[half:]))
        low1 = lows[i1]; low2 = lows[i2]
        if i2 <= i1 + 1:
            continue
        # middle peak between the two lows
        mid = highs[i1 + 1:i2]
        if len(mid) == 0:
            continue
        mid_peak = mid.max()
        top = win["high"].max()
        if top <= 0:
            continue
        depth = (top - min(low1, low2)) / top
        undercut = low2 < low1  # 2nd low undercuts 1st (shakeout)
        # recovery: last close back above the middle peak region / near right side rising
        recovering = win["close"].iloc[-1] > mid_peak * 0.90
        # genuine "W" geometry (spec §3): the two lows must be COMPARABLE legs (within ~10%),
        # not one deep low + noise, AND the middle peak must be a real intervening rally that
        # retraces at least ~40% of the way from the lows back up to the base top (a shallow
        # bump is a single bottom, not a W). These are shape rules from the published pattern,
        # not tolerances fitted to the advisor's picks.
        legs_comparable = abs(low2 - low1) / max(low1, low2) <= 0.12
        mid_retrace = (mid_peak - min(low1, low2)) / (top - min(low1, low2) + 1e-9)
        real_middle_peak = mid_retrace >= 0.40
        valid_shape = (mid_peak > low1 and mid_peak > low2 and depth <= DB_DEPTH_MAX
                       and undercut and recovering and legs_comparable and real_middle_peak)
        up = _prior_uptrend_pct(weekly, start_i)
        cand = Candidate("double_bottom", True,
                         pivot=_pivot(daily, mid_peak),
                         base_start=win.index[0], base_end=win.index[-1],
                         depth_pct=depth, duration_weeks=float(dur),
                         prior_uptrend_pct=up)
        reasons = []
        if not valid_shape:
            if not undercut:
                reasons.append("2nd-low-no-undercut")
            if depth > DB_DEPTH_MAX:
                reasons.append(f"deep({depth:.0%}>40%)")
            if not recovering:
                reasons.append("not-recovered")
            if not (mid_peak > low1 and mid_peak > low2):
                reasons.append("no-mid-peak")
            if not legs_comparable:
                reasons.append("legs-unequal(not-W)")
            if not real_middle_peak:
                reasons.append("shallow-mid-peak(single-bottom)")
        if up < PRIOR_UPTREND_MIN_PCT:
            reasons.append(f"prior uptrend {up:.0%}<30%")
        if reasons:
            cand.found = False; cand.reject_reason = "; ".join(reasons)
            if not best.found:
                best = cand
        else:
            return cand
    return best


# priority order: the more specific/structured pattern wins when several match.
_DETECTORS = [
    ("double_bottom", _find_double_bottom),
    ("cup_with_handle", _find_cup_with_handle),
    ("flat_base", _find_flat),
    ("consolidation", _find_consolidation),
]


def detect_base(daily_df: pd.DataFrame, as_of, *, symbol: Optional[str] = None,
                min_bars: int = 60) -> BaseResult:
    """Detect the most recent valid O'Neil base as of `as_of`, using only bars <= as_of.

    Returns a BaseResult. If no valid base is found, found=False and candidates carries the
    best near-miss per pattern for failure analysis.
    """
    as_of = pd.Timestamp(as_of)
    df = daily_df.copy()
    df = df[[c for c in ("open", "high", "low", "close", "volume") if c in df.columns]]
    df = df[~df.index.duplicated(keep="last")].sort_index()
    # HARD no-lookahead: cut everything after as_of before any computation
    df = _slice_asof(df, as_of)
    if len(df) < min_bars:
        return BaseResult(symbol, as_of, False, notes=f"insufficient history ({len(df)} bars < {min_bars})")

    weekly = _to_weekly(df)
    if len(weekly) < CUP_MIN_WEEKS + 2:
        return BaseResult(symbol, as_of, False, notes=f"insufficient weekly bars ({len(weekly)})")

    candidates = []
    winner = None
    for name, fn in _DETECTORS:
        try:
            c = fn(weekly, df)
        except Exception as ex:
            c = Candidate(name, False, reject_reason=f"detector-error: {ex}")
        candidates.append(c)
        if c.found and winner is None:
            winner = c

    if winner is None:
        best_near = min((c for c in candidates), key=lambda c: 0, default=None)
        return BaseResult(symbol, as_of, False, notes="no valid base",
                          candidates=candidates)

    dry = _volume_dryup(df, winner.base_start, winner.base_end)
    return BaseResult(
        symbol, as_of, True,
        pattern=winner.pattern, pivot=winner.pivot,
        base_start=winner.base_start, base_end=winner.base_end,
        depth_pct=winner.depth_pct, duration_weeks=winner.duration_weeks,
        prior_uptrend_pct=winner.prior_uptrend_pct,
        volume_dryup=dry,
        notes=winner.reject_reason or "",
        candidates=candidates,
    )


def load_ohlc_json(path: str) -> pd.DataFrame:
    """Load a cache JSON (list of {date,open,high,low,close,volume}) into a daily OHLC frame."""
    import json
    rows = json.load(open(path))
    if not rows:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None).dt.normalize()
    df = df.set_index("date").sort_index()
    keep = [c for c in ("open", "high", "low", "close", "volume") if c in df.columns]
    return df[keep].astype(float)


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 3:
        d = load_ohlc_json(sys.argv[1])
        r = detect_base(d, sys.argv[2], symbol=sys.argv[1])
        import json as _j
        print(_j.dumps(r.as_dict(), indent=2))
