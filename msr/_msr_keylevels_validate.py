#!/usr/bin/env python3
"""
Validate/clean the SPX Key Levels table.

last_price / upside_risk / downside_risk / upper_pv_band / lower_pv_band /
spread are internally redundant (same as the sector table), so reconstruct the
three primaries as the median of their candidate encodings and recompute the
derived ones. last_price, gex_flip, and the three strikes are price levels that
track SPX smoothly, so a local-median spike check catches OCR errors. implied
move is a constant template value (1.26) and is dropped to NULL with a note.

Input : _msr_spx_key_levels.csv
Output: _msr_spx_key_levels_clean.csv  +  _msr_keylevels_worklist.csv
"""
import csv
import os
import statistics

ROOT = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(ROOT, "_msr_spx_key_levels.csv")
OUT = os.path.join(ROOT, "_msr_spx_key_levels_clean.csv")
WORK = os.path.join(ROOT, "_msr_keylevels_worklist.csv")
PRICE_FIELDS = ["last_price", "gex_flip", "resistance_strike", "focal_strike", "support_strike"]


def f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def med(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def main():
    rows = list(csv.DictReader(open(SRC, encoding="utf-8")))
    rows.sort(key=lambda r: r["report_date"])

    fixp = os.path.join(ROOT, "_msr_keylevels_fixes.csv")
    fixes = {}
    if os.path.exists(fixp):
        for fx in csv.DictReader(open(fixp, encoding="utf-8")):
            fixes.setdefault(fx["report_date"], {})[fx["field"]] = fx["value"]

    # robust baseline for last (for candidate gating)
    lasts = [f(r["last_price"]) for r in rows]
    base = med([x for x in lasts if x])

    for r in rows:
        last, up, dn = f(r["last_price"]), f(r["upside_risk_pct"]), f(r["downside_risk_pct"])
        U, Lo, spr = f(r["upper_pv_band"]), f(r["lower_pv_band"]), f(r["spread_pct"])
        flags = []

        # reconstruct last from [ocr_last, upper-implied, lower-implied]
        cands = [last]
        if U and up is not None:
            cands.append(U / (1 + up / 100))
        if Lo and dn is not None:
            cands.append(Lo / (1 + dn / 100))
        cands = [c for c in cands if c and (not base or 0.5 * base <= c <= 2 * base)]
        if cands:
            last = round(med(cands), 2)
        # reconstruct up / dn (direct, band-implied, spread)
        ucands = [up]
        if U and last:
            ucands.append(round((U / last - 1) * 100, 2))
        if spr is not None and dn is not None:
            ucands.append(round(spr + dn, 2))
        ucands = [c for c in ucands if c is not None and -10 <= c <= 30]
        if ucands:
            up = med(ucands)
        dcands = [dn]
        if Lo and last:
            dcands.append(round((Lo / last - 1) * 100, 2))
        if spr is not None and up is not None:
            dcands.append(round(up - spr, 2))
        dcands = [c for c in dcands if c is not None and -30 <= c <= 10]
        if dcands:
            dn = med(dcands)

        r["last_price"] = last
        r["upside_risk_pct"] = round(up, 2) if up is not None else ""
        r["downside_risk_pct"] = round(dn, 2) if dn is not None else ""
        r["upper_pv_band"] = round(last * (1 + up / 100)) if (last and up is not None) else ""
        r["lower_pv_band"] = round(last * (1 + dn / 100)) if (last and dn is not None) else ""
        r["spread_pct"] = round(up - dn, 2) if (up is not None and dn is not None) else ""
        # implied move is a constant template value -> null it out
        if f(r["implied_move_pct"]) == 1.26:
            r["implied_move_pct"] = ""

        # apply vision fixes AFTER reconstruction (they are the final authority;
        # reconstruction can otherwise re-poison a primary from a bad band).
        fx = fixes.get(r["report_date"], {})
        for fld, v in fx.items():
            r[fld] = v
        if fx:
            last = f(r["last_price"]); up = f(r["upside_risk_pct"]); dn = f(r["downside_risk_pct"])
            if last and up is not None:
                r["upper_pv_band"] = round(last * (1 + up / 100))
            if last and dn is not None:
                r["lower_pv_band"] = round(last * (1 + dn / 100))
        r["flag"] = ";".join(flags)

    worklist = []
    # last_price & gex_flip: local-median spike check (they ARE the price level)
    for fld in ["last_price", "gex_flip"]:
        vals = [f(r[fld]) for r in rows]
        for i, r in enumerate(rows):
            v = vals[i]
            if v is None:
                if fld == "last_price":
                    r["flag"] = ";".join(sorted(set([t for t in r["flag"].split(";") if t] + [f"missing:{fld}"])))
                continue
            win = [x for j, x in enumerate(vals) if 0 < abs(j - i) <= 4 and x is not None]
            if len(win) >= 3:
                mloc = statistics.median(win)
                if mloc and abs(v / mloc - 1) > 0.20:
                    r["flag"] = ";".join(sorted(set([t for t in r["flag"].split(";") if t] + [f"anomaly:{fld}"])))
    # strikes: absolute check vs last price (near-the-money levels, within ~8%).
    # This catches digit-truncations directly without cascading on the median.
    for fld in ["resistance_strike", "focal_strike", "support_strike"]:
        for r in rows:
            v, last = f(r[fld]), f(r["last_price"])
            if v is None:
                r["flag"] = ";".join(sorted(set([t for t in r["flag"].split(";") if t] + [f"missing:{fld}"])))
            elif last and abs(v / last - 1) > 0.08:
                r["flag"] = ";".join(sorted(set([t for t in r["flag"].split(";") if t] + [f"anomaly:{fld}"])))

    for r in rows:
        if any(t.startswith(("anomaly:", "missing:")) for t in r["flag"].split(";") if t):
            worklist.append(r)

    fields = list(rows[0].keys())
    with open(OUT, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields); w.writeheader(); w.writerows(rows)
    with open(WORK, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields); w.writeheader(); w.writerows(worklist)

    clean = sum(1 for r in rows if not r["flag"])
    print(f"rows: {len(rows)}  clean: {clean} ({100*clean/len(rows):.1f}%)")
    print(f"worklist (anomaly/missing): {len(worklist)}")
    for r in worklist:
        print(f"  {r['report_date']}: {r['flag']}")
    print(f"-> {OUT}")


if __name__ == "__main__":
    main()
