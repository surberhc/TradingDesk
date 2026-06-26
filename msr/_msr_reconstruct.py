#!/usr/bin/env python3
"""
Redundancy reconstruction for the sector vol-bands dataset.

Each row carries six noisy OCR reads for three underlying values:
    last, upside%, downside%   (the unknowns)
    upper$ = last*(1+up/100)   lower$ = last*(1+dn/100)   spread% = up - dn

So every primary has THREE independent candidate encodings; taking the median
auto-rejects any single garbled cell. We iterate to convergence (the equations
are coupled), gate candidates for plausibility against a robust per-ticker
baseline (bands are usually clean), and flag only rows where too many cells are
garbled to reconstruct. A final time-series check catches anything residual.

Inputs : _msr_sector_bands.csv   (raw OCR, incl. ocr_upper/ocr_lower/ocr_spread)
Outputs: _msr_sector_bands_clean.csv  +  _msr_sector_vision_worklist.csv
"""
import csv
import os
import statistics
from collections import defaultdict

ROOT = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(ROOT, "_msr_sector_bands.csv")
OUT = os.path.join(ROOT, "_msr_sector_bands_clean.csv")
WORK = os.path.join(ROOT, "_msr_sector_vision_worklist.csv")


def f(x):
    try:
        v = float(x)
        return v
    except (TypeError, ValueError):
        return None


