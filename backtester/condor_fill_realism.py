r"""
condor_fill_realism.py — ARM 3: FILL REALISM for the 14:00 SPX 0DTE iron condor.

PAPER / research only. OFFLINE. Windows. STRICTLY READ-ONLY on the warehouse.

WHY THIS EXISTS
---------------
Every prior 0DTE-condor verdict flips sign between "mid fill" (positive) and
"50%-of-spread worst-side fill" (loses). We assumed f50-worst-side is the honest yardstick.
This script MEASURES what fill is actually realistic, so the whole line of research has an
honest bar. Pre-registered in docs\PREREG_condor_reopen_2026-07-06.md (Arm 3).

This is a MEASUREMENT, not a simulation. No P&L, no exit scan. For each tradeable day we
rebuild the SAME 14:00 iron condor the control builds (0.15-delta shorts, 5-pt wings), and
at the 14:00 snapshot we measure the bid/ask geometry of the four legs:

  * net MID credit          = sum of signed leg mids (sell shorts / buy wings at mid)
  * net WORST-SIDE credit   = sell shorts at BID, buy wings at ASK  (the control's honest open)
  * 4-leg round-trip spread cost ($) = sum over legs of (ask - bid), because crossing the
    full spread on entry AND exit pays each leg's bid/ask width exactly once per side, i.e.
    the round trip pays the full width once per leg per direction => the entry-to-exit cost
    of always crossing is sum(ask-bid) on the way in and again sum(ask-bid) on the way out.
    We report BOTH the one-way spread cost (= mid - worst_side credit, what the control's
    honest open already pays vs mid) and the full round-trip (2x one-way) cost.
  * spread cost as % of the MID credit — the headline: how much of the thin credit the
    bid/ask eats.
  * per-leg bid/ask width — to see whether the wings or the shorts are the wide legs.

WHICH FILL FRACTION DOES REALITY RESEMBLE?
------------------------------------------
The management report parameterizes fills as a fraction f of the net combo spread away from
mid: f=0 is mid, f=1.0 is the full worst-side. The control's honest open sits at f=1.0 (sell
bid / buy ask). The *realistic* execution fraction is an empirical question about how far
from mid a 4-leg SPX 0DTE combo actually fills. This script gives the ONE number that
anchors it: the one-way worst-side spread cost as a % of mid credit. If that % is small,
f50-worst-side (which charges half of it) is roughly fair-to-slightly-pessimistic; if it is
large, then even f50 understates the real drag and the prior refutations were, if anything,
too kind. We report the distribution by year and regime and state the resemblance verbatim.

NO LOOK-AHEAD / NO TUNING
-------------------------
Strikes come from ONLY the 14:00 snapshot via s6_control's own builders (same delta recon,
same 0.15 target, same 5-pt wings). Nothing here is fit to the data; there are no free knobs.
The regime label uses ONLY prior-EOD gamma+VIX via s6_matrix.DayClassifier (causal).
"""

from __future__ import annotations

import argparse
import csv
import datetime as _dt
import gc
from dataclasses import dataclass, asdict, field
from pathlib import Path

import numpy as np
import pandas as pd

import s5_intraday_data as s5
import s6_recon as recon
import s6_control as ctrl
from s6_matrix import DayClassifier

# --------------------------------------------------------------------------- #
# Output area (research; created on demand). Matches the s6 research convention.
# --------------------------------------------------------------------------- #
OUTPUT_DIR = ctrl.OUTPUT_DIR                     # backtester/output/s6_research
PARTIAL_CSV = OUTPUT_DIR / "condor_fill_realism_partial.csv"
FINAL_CSV = OUTPUT_DIR / "condor_fill_realism.csv"


