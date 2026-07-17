"""
s8_vol.py — underlying REALIZED-VOLATILITY helper for the S8 live-pilot rich entry
capture (Phase 1, gap-close #2; see docs/S8_LIVE_PILOT_DATA_CAPTURE_PLAN.md).

WHAT THIS IS FOR
----------------
capture_and_persist_entry records "everything we can" at the entry instant. One honest
gap in the Phase-1 record was ``EntryInfo.entry_realized_vol`` (left None). This module
fills it: it computes the underlying's recent realized volatility from SPX daily bars.

IMPORTANT — this is CONTEXT DATA, NOT A STRATEGY INPUT. It is never fed back into how S8
picks entries or exits (rule #1 stays clean); it is a well-defined, consistent number
attached to the record for later analysis. It therefore only needs a fixed, documented
definition — see realized_vol_from_closes() for the exact one.

Both functions are best-effort: the LIVE one never raises (None on any failure) so a vol
problem can never break the pilot cycle, and it only READS market data (zero-transmit).
"""
from __future__ import annotations

import math
import statistics
from typing import Any, Iterable, Optional

from ib_async import Index

# Default close-to-close lookback, in TRADING days (i.e. daily bars). ~21 trading days
# ≈ one calendar month of sessions — a standard short realized-vol window. This is a
# fixed definition for a context field, not a tuned strategy knob.
LOOKBACK_TRADING_DAYS = 21

# Trading days per year — the standard annualization factor for daily realized vol.
ANNUALIZATION_DAYS = 252

_SPX_EXCHANGE = "CBOE"


# --------------------------------------------------------------------------- #
# PURE: closing-price series -> annualized realized vol  (offline-testable)
# --------------------------------------------------------------------------- #
def realized_vol_from_closes(
    closes: Iterable[Any],
    annualization: int = ANNUALIZATION_DAYS,
) -> Optional[float]:
    """Annualized close-to-close realized volatility from a series of closing prices.

    DEFINITION (fixed and well-defined — this is context data, not a strategy input, so
    it just needs to be consistent):
      * daily log returns  r_t = ln(C_t / C_{t-1})  over the given closes (oldest→newest);
      * realized_vol = stdev(r) * sqrt(annualization), using the SAMPLE standard
        deviation (ddof=1, denominator N-1 — the usual realized-vol estimator);
      * annualization defaults to 252 (trading days per year).

    Returns None (never raises) when the series can't yield a valid number:
      * fewer than 3 usable closes (need ≥ 2 log returns for a sample stdev), OR
      * any close is None / NaN / non-positive (a non-positive close makes a log return
        undefined) — flagged by returning None rather than silently fabricating a value.
    """
    raw = list(closes)
    vals: list[float] = []
    for c in raw:
        if c is None:
            return None                      # a missing bar -> undefined, don't guess
        try:
            f = float(c)
        except (TypeError, ValueError):
            return None
        if f != f or f <= 0.0:               # NaN (f != f) or non-positive
            return None
        vals.append(f)

    if len(vals) < 3:                        # need >= 2 returns for a sample stdev
        return None

    rets = [math.log(vals[i] / vals[i - 1]) for i in range(1, len(vals))]
    if len(rets) < 2:
        return None
    try:
        sd = statistics.stdev(rets)          # sample stdev (ddof=1)
    except statistics.StatisticsError:
        return None
    return sd * math.sqrt(float(annualization))


# --------------------------------------------------------------------------- #
# LIVE: pull SPX daily bars, compute realized vol  (best-effort, never raises)
# --------------------------------------------------------------------------- #
def realized_vol_live(
    ib,
    lookback_days: int = LOOKBACK_TRADING_DAYS,
    annualization: int = ANNUALIZATION_DAYS,
) -> Optional[float]:
    """Best-effort annualized realized vol of SPX at entry from recent daily bars.

    Pulls SPX daily closes via ib.reqHistoricalData (whatToShow="TRADES", useRTH=True),
    requesting a generous calendar-day window so at least ``lookback_days`` trading bars
    are covered, then takes the most recent ``lookback_days + 1`` closes (that many closes
    yields ``lookback_days`` log returns) and runs realized_vol_from_closes().

    BEST-EFFORT by contract: NEVER raises — any failure (no entitlement, empty bars, a bad
    close) returns None so a vol problem can never break the pilot cycle. Reads only; it
    never transmits.
    """
    try:
        spx = Index("SPX", _SPX_EXCHANGE, "USD")
        try:
            ib.qualifyContracts(spx)
        except Exception:
            pass
        # Request extra calendar days: weekends/holidays mean trading bars < calendar days.
        duration_days = max(lookback_days * 2, lookback_days + 15, 40)
        bars = ib.reqHistoricalData(
            spx, endDateTime="", durationStr=f"{duration_days} D",
            barSizeSetting="1 day", whatToShow="TRADES", useRTH=True, formatDate=1)
        if not bars:
            return None
        closes = [getattr(b, "close", None) for b in bars]
        closes = closes[-(lookback_days + 1):]   # most recent (lookback_days+1) closes
        return realized_vol_from_closes(closes, annualization=annualization)
    except Exception:
        return None