def med(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def reconstruct_row(ocr_last, ocr_up, ocr_dn, ocr_up_b, ocr_lo_b, ocr_spr, base_last):
    """Iteratively solve last/up/dn as the median of their candidate encodings.
    base_last = robust per-ticker price baseline used only to gate candidates."""
    last, up, dn = ocr_last, ocr_up, ocr_dn

    def plausible_last(v):
        if v is None or v <= 0:
            return False
        if base_last:
            return 0.4 * base_last <= v <= 2.5 * base_last
        return v < 1e5

    for _ in range(6):
        # last candidates: direct, and back-solved from each band
        cands = [ocr_last]
        if ocr_up_b and up is not None:
            cands.append(ocr_up_b / (1 + up / 100.0))
        if ocr_lo_b and dn is not None:
            cands.append(ocr_lo_b / (1 + dn / 100.0))
        cands = [c for c in cands if plausible_last(c)]
        new_last = med(cands) if cands else last

        # upside candidates: direct, band-implied, spread+dn
        ucands = [ocr_up]
        if ocr_up_b and new_last:
            ucands.append((ocr_up_b / new_last - 1) * 100)
        if ocr_spr is not None and dn is not None:
            ucands.append(ocr_spr + dn)
        ucands = [c for c in ucands if c is not None and -2 <= c <= 40]
        new_up = med(ucands) if ucands else up

        # downside candidates: direct, band-implied, up-spread
        dcands = [ocr_dn]
        if ocr_lo_b and new_last:
            dcands.append((ocr_lo_b / new_last - 1) * 100)
        if ocr_spr is not None and new_up is not None:
            dcands.append(new_up - ocr_spr)
        dcands = [c for c in dcands if c is not None and -40 <= c <= 2]
        new_dn = med(dcands) if dcands else dn

        if (new_last, new_up, new_dn) == (last, up, dn):
            last, up, dn = new_last, new_up, new_dn
            break
        last, up, dn = new_last, new_up, new_dn

    return last, up, dn


def robust_baseline(vals):
    """Median of values within a sane spread, ignoring gross garbles."""
    vals = [v for v in vals if v is not None and v > 0]
    if not vals:
        return None
    m = statistics.median(vals)
    near = [v for v in vals if 0.3 * m <= v <= 3 * m]
    return statistics.median(near) if near else m


# OCR occasionally drops/doubles a letter in a sector ticker; normalise these
# back to the standard symbol so they join the correct time series.
TICKER_NORM = {"IWWM": "IWM", "IM": "IWM", "XI": "XLI", "XK": "XLK",
               "XU": "XLU", "XC": "XLC", "XB": "XLB", "XE": "XLE",
               "XV": "XLV", "XY": "XLY", "XP": "XLP", "XF": "XLF"}


def main():
    rows = list(csv.DictReader(open(SRC, encoding="utf-8")))
    for r in rows:
        r["asset"] = TICKER_NORM.get(r["asset"], r["asset"])
    by_ticker = defaultdict(list)
    for r in rows:
        by_ticker[r["asset"]].append(r)
    for t in by_ticker:
        by_ticker[t].sort(key=lambda r: r["report_date"])

    n_recon = n_flag = 0
    out_rows = []
    worklist = []

    for ticker, series in by_ticker.items():
        # baseline price from the band-implied prices (bands usually clean)
        band_prices = []
        for r in series:
            ou, ol = f(r["ocr_upper"]), f(r["ocr_lower"])
            up, dn = f(r["upside_pct"]), f(r["downside_pct"])
            if ou and up is not None:
                band_prices.append(ou / (1 + up / 100.0))
            if ol and dn is not None:
                band_prices.append(ol / (1 + dn / 100.0))
            if f(r["last"]):
                band_prices.append(f(r["last"]))
        base_last = robust_baseline(band_prices)

        # rvol/beta time-series medians for residual outlier checks
        rvols = [f(r["rvol_1m"]) for r in series]
        betas = [f(r["beta_2y"]) for r in series]
        rv_base = robust_baseline([x for x in rvols])
        bt_base = robust_baseline([x for x in betas])

        for r in series:
            last, up, dn = reconstruct_row(
                f(r["last"]), f(r["upside_pct"]), f(r["downside_pct"]),
                f(r["ocr_upper"]), f(r["ocr_lower"]), f(r["ocr_spread"]), base_last)

            flags = []
            # confidence: did the candidates corroborate?
            if last is None or (base_last and not (0.4 * base_last <= last <= 2.5 * base_last)):
                flags.append("anomaly:last")
            if up is None or not (-2 <= up <= 40):
                flags.append("anomaly:upside")
            if dn is None or not (-40 <= dn <= 2):
                flags.append("anomaly:downside")

            rvol, beta = f(r["rvol_1m"]), f(r["beta_2y"])
            if rvol is None:
                flags.append("missing:rvol")
            elif rv_base and not (0.25 * rv_base <= rvol <= 4 * rv_base):
                flags.append("anomaly:rvol")
            # beta_2y is a 2-year regression -> near-constant per ticker, so a
            # missing/garbled beta is reliably filled from the ticker baseline
            # (more accurate than vision). Mark it 'filled:beta', not a worklist
            # anomaly.
            if bt_base is not None and (beta is None or
                                        abs(beta - bt_base) > max(0.6, 0.8 * abs(bt_base))):
                beta = round(bt_base, 2)
                flags.append("filled:beta")
            elif beta is None:
                flags.append("missing:beta")

            r["last"] = round(last, 2) if last is not None else ""
            r["upside_pct"] = round(up, 2) if up is not None else ""
            r["downside_pct"] = round(dn, 2) if dn is not None else ""
            r["upper_pvb"] = round(last * (1 + up / 100.0)) if (last is not None and up is not None) else ""
            r["lower_pvb"] = round(last * (1 + dn / 100.0)) if (last is not None and dn is not None) else ""
            r["spread_pct"] = round(up - dn, 2) if (up is not None and dn is not None) else ""
            r["rvol_1m"] = round(rvol, 2) if rvol is not None else ""
            r["beta_2y"] = round(beta, 2) if beta is not None else ""
            r["flag"] = ";".join(flags)
            out_rows.append(r)
            if flags:
                n_flag += 1
            if any(t.startswith("anomaly:") or t in ("missing:rvol", "missing:last")
                   for t in flags):
                worklist.append(r)

    # local-spike check on `last`: a price that deviates >20% from its own
    # local median is an OCR spike (prices don't move that much day-to-day);
    # the global range test misses these. Smooth trends are unaffected because
    # each point is compared to its immediate neighbours.
    byt = defaultdict(list)
    for r in out_rows:
        byt[r["asset"]].append(r)
    for t, ser in byt.items():
        ser.sort(key=lambda r: r["report_date"])
        lasts = [f(r["last"]) for r in ser]
        for i, r in enumerate(ser):
            v = lasts[i]
            if v is None:
                continue
            win = [x for j, x in enumerate(lasts) if 0 < abs(j - i) <= 4 and x is not None]
            if len(win) >= 3:
                med = statistics.median(win)
                if med and abs(v / med - 1) > 0.20 and "recovered:last" not in r["flag"] \
                        and "anomaly:last" not in r["flag"]:
                    r["flag"] = ";".join(sorted(set(
                        [tg for tg in r["flag"].split(";") if tg] + ["anomaly:last"])))
                    if r not in worklist:
                        worklist.append(r)

    out_rows.sort(key=lambda r: (r["report_date"], r["asset"]))
    worklist.sort(key=lambda r: (r["report_date"], r["asset"]))
    fields = list(rows[0].keys())
    with open(OUT, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields); w.writeheader(); w.writerows(out_rows)
    with open(WORK, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields); w.writeheader(); w.writerows(worklist)

    n = len(out_rows)
    clean = sum(1 for r in out_rows if not r["flag"])
    from collections import Counter
    tok = Counter(t for r in out_rows for t in r["flag"].split(";") if t)
    print(f"rows: {n}")
    print(f"  clean: {clean} ({100*clean/n:.1f}%)")
    print(f"  flagged: {n-clean} ({100*(n-clean)/n:.1f}%)")
    print(f"  vision worklist (value anomalies): {len(worklist)} ({100*len(worklist)/n:.1f}%)")
    print("  flag tokens:", dict(tok.most_common()))
    print(f"-> {OUT}\n-> {WORK}")


if __name__ == "__main__":
    main()