# --------------------------------------------------------------------------- #
# Per-day measurement record
# --------------------------------------------------------------------------- #
@dataclass
class FillRecord:
    day: _dt.date
    measured: bool = False
    skip_reason: str = ""
    # strikes (same as the control's iron condor)
    put_short_k: float = float("nan")
    put_long_k: float = float("nan")
    call_short_k: float = float("nan")
    call_long_k: float = float("nan")
    # credits (option points, per 1-lot condor)
    mid_credit: float = float("nan")          # signed sum of leg mids
    worstside_credit: float = float("nan")    # sell shorts at bid, buy wings at ask
    # spread costs (option points)
    oneway_spread_cost: float = float("nan")  # = mid_credit - worstside_credit
    roundtrip_spread_cost: float = float("nan")  # = 2 * oneway (cross full spread in AND out)
    # spread cost as % of the mid credit
    oneway_pct_of_credit: float = float("nan")
    roundtrip_pct_of_credit: float = float("nan")
    # per-leg bid/ask widths (option points)
    put_short_width: float = float("nan")
    put_long_width: float = float("nan")
    call_short_width: float = float("nan")
    call_long_width: float = float("nan")
    shorts_width_sum: float = float("nan")    # the two shorts
    wings_width_sum: float = float("nan")     # the two wings
    # regime labels (prior-EOD, causal)
    gamma_regime: str = "unknown"
    vix_regime: str = "unknown"
    year: int = 0


def _leg_width(snap: pd.DataFrame, strike: float, right: str) -> float | None:
    """ask - bid for one leg at the snapshot, or None if unquoted."""
    q = ctrl._leg_quote(snap, strike, right)
    if q is None:
        return None
    bid, ask = q
    return ask - bid


def _load_0dte_quote(d: _dt.date) -> pd.DataFrame | None:
    """Read ONLY the 0DTE expiration slice of the day's quote parquet (kept rows).

    A pyarrow row-group filter on `expiration == d` reads ~2-3% of the ~4.8M-row daily
    parquet (0DTE is a thin slice of the whole chain), which is ~20x faster than loading
    the entire day and filtering in pandas — and gives the IDENTICAL 0DTE kept-rows (proven
    by row-count + value equality in validation). This is pure I/O narrowing; it changes
    nothing about the store-on-change semantics we honor downstream. Returns the parsed
    kept-rows frame (schema of load_day().quote) or None if the file is absent.
    """
    import pyarrow.parquet as pq
    qp = s5._quote_path(d)
    if not qp.is_file():
        return None
    exp_str = d.strftime("%Y-%m-%d")
    tbl = pq.read_table(qp, filters=[("expiration", "=", exp_str)])
    q = tbl.to_pandas()
    if q.empty:
        return q
    q["timestamp"] = pd.to_datetime(q["timestamp"])
    return q


def _entry_snapshot_from_quote(d: _dt.date, quote: pd.DataFrame) -> pd.DataFrame | None:
    """The 0DTE NBBO at the 14:00 entry minute, from a 0DTE kept-rows quote frame.

    FAST PATH — identical result to zero_dte_chain -> _snap_at, far cheaper. The store-on-
    change contract says a contract's NBBO at 14:00 is its LAST kept row at-or-before 14:00
    (forward-fill within the contract key). We compute that directly: keep only kept-rows
    timestamped at-or-before 14:00 (drop later rows => no look-ahead), floor to the minute,
    and take, per contract key, the single latest row. This reproduces the full-grid ffill
    snapshot exactly (proven by row + value equality in validation) without materializing
    all 391 minutes. Returns strike, right, bid, ask, or None if empty.
    """
    entry_minute = pd.Timestamp(_dt.datetime.combine(d, ctrl.ENTRY_TIME))
    exp_str = d.strftime("%Y-%m-%d")
    q = quote[(quote["expiration"] == exp_str) & (quote["timestamp"] <= entry_minute)]
    if q.empty:
        return None
    q = q.copy()
    q["minute"] = q["timestamp"].dt.floor("min")
    # Latest kept row per contract key at-or-before 14:00 (the forward-filled value).
    q = q.sort_values("timestamp")
    last = q.drop_duplicates(subset=s5.CONTRACT_KEY, keep="last")
    out = last[["strike", "right", "bid", "ask"]].reset_index(drop=True)
    return out if not out.empty else None


def _entry_snapshot(d: _dt.date, dd: s5.DayData) -> pd.DataFrame | None:
    """The 0DTE NBBO at the 14:00 entry minute from a full DayData (uses the fast path)."""
    return _entry_snapshot_from_quote(d, dd.quote)


