"""
S8 mechanical simulator — independently re-derives what trades the British IC / S8
strategy WOULD make, directly from raw SPXW 1-minute options market data (not from
account fills). This is a from-scratch simulator, separate from reconstruct.py
(which replays real fills). See british_ic/SIMULATOR_BUILD_PROGRESS.md for the
build log and design rationale.

Data: C:\\TradingDesk-Local\\warehouse\\raw\\options_1m\\SPXW\\{ohlc,quote}\\YYYYMMDD.parquet
(read-only, never written to).

Usage:
    from s8_mechanical_simulator import TEMPLATES, simulate_day
    trades = simulate_day("20251231", TEMPLATES["Puts-80-$4"])
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, time
from pathlib import Path

import numpy as np
import pandas as pd

WAREHOUSE = Path(r"C:\TradingDesk-Local\warehouse\raw\options_1m\SPXW")

# ---------------------------------------------------------------------------
# Template configuration
#
# STAGE B FINAL (2026-07-09) — entry-time grids are REAL, computed schedules
# derived directly from combo_ledger.csv (all 2,592 combos, filtered to
# single-long-leg combos, n=2,276) joined against TAT-tradelog's true Template
# labels (reusing template_join.py's join_full_range() function). A real strike-
# parsing regex bug was found+fixed along the way: `long_strikes` column values
# look like "[np.int64(6975)]" and need `re.search(r'\((\d+)\)', s).group(1)`,
# NOT a bare \d+ search (which matches the "64" inside "int64" first). Result:
# 1,350 of 2,276 rows got a real Template label (1,071 MATCHED + 279
# AMBIGUOUS_MULTI_CANDIDATE, both usable).
#
# Grids below are 5-min-bucketed real observed entry times, ET (the warehouse's
# native timezone, matches the raw quote data's own timestamp convention -- do
# NOT convert to CT). Threshold = buckets with >=5% of that template's
# observations. Seven templates (80-$4 x2, 80-$3 x2, 50-$2 x2, 80-$2) have
# REAL-DERIVED schedules (n=20 to n=491, trustworthy). Four templates
# (Puts/Calls-50-$3, Puts/Calls-50-$4) have only 1-3 real observations each --
# too thin to derive a schedule from -- and FALL BACK to the documented
# schedule in STRATEGY_MECHANICS.md / docs/S8_SPEC.md section 2.1 (the "50-$4
# afternoon grid ~12:15-13:45 CT" pattern), converted CT -> ET by adding 1 hour.
# This real-derived vs. documented-fallback distinction is flagged per-template
# below and matters for interpreting calibration results -- see
# SIMULATOR_STAGE_B_PROGRESS.md.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Template:
    name: str
    side: str  # "PUT" or "CALL" -- which side the short leg is sold on (credit spread)
    entry_times_et: tuple[str, ...]  # "HH:MM" grid, ET
    target_delta: float  # median short-leg |delta| target from template_delta_stats.csv
    target_width: float  # median long-leg width (points) from template_delta_stats.csv
    stop_multiple: float  # StopMultiple, S8_SPEC.md section 2.3 / STRATEGY_MECHANICS.md section 4
    target_credit: float  # nominal "$2/$3/$4" label, used only as a sanity display value


TEMPLATES: dict[str, Template] = {
    "Puts-80-$4": Template(
        name="British IC - Puts - 80 - $4",
        side="PUT",
        # REAL-DERIVED, n=491. >=5% buckets from real combo_ledger/TAT join.
        entry_times_et=("09:30", "09:40", "09:50", "10:05", "10:15", "13:00"),
        target_delta=0.2318,  # delta_median, template_delta_stats.csv
        target_width=80.0,    # width_median
        stop_multiple=3.3,
        target_credit=4.0,
    ),
    "Calls-80-$4": Template(
        name="British IC - Calls - 80 - $4",
        side="CALL",
        # REAL-DERIVED, n=363. >=5% buckets from real combo_ledger/TAT join.
        entry_times_et=("09:30", "09:40", "09:50", "10:05", "10:15"),
        target_delta=0.2489,
        target_width=75.0,
        stop_multiple=3.3,
        target_credit=4.0,
    ),
    "Puts-50-$4": Template(
        name="British IC - Puts - 50 - $4",
        side="PUT",
        # THIN/DOCUMENTED-FALLBACK: n=1 real observation (10:50 ET) -- too few to
        # trust. Falls back to STRATEGY_MECHANICS.md/S8_SPEC.md section 2.1's
        # documented "50-$4 afternoon grid ~12:15-13:45 CT" -> ET (+1h):
        # 12:15/12:45/13:00/13:30 CT -> 13:15/13:45/14:00/14:30 ET.
        entry_times_et=("13:15", "13:45", "14:00", "14:30"),
        target_delta=0.2692,
        target_width=50.0,
        stop_multiple=3.2,
        target_credit=4.0,
    ),
    "Calls-50-$4": Template(
        name="British IC - Calls - 50 - $4",
        side="CALL",
        # THIN/DOCUMENTED-FALLBACK: n=1 real observation (11:05 ET) -- too few to
        # trust. Same documented-CT->ET fallback grid as Puts-50-$4.
        entry_times_et=("13:15", "13:45", "14:00", "14:30"),
        target_delta=0.2766,
        target_width=50.0,
        stop_multiple=3.2,
        target_credit=4.0,
    ),
    "Puts-80-$3": Template(
        name="British IC - Puts - 80 - $3",
        side="PUT",
        # REAL-DERIVED, n=96. >=5% buckets from real combo_ledger/TAT join.
        entry_times_et=("13:00", "13:15", "13:30", "13:55"),
        target_delta=0.2632,
        target_width=80.0,
        stop_multiple=2.4,
        target_credit=3.0,
    ),
    "Calls-80-$3": Template(
        name="British IC - Calls - 80 - $3",
        side="CALL",
        # REAL-DERIVED, n=44. >=5% buckets from real combo_ledger/TAT join.
        entry_times_et=("10:15", "13:00", "13:15", "13:30", "14:30"),
        target_delta=0.2843,
        target_width=70.0,
        stop_multiple=2.4,
        target_credit=3.0,
    ),
    "Puts-50-$2": Template(
        name="British IC - Puts - 50 - $2",
        side="PUT",
        # REAL-DERIVED, n=179. >=5% buckets from real combo_ledger/TAT join.
        entry_times_et=("13:00", "13:15", "13:30", "13:55", "14:05", "14:25", "14:30"),
        target_delta=0.2156,
        target_width=45.0,
        stop_multiple=2.0,
        target_credit=2.0,
    ),
    "Calls-50-$2": Template(
        name="British IC - Calls - 50 - $2",
        side="CALL",
        # REAL-DERIVED, n=151. >=5% buckets from real combo_ledger/TAT join.
        entry_times_et=("13:00", "13:15", "13:30", "13:55", "14:05", "14:10", "14:30"),
        target_delta=0.2363,
        target_width=45.0,
        stop_multiple=2.0,
        target_credit=2.0,
    ),
    "Puts-80-$2": Template(
        name="British IC - Puts - 80 - $2",
        side="PUT",
        # REAL-DERIVED, n=20, no dominant slot (matches STRATEGY_MECHANICS.md's
        # existing characterization) -- all buckets clearing >=5% listed.
        entry_times_et=("11:05", "12:05", "12:30", "12:55", "13:00", "13:15", "13:25",
                         "13:30", "13:40", "14:00", "14:15", "14:20", "14:25", "14:35", "14:40"),
        target_delta=0.2444,
        target_width=80.0,
        stop_multiple=2.0,
        target_credit=2.0,
    ),
    "Puts-50-$3": Template(
        name="British IC - Puts - 50 - $3",
        side="PUT",
        # THIN/DOCUMENTED-FALLBACK: n=3 real observations (11:05, 11:10, 12:10 ET,
        # not statistically reliable). Same documented-CT->ET fallback grid as
        # Puts/Calls-50-$4.
        entry_times_et=("13:15", "13:45", "14:00", "14:30"),
        target_delta=0.2846,
        target_width=50.0,
        stop_multiple=2.4,
        target_credit=3.0,
    ),
    "Calls-50-$3": Template(
        name="British IC - Calls - 50 - $3",
        side="CALL",
        # THIN/DOCUMENTED-FALLBACK: n=1 real observation (11:05 ET) -- too few to
        # trust. Same documented-CT->ET fallback grid as Puts-50-$4.
        entry_times_et=("13:15", "13:45", "14:00", "14:30"),
        target_delta=0.2923,  # delta_median, template_delta_stats.csv
        target_width=50.0,
        stop_multiple=2.4,
        target_credit=3.0,
    ),
}

SETTLEMENT_TIME_ET = "16:00"  # last available 1-min bar for 0DTE SPXW in this warehouse
RISK_FREE_RATE = 0.045  # flat proxy; short-dated 0DTE, negligible impact on parity/BS
SHORT_STOP_SLIPPAGE_MULT = 13.6  # mid + 13.6x quoted half-spread, per locked fill model
LONG_CLOSE_SLIPPAGE_MULT = 2.0   # mid + 2.0x quoted half-spread, per locked fill model


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------


def _load_day(date_str: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Load ohlc + quote parquet for a given YYYYMMDD date, 0DTE (expiration==date) rows only."""
    ohlc = pd.read_parquet(WAREHOUSE / "ohlc" / f"{date_str}.parquet")
    quote = pd.read_parquet(WAREHOUSE / "quote" / f"{date_str}.parquet")
    exp = f"{date_str[0:4]}-{date_str[4:6]}-{date_str[6:8]}"
    ohlc0 = ohlc[ohlc["expiration"] == exp].copy()
    quote0 = quote[quote["expiration"] == exp].copy()
    return ohlc0, quote0


