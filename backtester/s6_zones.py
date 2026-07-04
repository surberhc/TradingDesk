r"""
s6_zones.py — Brandon Wendell "Leg-Base Zoning" supply/demand zone detector.

PAPER / research only. OFFLINE. Windows. STRICTLY READ-ONLY on the warehouse.

WHAT THIS IS
------------
The mechanized, FROZEN implementation of Brandon Wendell's documented supply/demand
zone method, run on the intraday SPX price series (recovered per-minute from the 1-minute
SPXW warehouse via put-call parity in s6_recon, then resampled to 5-minute / 15-minute
bars). It is the "crown jewel" input to the S6 strike-selection experiment: it produces the
zone boundaries a discretionary trader would draw, so we can test whether placing short
strikes just outside those zones beats blind delta placement.

It makes NO strategy P&L decision and it is NOT tuned. It implements ONE plainly-documented
interpretation of each rule and FREEZES it. See the PRE-REGISTRATION block below.

===========================================================================================
PRE-REGISTRATION — FROZEN ZONE INTERPRETATION (rule #1: never curve-fit the signal)
===========================================================================================
Brandon's rules (from "Brandon Wendell Strategy Overview.pdf" / "Leg-Base Zoning Technique"
and the SPX cash-flow research handoff) are documented but leave several parameters as
ranges or "to test." Sweeping them and keeping the best would be curve-fitting the signal
DEFINITION, which is forbidden. So for EACH ambiguity we pick ONE plain, documented
interpretation, WRITE IT DOWN HERE, and FREEZE it. These are declared once as module
constants and never swept.

DECISION 1 — BASE vs LEG CANDLE (verbatim from the Leg-Base Zoning PDF):
    * base candle = its CLOSE stays INSIDE the prior candle's high-low range (equilibrium).
    * leg  candle = its CLOSE is OUTSIDE the prior candle's high-low range (imbalance).
  This is the exact documented definition; no free parameter. FROZEN.

DECISION 2 — BASE LENGTH: documented range is 1-6 candles.
  FROZEN CHOICE: a base is a MAXIMAL run of 1..6 consecutive base candles (BASE_MIN=1,
  BASE_MAX=6). A run longer than 6 is NOT a valid base (too long = not a crisp pause).
  We do not sweep BASE_MIN/BASE_MAX; we take the documented endpoints verbatim.

DECISION 3 — LEG-IN / LEG-OUT & PATTERN (DBR/RBR/RBD/DBD):
  * leg-IN  = the single leg candle IMMEDIATELY BEFORE the base (its direction: up=rally,
    down=drop).
  * leg-OUT = the departure AFTER the base. Documented departure rule: "require 2+
    follow-through candles making new highs/lows" AND "the move from the base should break
    a prior swing high or swing low."
  FROZEN CHOICE for the departure (DECISION 4 below) and pattern label:
    demand zone (short PUTs below it) = leg-OUT is UP (DBR if leg-in down, RBR if leg-in up).
    supply zone (short CALLs above it) = leg-OUT is DOWN (RBD if leg-in down..., DBD if down).
  Direction of leg-out is what makes it supply vs demand; we require BOTH follow-through
  AND the swing-break, per the documented "no nearby opposing pressure / break a prior
  swing" qualification.

DECISION 4 — DEPARTURE MAGNITUDE (documented as a range 1.5x-2.5x base range, "to test"):
  FROZEN CHOICE: the leg-out must (a) have >= DEPARTURE_FOLLOWTHROUGH (=2) candles after
  the base that make progressively new extremes in the leg-out direction, AND (b) travel a
  net distance of at least DEPARTURE_MIN_RANGE_MULT (=2.0) times the base's own high-low
  range measured from the base proximal edge. 2.0x is the MIDPOINT of the documented
  1.5x-2.5x band and the value Brandon's PDF uses for the "opposing-pressure" clearance
  ("within roughly two times the height of the zone"). We take 2.0x once and FREEZE it; we
  do NOT try 1.5/2.0/2.5 and keep the best.

DECISION 5 — ZONE BOUNDARIES (documented: full-wick "model A" vs distal/proximal "model B"):
  The handoff notes SMS zones are usually quoted as a simple low-high band (e.g. 3606-3610),
  which matches the FULL-WICK model. Brandon's own strike rule ("short put at/below demand
  LOW", "short call at/above supply HIGH") only needs the wick extremes.
  FROZEN CHOICE: BOUNDARY MODEL A (full wick range of the base):
      zone_low  = min(low  of all base candles)
      zone_high = max(high of all base candles)
    proximal edge (the edge price returns to first) and distal edge (the far edge) are then:
      demand: proximal = zone_high, distal = zone_low
      supply: proximal = zone_low,  distal = zone_high
  We take model A once and FREEZE it; we do NOT test A vs B and keep the better match.

DECISION 6 — FRESHNESS (documented: prefer fresh/untested zones):
  FROZEN CHOICE: a zone is FRESH at a query time T if price has NOT re-entered the zone
  band [zone_low, zone_high] on any completed bar strictly between the leg-out confirmation
  and T. We prefer fresh zones and, when required, EXCLUDE tested zones (never fall back to
  a tested zone to force a trade — a no-fresh-zone day is a NO-TRADE, recorded as a skip).

DECISION 7 — TIMEFRAME PRIORITY (documented: 5m most common, 15m second; "to test"):
  FROZEN CHOICE: prefer the 15-MINUTE zone when a fresh qualifying one exists on the
  correct side and is nearer than ~the expected move; otherwise fall back to the 5-MINUTE
  zone. Rationale from the handoff: "15-minute zones may be preferred when close enough...
  5-minute zones appear to be used when 15-minute zones are too far away/unavailable."
  Concretely: we take the NEAREST fresh qualifying zone to spot, and when a 5m and a 15m
  zone are BOTH fresh+qualifying, the 15m wins ties/near-ties (PREFER_15M=True). One rule,
  FROZEN — we do not sweep timeframe priority.

DECISION 8 — "NEAREST TO SPOT" SELECTION among multiple fresh qualifying zones on a side:
  FROZEN CHOICE: pick the fresh qualifying zone whose PROXIMAL edge is nearest to (but on
  the correct side of) spot — i.e. the closest overhead supply for calls, closest underfoot
  demand for puts. This matches "trade the first revisit" / place the short strike just
  beyond the RELEVANT (nearest) zone. FROZEN.

NO LOOK-AHEAD (load-bearing):
  For a query at time T (the 14:00 entry), we use ONLY bars whose CLOSE timestamp is <= T.
  A zone is admissible only if its leg-out CONFIRMATION bar closed at or before T. Freshness
  is evaluated only over bars that closed at or before T. This is pinned by a unit test.

Everything is plain pandas/numpy with comments on the "why".
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass

import numpy as np
import pandas as pd

# --------------------------------------------------------------------------- #
# FROZEN pre-registered constants (see PRE-REGISTRATION block above). NOT tuned.
# --------------------------------------------------------------------------- #
BASE_MIN_CANDLES = 1          # DECISION 2 — documented endpoints, verbatim.
BASE_MAX_CANDLES = 6
DEPARTURE_FOLLOWTHROUGH = 2   # DECISION 4a — "2+ follow-through candles".
DEPARTURE_MIN_RANGE_MULT = 2.0  # DECISION 4b — midpoint of documented 1.5-2.5x, = PDF "2x".
PREFER_15M = True             # DECISION 7 — 15m preferred over 5m when both fresh+qualifying.
SESSION_OPEN = _dt.time(9, 30)
SESSION_CLOSE = _dt.time(16, 0)

# Timeframes we build (minutes). Handoff: 5m most common, 15m second. FROZEN to these two.
TIMEFRAMES = (5, 15)


# --------------------------------------------------------------------------- #
# Bar resampling — session-aware OHLC from a 1-minute spot series.
# --------------------------------------------------------------------------- #
def resample_ohlc(spot_1m: pd.Series, timeframe_min: int, day: _dt.date) -> pd.DataFrame:
    """Resample a 1-minute spot series into session-aware OHLC bars of `timeframe_min`.

    `spot_1m` is a pandas Series indexed by tz-naive minute timestamps (the recovered SPX
    spot, one value per traded minute). Bars are label-LEFT, closed-LEFT within the regular
    session 09:30..16:00, so a bar's timestamp is its OPEN minute and its CLOSE time is
    (open + timeframe_min). We attach a `close_time` column = the instant the bar is fully
    formed and thus first usable without look-ahead.

    Only bars with at least one observation are returned (a bar with no spot obs is dropped;
    that mirrors "no trade" minutes). O/H/L/C are computed from whatever 1-min spots fall in
    the bar window — a faithful OHLC of the recovered underlying.
    """
    s = spot_1m.dropna().sort_index()
    if s.empty:
        return pd.DataFrame(columns=["open", "high", "low", "close", "close_time"])
    start = pd.Timestamp(_dt.datetime.combine(day, SESSION_OPEN))
    # Right edge for the last partial bar is the session close.
    rule = f"{timeframe_min}min"
    grp = s.groupby(pd.Grouper(freq=rule, origin=start, label="left", closed="left"))
    ohlc = grp.agg(["first", "max", "min", "last"])
    ohlc.columns = ["open", "high", "low", "close"]
    ohlc = ohlc.dropna(subset=["open"])
    # close_time = the bar is complete at open + timeframe (the first instant it is usable).
    ohlc["close_time"] = ohlc.index + pd.Timedelta(minutes=timeframe_min)
    ohlc.index.name = "open_time"
    return ohlc


# --------------------------------------------------------------------------- #
# Base / leg candle classification (DECISION 1).
# --------------------------------------------------------------------------- #
def classify_candles(ohlc: pd.DataFrame) -> pd.Series:
    """Label each candle 'base' | 'leg_up' | 'leg_down' per Brandon's definition.

    base : CLOSE inside the PRIOR candle's [low, high] range (equilibrium).
    leg  : CLOSE outside the prior candle's range (imbalance); direction from close vs
           prior high/low: close above prior high -> 'leg_up'; below prior low -> 'leg_down'.
    The FIRST candle of the day has no prior candle -> labeled 'base' (no imbalance provable).
    """
    labels = []
    highs = ohlc["high"].to_numpy()
    lows = ohlc["low"].to_numpy()
    closes = ohlc["close"].to_numpy()
    for i in range(len(ohlc)):
        if i == 0:
            labels.append("base")
            continue
        prior_hi, prior_lo = highs[i - 1], lows[i - 1]
        c = closes[i]
        if c > prior_hi:
            labels.append("leg_up")
        elif c < prior_lo:
            labels.append("leg_down")
        else:
            labels.append("base")
    return pd.Series(labels, index=ohlc.index, name="candle_type")


# --------------------------------------------------------------------------- #
# Zone container
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Zone:
    """One detected supply/demand zone (boundary model A = full wick range of the base)."""

    kind: str                 # 'demand' | 'supply'
    pattern: str              # 'DBR' | 'RBR' | 'RBD' | 'DBD'
    timeframe_min: int
    zone_low: float
    zone_high: float
    proximal: float           # edge price returns to first
    distal: float             # far edge
    base_start: pd.Timestamp  # open_time of first base candle
    base_end: pd.Timestamp    # open_time of last base candle
    confirm_time: pd.Timestamp  # close_time of the confirming leg-out candle (usable-after)
    base_count: int
    base_range: float

    def contains(self, price: float) -> bool:
        return self.zone_low <= price <= self.zone_high


# --------------------------------------------------------------------------- #
# Zone detection on ONE timeframe (all rules; no freshness/side filter yet).
# --------------------------------------------------------------------------- #
def detect_zones_timeframe(ohlc: pd.DataFrame, timeframe_min: int) -> list[Zone]:
    """Detect every leg-base zone on one timeframe's OHLC, applying the FROZEN rules.

    Algorithm (all documented, all frozen):
      1. classify each candle base/leg.
      2. find every MAXIMAL run of 1..BASE_MAX consecutive base candles.
      3. leg-IN = the candle immediately before the base run (must be a leg; its direction).
      4. leg-OUT = the departure right after the base run. Require:
           (a) >= DEPARTURE_FOLLOWTHROUGH candles after the base making progressively new
               extremes in the leg-out direction, AND
           (b) net travel from the base proximal edge >= DEPARTURE_MIN_RANGE_MULT * base_range,
               AND the leg-out breaks the base's opposite extreme (a prior swing) — i.e. an up
               leg-out closes above the base high; a down leg-out closes below the base low.
      5. classify demand (leg-out up) vs supply (leg-out down); pattern from leg-in+leg-out.
      6. boundaries = full wick range of the base (model A).
      7. confirm_time = close_time of the LAST required follow-through candle (usable-after).
    Runs longer than BASE_MAX candles are skipped (not a crisp base).
    """
    if len(ohlc) < 3:
        return []
    ctype = classify_candles(ohlc).to_numpy()
    highs = ohlc["high"].to_numpy()
    lows = ohlc["low"].to_numpy()
    closes = ohlc["close"].to_numpy()
    close_times = ohlc["close_time"].to_numpy()
    open_times = ohlc.index.to_numpy()
    n = len(ohlc)

    zones: list[Zone] = []
    i = 0
    while i < n:
        if ctype[i] != "base":
            i += 1
            continue
        # Maximal run of consecutive base candles [i .. j-1].
        j = i
        while j < n and ctype[j] == "base":
            j += 1
        run_len = j - i
        run_start, run_end = i, j - 1
        # Advance the outer cursor past this run regardless of whether it qualifies.
        next_i = j

        # Base length must be within the documented 1..BASE_MAX window.
        if run_len < BASE_MIN_CANDLES or run_len > BASE_MAX_CANDLES:
            i = next_i
            continue
        # Need a leg-IN candle before the run and a leg-OUT after it.
        if run_start == 0 or run_end + 1 >= n:
            i = next_i
            continue

        leg_in_type = ctype[run_start - 1]
        if leg_in_type not in ("leg_up", "leg_down"):
            i = next_i
            continue

        base_low = float(np.min(lows[run_start:run_end + 1]))
        base_high = float(np.max(highs[run_start:run_end + 1]))
        base_range = base_high - base_low
        if base_range <= 0:
            i = next_i
            continue

        # --- leg-OUT: the first candle after the base and its follow-through. ---
        out_idx = run_end + 1
        out_type = ctype[out_idx]
        if out_type not in ("leg_up", "leg_down"):
            i = next_i
            continue
        leg_out_up = out_type == "leg_up"

        # Break of the base's opposite extreme (a prior swing): up leg-out must close above
        # base_high; down leg-out must close below base_low.
        if leg_out_up and not (closes[out_idx] > base_high):
            i = next_i
            continue
        if (not leg_out_up) and not (closes[out_idx] < base_low):
            i = next_i
            continue

        # Follow-through: need >= DEPARTURE_FOLLOWTHROUGH candles from out_idx making
        # progressively new extremes in the leg-out direction.
        needed = DEPARTURE_FOLLOWTHROUGH
        ft_end = None
        if leg_out_up:
            run_extreme = highs[out_idx]
            cnt = 1
            k = out_idx + 1
            while k < n and cnt < needed:
                if highs[k] > run_extreme:
                    run_extreme = highs[k]
                    cnt += 1
                    k += 1
                else:
                    break
            if cnt >= needed:
                ft_end = k - 1
        else:
            run_extreme = lows[out_idx]
            cnt = 1
            k = out_idx + 1
            while k < n and cnt < needed:
                if lows[k] < run_extreme:
                    run_extreme = lows[k]
                    cnt += 1
                    k += 1
                else:
                    break
            if cnt >= needed:
                ft_end = k - 1
        if ft_end is None:
            i = next_i
            continue

        # Departure magnitude: net travel from base proximal edge >= mult * base_range.
        if leg_out_up:
            proximal = base_high      # demand: price returns down to the top of the base
            distal = base_low
            travel = run_extreme - proximal
        else:
            proximal = base_low       # supply: price returns up to the bottom of the base
            distal = base_high
            travel = proximal - run_extreme
        if travel < DEPARTURE_MIN_RANGE_MULT * base_range:
            i = next_i
            continue

        # Pattern label from leg-in + leg-out direction.
        if leg_out_up:
            kind = "demand"
            pattern = "RBR" if leg_in_type == "leg_up" else "DBR"
        else:
            kind = "supply"
            pattern = "DBD" if leg_in_type == "leg_down" else "RBD"

        zones.append(Zone(
            kind=kind, pattern=pattern, timeframe_min=timeframe_min,
            zone_low=base_low, zone_high=base_high,
            proximal=float(proximal), distal=float(distal),
            base_start=pd.Timestamp(open_times[run_start]),
            base_end=pd.Timestamp(open_times[run_end]),
            confirm_time=pd.Timestamp(close_times[ft_end]),
            base_count=run_len, base_range=base_range,
        ))
        i = next_i

    return zones


# --------------------------------------------------------------------------- #
# Freshness (DECISION 6) — evaluated causally over bars closing at/before T.
# --------------------------------------------------------------------------- #
def is_fresh(zone: Zone, ohlc: pd.DataFrame, as_of: pd.Timestamp) -> bool:
    """True iff price has NOT re-entered the zone band on any bar that closed strictly after
    the zone's confirm_time and at/before `as_of`.

    Re-entry = a bar whose [low, high] overlaps the zone band [zone_low, zone_high]. Only
    bars with close_time in (confirm_time, as_of] are considered => strictly causal.
    """
    mask = (ohlc["close_time"] > zone.confirm_time) & (ohlc["close_time"] <= as_of)
    after = ohlc[mask]
    if after.empty:
        return True
    overlaps = (after["low"] <= zone.zone_high) & (after["high"] >= zone.zone_low)
    return not bool(overlaps.any())


# --------------------------------------------------------------------------- #
# Zone universe at a query time (all timeframes, causal, with freshness).
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ZoneUniverse:
    """All zones known and admissible at a query time `as_of`, per timeframe, with freshness."""

    as_of: pd.Timestamp
    zones: list[Zone]              # every admissible zone (confirmed at/before as_of)
    fresh_flags: list[bool]        # parallel to zones: is each fresh at as_of


def build_zone_universe(
    spot_1m: pd.Series, day: _dt.date, as_of: pd.Timestamp,
    timeframes: tuple[int, ...] = TIMEFRAMES,
) -> ZoneUniverse:
    """Detect zones on each timeframe using ONLY bars closed at/before `as_of`, and flag
    freshness. This is the causal entry point the experiment calls at the 14:00 snapshot.

    No look-ahead: we resample the full 1-min series but then RESTRICT each timeframe's OHLC
    to bars whose close_time <= as_of before detection AND freshness. A zone is admissible
    only if its confirm_time <= as_of (guaranteed since detection only sees such bars)."""
    all_zones: list[Zone] = []
    fresh: list[bool] = []
    for tf in timeframes:
        ohlc = resample_ohlc(spot_1m, tf, day)
        ohlc = ohlc[ohlc["close_time"] <= as_of]
        if len(ohlc) < 3:
            continue
        zs = detect_zones_timeframe(ohlc, tf)
        for z in zs:
            # confirm_time <= as_of is guaranteed by the restricted ohlc, but assert cheaply.
            if z.confirm_time <= as_of:
                all_zones.append(z)
                fresh.append(is_fresh(z, ohlc, as_of))
    return ZoneUniverse(as_of=as_of, zones=all_zones, fresh_flags=fresh)


# --------------------------------------------------------------------------- #
# Selection (DECISIONS 7 & 8) — nearest fresh qualifying zone on the correct side.
# --------------------------------------------------------------------------- #
def select_zone(
    universe: ZoneUniverse, spot: float, side: str, prefer_15m: bool = PREFER_15M
) -> Zone | None:
    """Pick the zone to place a short strike beyond, per the FROZEN selection rule.

    side='demand' (for a short PUT): fresh demand zone BELOW spot, nearest proximal edge.
    side='supply' (for a short CALL): fresh supply zone ABOVE spot, nearest proximal edge.
    If both a 5m and a 15m zone qualify at (near-)equal nearness, prefer 15m (DECISION 7).
    Returns None if there is no fresh qualifying zone (=> NO TRADE; caller records a skip).
    """
    candidates: list[Zone] = []
    for z, fr in zip(universe.zones, universe.fresh_flags):
        if not fr:
            continue
        if z.kind != side:
            continue
        if side == "demand":
            # Demand must sit BELOW spot (short put placed at/below the zone low).
            if z.proximal <= spot and z.zone_high <= spot:
                candidates.append(z)
        else:  # supply
            if z.proximal >= spot and z.zone_low >= spot:
                candidates.append(z)
    if not candidates:
        return None

    def distance(z: Zone) -> float:
        return abs(spot - z.proximal)

    # Nearest proximal edge; break ties/near-ties toward 15m if prefer_15m.
    if prefer_15m:
        # Sort by (distance, timeframe-preference): larger timeframe wins on near-ties.
        candidates.sort(key=lambda z: (round(distance(z), 6), -z.timeframe_min))
    else:
        candidates.sort(key=lambda z: (round(distance(z), 6), z.timeframe_min))
    return candidates[0]