def measure_day(d: _dt.date, day_data: s5.DayData | None = None,
                clf: DayClassifier | None = None) -> FillRecord:
    """Measure the 14:00 iron-condor bid/ask geometry for one day. Never raises on a
    single-day data quirk — returns a non-measured record with a skip_reason."""
    rec = FillRecord(day=d, year=d.year)
    try:
        entry_minute = pd.Timestamp(_dt.datetime.combine(d, ctrl.ENTRY_TIME))
        # Fast path: read ONLY the 0DTE slice (or reuse a preloaded DayData if given).
        if day_data is not None:
            snap = _entry_snapshot(d, day_data)
        else:
            q0 = _load_0dte_quote(d)
            if q0 is None:
                rec.skip_reason = "no quote parquet"
                return rec
            snap = _entry_snapshot_from_quote(d, q0)
        if snap is None or snap.empty:
            rec.skip_reason = "no 14:00 0dte snapshot"
            return rec
        sr = recon.recover_forward_spot(snap, entry_minute, d)
        if sr is None:
            rec.skip_reason = "spot recon failed at entry"
            return rec
        delta_tbl = recon.per_strike_delta(snap, entry_minute, d, sr.spot)

        # Reuse the control's EXACT iron-condor builder so the legs match byte-for-byte.
        build = ctrl._build_iron_condor(snap, delta_tbl, ctrl.TARGET_SHORT_DELTA)
        if build is None:
            rec.skip_reason = "could not build condor at entry"
            return rec

        rec.put_short_k = build["short_strike"]
        rec.put_long_k = build["long_strike"]
        rec.call_short_k = build["short_strike_2"]
        rec.call_long_k = build["long_strike_2"]

        # The control's honest worst-side credit is exactly build["entry_credit"]
        # (sell shorts at bid, buy wings at ask). Use it verbatim.
        rec.worstside_credit = float(build["entry_credit"])

        # Now recover each leg's (bid, ask) to compute mids and widths.
        legs = build["legs"]  # [(strike, right, side)] side +1 short(sold) / -1 long(bought)
        mid_credit = 0.0
        widths = {}
        ok = True
        for strike, right, side in legs:
            q = ctrl._leg_quote(snap, strike, right)
            if q is None:
                ok = False
                break
            bid, ask = q
            mid = 0.5 * (bid + ask)
            # signed mid: a SOLD leg (+1) receives its mid (+), a BOUGHT leg (-1) pays (-)
            mid_credit += side * mid
            widths[(strike, right)] = ask - bid
        if not ok:
            rec.skip_reason = "leg unquoted at entry"
            return rec

        rec.mid_credit = mid_credit
        # Per-leg widths, keyed by role.
        rec.put_short_width = widths[(rec.put_short_k, "PUT")]
        rec.put_long_width = widths[(rec.put_long_k, "PUT")]
        rec.call_short_width = widths[(rec.call_short_k, "CALL")]
        rec.call_long_width = widths[(rec.call_long_k, "CALL")]
        rec.shorts_width_sum = rec.put_short_width + rec.call_short_width
        rec.wings_width_sum = rec.put_long_width + rec.call_long_width

        # One-way worst-side spread cost = mid credit minus worst-side credit.
        # (Selling at bid instead of mid loses half a width per short; buying at ask instead
        #  of mid loses half a width per wing; summed = half of every leg's width. Round-trip
        #  crossing full spread in AND out pays the FULL width per leg once each way = the
        #  sum of all four widths. Both are reported.)
        rec.oneway_spread_cost = rec.mid_credit - rec.worstside_credit
        rec.roundtrip_spread_cost = sum(widths.values())  # full 4-leg spread, one crossing

        # Percent-of-credit uses the MID credit as the honest denominator (the theoretical
        # premium before any bid/ask drag). Guard tiny/nonpositive credits.
        if np.isfinite(rec.mid_credit) and rec.mid_credit > 0:
            rec.oneway_pct_of_credit = 100.0 * rec.oneway_spread_cost / rec.mid_credit
            rec.roundtrip_pct_of_credit = 100.0 * rec.roundtrip_spread_cost / rec.mid_credit

        # Regime labels (causal, prior-EOD).
        if clf is not None:
            lab = clf.classify(d)
            rec.gamma_regime = lab["gamma_regime"]
            rec.vix_regime = lab["vix_regime"]

        rec.measured = True
        return rec
    except Exception as e:
        rec.skip_reason = f"error: {type(e).__name__}: {e}"
        return rec