def _mid(bid, ask):
    """Scalar or vectorized (pandas Series) mid-price, nan where quote is invalid."""
    if isinstance(bid, pd.Series) or isinstance(ask, pd.Series):
        valid = (bid > 0) & (ask > 0) & (ask >= bid)
        return np.where(valid, (bid + ask) / 2.0, np.nan)
    if bid is None or ask is None or bid <= 0 or ask <= 0 or ask < bid:
        return float("nan")
    return (bid + ask) / 2.0


# ---------------------------------------------------------------------------
# Underlying spot via put-call parity
#
# No separate underlying/spot feed exists in the warehouse (checked: `derived\`
# only has *_gex_daily.parquet aggregates and ddoi_spx/spxw_daily.parquet --
# neither is a spot price series; `raw\options_1m\SPX\` is itself another
# options chain, not an underlying feed). Derived synthetically:
#   S ~= C - P + K   (0DTE => time-to-expiry discount factor ~1, ignored)
# using several near-the-money strikes and taking the median to reduce
# single-quote noise, at each timestamp needed.
# ---------------------------------------------------------------------------


def estimate_spot(quote0: pd.DataFrame, timestamp: str, approx_spot_hint: float | None = None) -> float:
    """
    Estimate SPX spot at `timestamp` via put-call parity across the chain's
    available strikes for that minute. Uses a two-pass approach: first pass
    uses the full available strike range to get a rough spot, second pass
    restricts to strikes within 3% of that rough spot (true near-ATM) and
    takes the median parity-implied spot for robustness to wide/stale quotes.
    """
    snap = quote0[quote0["timestamp"] == timestamp]
    if snap.empty:
        return float("nan")

    calls = snap[snap["right"] == "CALL"][["strike", "bid", "ask"]].copy()
    puts = snap[snap["right"] == "PUT"][["strike", "bid", "ask"]].copy()
    calls["mid"] = _mid(calls["bid"], calls["ask"])
    puts["mid"] = _mid(puts["bid"], puts["ask"])
    calls = calls.dropna(subset=["mid"])
    puts = puts.dropna(subset=["mid"])

    merged = calls.merge(puts, on="strike", suffixes=("_c", "_p"))
    if merged.empty:
        return float("nan")
    merged["implied_spot"] = merged["mid_c"] - merged["mid_p"] + merged["strike"]

    # Pass 1: rough spot from full set (median is robust to deep ITM/OTM noise)
    rough = merged["implied_spot"].median()

    # Pass 2: restrict to strikes within 3% of rough spot, re-derive
    if approx_spot_hint is not None:
        rough = approx_spot_hint
    near = merged[(merged["strike"] >= rough * 0.97) & (merged["strike"] <= rough * 1.03)]
    if len(near) >= 3:
        return float(near["implied_spot"].median())
    return float(rough)


