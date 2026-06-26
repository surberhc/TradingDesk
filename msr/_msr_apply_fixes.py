#!/usr/bin/env python3
"""
Apply auditable manual (vision) corrections to the clean sector dataset.

Reads _msr_manual_fixes.csv (report_date, asset, field, value, note), overwrites
the matching cell in _msr_sector_bands_clean.csv, recomputes derived band/spread
columns if a primary changed, and rewrites that row's flag to drop the resolved
issue and record 'vision:<field>'. Re-runnable and idempotent.
"""
import csv
import os

ROOT = os.path.dirname(os.path.abspath(__file__))
CLEAN = os.path.join(ROOT, "_msr_sector_bands_clean.csv")
FIXES = os.path.join(ROOT, "_msr_manual_fixes.csv")


def f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def main():
    rows = list(csv.DictReader(open(CLEAN, encoding="utf-8")))
    idx = {(r["report_date"], r["asset"]): r for r in rows}
    fixes = list(csv.DictReader(open(FIXES, encoding="utf-8")))

    applied = 0
    missing = []
    for fx in fixes:
        key = (fx["report_date"], fx["asset"])
        r = idx.get(key)
        if r is None:
            missing.append(key)
            continue
        field = fx["field"]
        r[field] = fx["value"]
        # recompute derived if a primary changed
        last, up, dn = f(r["last"]), f(r["upside_pct"]), f(r["downside_pct"])
        if last is not None and up is not None:
            r["upper_pvb"] = round(last * (1 + up / 100.0))
        if last is not None and dn is not None:
            r["lower_pvb"] = round(last * (1 + dn / 100.0))
        if up is not None and dn is not None:
            r["spread_pct"] = round(up - dn, 2)
        # rewrite flag: drop the resolved missing/anomaly for this field, add
        # vision tag. Flag tokens use short names (rvol/beta/upside/downside/last)
        # while fields use full names (rvol_1m/beta_2y/upside_pct/...).
        short = {"rvol_1m": "rvol", "beta_2y": "beta", "upside_pct": "upside",
                 "downside_pct": "downside", "last": "last"}.get(field, field)
        keep = [t for t in r["flag"].split(";")
                if t and not (t.split(":")[-1] == short and
                              (t.startswith("missing:") or t.startswith("anomaly:")))]
        keep.append(f"vision:{short}")
        r["flag"] = ";".join(sorted(set(keep)))
        applied += 1

    fields = list(rows[0].keys())
    with open(CLEAN, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

    n = len(rows)
    remaining = sum(1 for r in rows if any(t.startswith(("anomaly:", "missing:"))
                                           for t in r["flag"].split(";")))
    print(f"applied {applied} fixes; {len(missing)} unmatched keys {missing[:5]}")
    print(f"rows still carrying anomaly/missing: {remaining} ({100*remaining/n:.1f}%)")


if __name__ == "__main__":
    main()
