#!/usr/bin/env python3
"""
Time-series validation/repair for the sector vol-bands dataset.

The noisy band cross-checks (chk:*) produce many false positives because the
redundant $ band cells are small and OCR-garbled even when the row is correct.
A ticker's price / realized-vol / beta instead move smoothly day-to-day, so a
robust per-ticker time series exposes the genuine OCR errors (dropped decimals
-> ~10x spikes, garbles -> wild values) with almost no false positives.

For each ticker and each smoothly-varying column we:
  * compare every value to a robust local median (centered window),
  * auto-repair obvious dropped/extra decimals (ratio ~10x / ~100x -> rescale),
  * flag values that remain implausibly far from the local median,
  * leave clean values untouched.

Then upper/lower/spread are recomputed from the cleaned primaries. Output:
_msr_sector_bands_clean.csv  (+ a vision worklist of residual anomalies).
"""
import csv
import os
import statistics
from collections import defaultdict

ROOT = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(ROOT, "_msr_sector_bands.csv")
OUT = os.path.join(ROOT, "_msr_sector_bands_clean.csv")
WORKLIST = os.path.join(ROOT, "_msr_sector_vision_worklist.csv")

# column -> (max plausible day-to-day relative jump for an anomaly call)
# last prices barely move; rvol/beta vary more.
SMOOTH = {
    "last": 0.35,
    "upside_pct": None,   # bounded check instead (handled separately)
    "downside_pct": None,
    "rvol_1m": 0.80,
    "beta_2y": 0.60,
}


def f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def local_median(series, i, win=4):
    lo, hi = max(0, i - win), min(len(series), i + win + 1)
    vals = [v for j, v in enumerate(series) if lo <= j < hi and j != i and v is not None]
    return statistics.median(vals) if len(vals) >= 3 else None


def try_decimal_repair(v, med):
    """If v is ~10x or ~100x (or ~1/10) the local median, rescale."""
    if v is None or med is None or med == 0:
        return None
    for factor in (10.0, 100.0, 0.1, 0.01):
        if abs((v / factor) / med - 1) < 0.15:
            return round(v / factor, 4)
    return None


def main():
    rows = list(csv.DictReader(open(SRC, encoding="utf-8")))
    # index rows by ticker, ordered by date
    by_ticker = defaultdict(list)
    for r in rows:
        by_ticker[r["asset"]].append(r)
    for t in by_ticker:
        by_ticker[t].sort(key=lambda r: r["report_date"])

    new_flags = {id(r): [] for r in rows}
    repairs = 0
    anomalies = 0

    for ticker, series in by_ticker.items():
        for col, jump in SMOOTH.items():
            vals = [f(r[col]) for r in series]
            for i, r in enumerate(series):
                v = vals[i]
                med = local_median(vals, i)
                if v is None:
                    if col in ("last",):  # critical column missing
                        new_flags[id(r)].append(f"missing:{col}")
                    continue
                if med is None:
                    continue
                # decimal repair first
                if jump is not None and abs(v / med - 1) > jump:
                    fixed = try_decimal_repair(v, med)
                    if fixed is not None:
                        r[col] = fixed
                        vals[i] = fixed
                        new_flags[id(r)].append(f"repaired:{col}")
                        repairs += 1
                    else:
                        new_flags[id(r)].append(f"anomaly:{col}")
                        anomalies += 1

        # bounded sanity for the percentage columns (already mostly clean)
        for col, lo, hi in [("upside_pct", 0, 25), ("downside_pct", -25, 0)]:
            for r in series:
                v = f(r[col])
                if v is not None and not (lo <= v <= hi):
                    new_flags[id(r)].append(f"anomaly:{col}")
                    anomalies += 1

    # recompute derived from cleaned primaries; assemble output
    out_rows = []
    worklist = []
    for r in rows:
        last, up, dn = f(r["last"]), f(r["upside_pct"]), f(r["downside_pct"])
        if last is not None and up is not None:
            r["upper_pvb"] = round(last * (1 + up / 100.0))
        if last is not None and dn is not None:
            r["lower_pvb"] = round(last * (1 + dn / 100.0))
        if up is not None and dn is not None:
            r["spread_pct"] = round(up - dn, 2)
        ts_flags = new_flags[id(r)]
        # carry only the genuinely-actionable original flags (drop noisy chk:*)
        orig = [t for t in r["flag"].split(";")
                if t and (t.startswith("missing:") or t.startswith("range:")
                          or t.startswith("bad:"))]
        # missing:rvol/beta from OCR are real gaps worth keeping
        combined = sorted(set(ts_flags + [o for o in orig
                                          if o in ("missing:rvol", "missing:beta")]))
        r["flag"] = ";".join(combined)
        out_rows.append(r)
        if any(t.startswith("anomaly:") or t == "missing:last" for t in combined):
            worklist.append(r)

    fields = list(rows[0].keys())
    with open(OUT, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(out_rows)
    with open(WORKLIST, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(worklist)

    n = len(out_rows)
    vision = len(worklist)
    has_rvolbeta = sum(1 for r in out_rows if "missing:rvol" in r["flag"] or "missing:beta" in r["flag"])
    print(f"rows: {n}")
    print(f"  auto-repaired (decimal) : {repairs}")
    print(f"  anomalies flagged       : {anomalies}")
    print(f"  rows on VISION worklist : {vision} ({100*vision/n:.1f}%)  -> {os.path.basename(WORKLIST)}")
    print(f"  rows missing rvol/beta  : {has_rvolbeta}")
    print(f"-> {OUT}")


if __name__ == "__main__":
    main()