# ---------------------------------------------------------------------------
# Delta estimation
#
# No greeks/IV feed exists anywhere in the warehouse (checked: raw/options_1m
# has only ohlc + quote, no greeks; derived/ has only GEX/DDOI daily
# aggregates, not per-contract IV or delta). Delta is estimated via
# Black-Scholes, back-solving IV from the option's own quoted mid price
# (Newton's method / bisection on BS price), then computing BS delta from
# that IV. This is more faithful to the "vol-adaptive same-delta-target-
# across-regimes" finding in STRATEGY_MECHANICS.md section 2 than a fixed
# moneyness/strike-distance proxy would be, since it reprices per-strike vol
# skew implicitly (each strike's IV is solved independently from its own
# quote, not assumed flat).
# ---------------------------------------------------------------------------


def _bs_price(S: float, K: float, T: float, r: float, sigma: float, right: str) -> float:
    if T <= 0 or sigma <= 0:
        return max(0.0, (S - K) if right == "CALL" else (K - S))
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    from math import erf

    def ncdf(x):
        return 0.5 * (1 + erf(x / math.sqrt(2)))

    if right == "CALL":
        return S * ncdf(d1) - K * math.exp(-r * T) * ncdf(d2)
    else:
        return K * math.exp(-r * T) * ncdf(-d2) - S * ncdf(-d1)