# --------------------------------------------------------------------------- #
# Full-history run — resumable per-day CSV, gc-safe, chunkable.
# --------------------------------------------------------------------------- #
def run_history(days: list[_dt.date] | None = None, verbose: bool = True,
                resume: bool = True, max_new_days: int | None = None) -> pd.DataFrame:
    """Measure every available 0DTE day. Appends+flushes per day (resumable), releases the
    big per-day frame each iteration (gc-safe), and stops after `max_new_days` new days if
    set (fresh-process chunk loop for very long windows)."""
    if days is None:
        days = s5.available_days()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    done_days: set[str] = set()
    if resume and PARTIAL_CSV.is_file():
        try:
            prev = pd.read_csv(PARTIAL_CSV, usecols=["day"])
            done_days = set(prev["day"].astype(str).unique())
        except Exception:
            done_days = set()
    if verbose and done_days:
        print(f"resume: {len(done_days)} days already in partial CSV; skipping them",
              flush=True)

    clf = DayClassifier()
    n = len(days)
    fieldnames = list(asdict(FillRecord(day=days[0])).keys())
    write_header = not PARTIAL_CSV.is_file()
    new_count = 0
    with open(PARTIAL_CSV, "a", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        if write_header:
            writer.writeheader()
        for i, d in enumerate(days, 1):
            if str(d) in done_days:
                continue
            if max_new_days is not None and new_count >= max_new_days:
                if verbose:
                    print(f"reached --max-new-days={max_new_days}; exiting cleanly "
                          f"(partial CSV durable).", flush=True)
                break
            # measure_day does the fast 0DTE-only parquet read itself (no full load_day).
            rec = measure_day(d, day_data=None, clf=clf)
            writer.writerow(asdict(rec))
            fh.flush()
            new_count += 1
            # Memory hygiene: drop the record + collect so RAM resets each day.
            del rec
            gc.collect()
            if verbose and (i % 25 == 0 or i == n):
                print(f"[{i}/{n}] {d} done  (new measured this run={new_count})", flush=True)

    df = pd.read_csv(PARTIAL_CSV)
    df["measured"] = df["measured"].astype(str).str.lower().isin(["true", "1"])
    df.to_csv(FINAL_CSV, index=False)
    if verbose:
        m = df[df["measured"]]
        print(f"\n{len(df)} day-rows, {len(m)} measured. Saved {FINAL_CSV}", flush=True)
    return df


# --------------------------------------------------------------------------- #
# Aggregation helpers (used by the reporter).
# --------------------------------------------------------------------------- #
_PCTS = [5, 25, 50, 75, 95]


def _dist(series: pd.Series) -> dict:
    s = pd.to_numeric(series, errors="coerce").dropna()
    if s.empty:
        return {"n": 0}
    q = np.percentile(s.to_numpy(), _PCTS)
    return {
        "n": int(len(s)),
        "median": round(float(q[2]), 1),
        "p05": round(float(q[0]), 1),
        "p25": round(float(q[1]), 1),
        "p75": round(float(q[3]), 1),
        "p95": round(float(q[4]), 1),
        "mean": round(float(s.mean()), 1),
    }


def summarize(df: pd.DataFrame, col: str = "oneway_pct_of_credit") -> dict:
    """Overall + by-year + by-regime distribution of a spread-% column (measured days only,
    with a positive mid credit so the % is meaningful)."""
    m = df[df["measured"] & (pd.to_numeric(df["mid_credit"], errors="coerce") > 0)].copy()
    out = {"overall": _dist(m[col])}
    out["by_year"] = {int(y): _dist(g[col]) for y, g in m.groupby("year")}
    out["by_gamma"] = {str(k): _dist(g[col]) for k, g in m.groupby("gamma_regime")}
    out["by_vix"] = {str(k): _dist(g[col]) for k, g in m.groupby("vix_regime")}
    out["by_regime_cell"] = {
        f"{gk}/{vk}": _dist(g[col])
        for (gk, vk), g in m.groupby(["gamma_regime", "vix_regime"])
    }
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Arm 3: SPX 0DTE condor fill realism.")
    ap.add_argument("--max-new-days", type=int, default=None,
                    help="process at most N not-yet-done days, then exit cleanly.")
    ap.add_argument("--no-resume", action="store_true", help="ignore the partial CSV.")
    ap.add_argument("--days", type=int, default=None,
                    help="limit to the FIRST N available days (validation runs).")
    args = ap.parse_args()
    _days = s5.available_days()
    if args.days is not None:
        _days = _days[: args.days]
    run_history(days=_days, resume=not args.no_resume, max_new_days=args.max_new_days)