def _bs_delta(S: float, K: float, T: float, r: float, sigma: float, right: str) -> float:
    if T <= 0 or sigma <= 0:
        return 1.0 if (right == "CALL" and S > K) else (0.0 if right == "CALL" else (-1.0 if S < K else 0.0))
    from math import erf

    def ncdf(x):
        return 0.5 * (1 + erf(x / math.sqrt(2)))

    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    if right == "CALL":
        return ncdf(d1)
    else:
        return ncdf(d1) - 1.0


def implied_vol_from_price(price: float, S: float, K: float, T: float, r: float, right: str) -> float:
    """Bisection solve for IV given a quoted option mid price. Returns nan if unsolvable."""
    if price <= 0 or T <= 0 or S <= 0 or K <= 0:
        return float("nan")
    intrinsic = max(0.0, (S - K) if right == "CALL" else (K - S))
    if price < intrinsic - 1e-6:
        return float("nan")  # below intrinsic, can't solve (stale/crossed quote)

    lo, hi = 1e-4, 5.0
    f_lo = _bs_price(S, K, T, r, lo, right) - price
    f_hi = _bs_price(S, K, T, r, hi, right) - price
    if f_lo * f_hi > 0:
        return float("nan")
    for _ in range(60):
        mid = (lo + hi) / 2
        f_mid = _bs_price(S, K, T, r, mid, right) - price
        if abs(f_mid) < 1e-6:
            return mid
        if f_lo * f_mid < 0:
            hi = mid
            f_hi = f_mid
        else:
            lo = mid
            f_lo = f_mid
    return (lo + hi) / 2


def estimate_delta(price: float, S: float, K: float, T: float, right: str, r: float = RISK_FREE_RATE) -> float:
    iv = implied_vol_from_price(price, S, K, T, r, right)
    if math.isnan(iv):
        return float("nan")
    return _bs_delta(S, K, T, r, iv, right)


def _time_to_expiry_years(timestamp: str, settlement_hhmm: str = "16:00") -> float:
    """0DTE time-to-expiry in years from a given ET HH:MM timestamp to settlement same day."""
    t = datetime.strptime(timestamp[11:16], "%H:%M")
    close = datetime.strptime(settlement_hhmm, "%H:%M")
    minutes_left = max((close - t).total_seconds() / 60.0, 1.0)  # floor at 1 min to avoid T=0
    # trading-minutes-per-year proxy: 390 min/day * 252 days/year
    return minutes_left / (390.0 * 252.0)


# ---------------------------------------------------------------------------
# Strike selection
# ---------------------------------------------------------------------------


@dataclass
class TradeResult:
    date: str
    template: str
    side: str
    entry_time: str
    spot_at_entry: float
    short_strike: float
    long_strike: float
    width: float
    short_entry_mid: float
    long_entry_mid: float
    entry_credit: float  # short_entry_mid - long_entry_mid
    short_delta_at_entry: float
    stop_target: float  # PriceStopTarget
    exit_time: str = ""
    exit_reason: str = ""  # "stop" | "settlement" | "no_data"
    short_exit_price: float = float("nan")
    long_exit_price: float = float("nan")
    exit_debit: float = float("nan")  # what it cost to close the spread
    pnl_per_spread: float = float("nan")  # (entry_credit - exit_debit) * 100, per 1 spread


def _select_strikes(quote0: pd.DataFrame, timestamp: str, side: str, spot: float, target_delta: float,
                     target_width: float, T: float) -> dict | None:
    """
    Pick short strike whose BS-implied |delta| is closest to target_delta, then
    long strike = short strike -/+ target_width (long protects further OTM).
    Search across strikes within a reasonable band around spot.
    """
    right = side  # "PUT" or "CALL"
    snap = quote0[(quote0["timestamp"] == timestamp) & (quote0["right"] == right)].copy()
    if snap.empty:
        return None
    snap["mid"] = _mid(snap["bid"], snap["ask"])
    snap = snap.dropna(subset=["mid"])
    if snap.empty:
        return None

    # restrict candidate short strikes to a plausible OTM band (target delta ~0.15-0.40)
    if right == "PUT":
        cand = snap[snap["strike"] < spot * 1.005]
    else:
        cand = snap[snap["strike"] > spot * 0.995]
    if cand.empty:
        return None

    best_row = None
    best_diff = float("inf")
    for _, row in cand.iterrows():
        d = estimate_delta(row["mid"], spot, row["strike"], T, right)
        if math.isnan(d):
            continue
        diff = abs(abs(d) - target_delta)
        if diff < best_diff:
            best_diff = diff
            best_row = (row["strike"], row["mid"], d)
    if best_row is None:
        return None

    short_strike, short_mid, short_delta = best_row
    if right == "PUT":
        long_strike = short_strike - target_width
    else:
        long_strike = short_strike + target_width

    # snap long strike to nearest available strike on the chain
    avail_strikes = snap["strike"].unique()
    long_strike = float(avail_strikes[np.argmin(np.abs(avail_strikes - long_strike))])
    long_row = snap[snap["strike"] == long_strike]
    if long_row.empty:
        return None
    long_mid = float(long_row["mid"].iloc[0])

    return {
        "short_strike": float(short_strike),
        "short_mid": float(short_mid),
        "short_delta": float(short_delta),
        "long_strike": long_strike,
        "long_mid": long_mid,
    }


# ---------------------------------------------------------------------------
# Position tracking (stop trigger, B2 long-leg close, settlement)
# ---------------------------------------------------------------------------


def _quote_at(quote0: pd.DataFrame, timestamp: str, right: str, strike: float) -> tuple[float, float, float] | None:
    row = quote0[(quote0["timestamp"] == timestamp) & (quote0["right"] == right) & (quote0["strike"] == strike)]
    if row.empty:
        return None
    bid = float(row["bid"].iloc[0])
    ask = float(row["ask"].iloc[0])
    mid = _mid(bid, ask)
    if math.isnan(mid):
        return None
    half_spread = (ask - bid) / 2.0
    return mid, half_spread, ask - bid


def _minutes_after(timestamp: str, n: int) -> str:
    date_part, time_part = timestamp.split("T")
    t = datetime.strptime(time_part[:5], "%H:%M")
    t2 = t.replace(minute=(t.minute + n) % 60, hour=t.hour + (t.minute + n) // 60)
    return f"{date_part}T{t2.strftime('%H:%M')}:00.000"


def simulate_trade(quote0: pd.DataFrame, date_str: str, template: Template, entry_hhmm: str,
                    spot_at_entry: float) -> TradeResult | None:
    ts = f"{date_str[0:4]}-{date_str[4:6]}-{date_str[6:8]}T{entry_hhmm}:00.000"
    right = template.side
    T_entry = _time_to_expiry_years(ts)

    picked = _select_strikes(quote0, ts, right, spot_at_entry, template.target_delta, template.target_width, T_entry)
    if picked is None:
        return None

    short_strike = picked["short_strike"]
    long_strike = picked["long_strike"]
    short_entry_mid = picked["short_mid"]
    long_entry_mid = picked["long_mid"]
    short_delta = picked["short_delta"]

    entry_credit = short_entry_mid - long_entry_mid
    if entry_credit <= 0:
        return None  # not a real credit spread at this snapshot; skip

    stop_target = math.floor(10 * (entry_credit + template.stop_multiple)) / 10.0

    result = TradeResult(
        date=date_str,
        template=template.name,
        side=right,
        entry_time=entry_hhmm,
        spot_at_entry=spot_at_entry,
        short_strike=short_strike,
        long_strike=long_strike,
        width=abs(short_strike - long_strike),
        short_entry_mid=short_entry_mid,
        long_entry_mid=long_entry_mid,
        entry_credit=entry_credit,
        short_delta_at_entry=short_delta,
        stop_target=stop_target,
    )

    # walk forward minute by minute from entry to settlement, watching the
    # spread's cost-to-close (short_mid - long_mid) against stop_target
    all_ts = sorted(quote0[quote0["timestamp"] >= ts]["timestamp"].unique())
    settlement_ts = f"{date_str[0:4]}-{date_str[4:6]}-{date_str[6:8]}T{SETTLEMENT_TIME_ET}:00.000"

    stopped = False
    for cur_ts in all_ts:
        if cur_ts > settlement_ts:
            break
        sq = _quote_at(quote0, cur_ts, right, short_strike)
        lq = _quote_at(quote0, cur_ts, right, long_strike)
        if sq is None or lq is None:
            continue
        short_mid, short_half_spread, _ = sq
        long_mid, long_half_spread, _ = lq
        cost_to_close = short_mid - long_mid  # what it costs to buy back short & sell long at mid

        if cost_to_close >= stop_target:
            # STOP TRIGGERED. Short-leg exit filled at mid + 13.6x half-spread
            # (adverse direction = paying more to buy back the short).
            short_exit_price = short_mid + SHORT_STOP_SLIPPAGE_MULT * short_half_spread
            # B2: close long leg immediately too, at mid +/- 2.0x half-spread
            # (long leg is being SOLD to close -> adverse direction = mid - 2.0x half-spread)
            long_exit_price = max(0.0, long_mid - LONG_CLOSE_SLIPPAGE_MULT * long_half_spread)

            result.exit_time = cur_ts[11:16]
            result.exit_reason = "stop"
            result.short_exit_price = short_exit_price
            result.long_exit_price = long_exit_price
            result.exit_debit = short_exit_price - long_exit_price
            result.pnl_per_spread = (entry_credit - result.exit_debit) * 100.0
            stopped = True
            break

    if not stopped:
        # ran to settlement: mark both legs at the last available quote at/near 16:00 ET
        last_ts = None
        for cur_ts in reversed(all_ts):
            if cur_ts <= settlement_ts:
                sq = _quote_at(quote0, cur_ts, right, short_strike)
                lq = _quote_at(quote0, cur_ts, right, long_strike)
                if sq is not None and lq is not None:
                    last_ts = cur_ts
                    short_mid, _, _ = sq
                    long_mid, _, _ = lq
                    break
        if last_ts is None:
            result.exit_reason = "no_data"
            return result
        result.exit_time = last_ts[11:16]
        result.exit_reason = "settlement"
        result.short_exit_price = short_mid  # settlement fills modeled at mid (no slippage stated for this case)
        result.long_exit_price = long_mid
        result.exit_debit = short_mid - long_mid
        result.pnl_per_spread = (entry_credit - result.exit_debit) * 100.0

    return result


def simulate_day(date_str: str, template: Template) -> list[TradeResult]:
    """Simulate every scheduled entry for `template` on `date_str` (YYYYMMDD)."""
    ohlc0, quote0 = _load_day(date_str)
    if quote0.empty:
        return []

    results: list[TradeResult] = []
    rough_spot = None
    for hhmm in template.entry_times_et:
        ts = f"{date_str[0:4]}-{date_str[4:6]}-{date_str[6:8]}T{hhmm}:00.000"
        spot = estimate_spot(quote0, ts, approx_spot_hint=rough_spot)
        if math.isnan(spot):
            continue
        rough_spot = spot
        trade = simulate_trade(quote0, date_str, template, hhmm, spot)
        if trade is not None:
            results.append(trade)
    return results


def results_to_dataframe(results: list[TradeResult]) -> pd.DataFrame:
    return pd.DataFrame([r.__dict__ for r in results])


if __name__ == "__main__":
    import sys

    date_str = sys.argv[1] if len(sys.argv) > 1 else "20260101"
    tmpl_name = sys.argv[2] if len(sys.argv) > 2 else "Puts-80-$4"
    tmpl = TEMPLATES[tmpl_name]
    trades = simulate_day(date_str, tmpl)
    df = results_to_dataframe(trades)
    print(df.to_string())
