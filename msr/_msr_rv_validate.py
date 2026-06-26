#!/usr/bin/env python3
"""
Cross-report consensus validation for the realized-vol dataset.

A calendar date's SPX daily return is a single fixed number, but it appears in
many reports (in the 1m column ~1 month later, the 3m column ~3 months later).
So pooling every (date, drop%) observation across all 281 reports lets us derive
a consensus return per date and flag/correct the rare OCR disagreement.

Also validates intra-report date ordering (rows must be strictly increasing).

Inputs : _msr_realized_vol.csv
Outputs: _msr_realized_vol_clean.csv      (per-report table, corrected + flagged)
         _msr_spx_daily_returns.csv       (bonus: consensus return per date)
"""
import csv
import os
from collections import defaultdict, Counter

ROOT = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(ROOT, "_msr_realized_vol.csv")
OUT = os.path.join(ROOT, "_msr_realized_vol_clean.csv")
SERIES = os.path.join(ROOT, "_msr_spx_daily_returns.csv")


def f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def main():
    rows = list(csv.DictReader(open(SRC, encoding="utf-8")))

    # pool observations per date from both columns
    obs = defaultdict(list)
    for r in rows:
        for dcol, vcol in (("date_1m", "drop_1m_pct"), ("date_3m", "drop_3m_pct")):
            d, v = r[dcol], f(r[vcol])
            if d and v is not None:
                obs[d].append(round(v, 1))

    consensus = {}
    for d, vals in obs.items():
        c = Counter(vals)
        top, n = c.most_common(1)[0]
        # require either >1 supporting obs or a single unambiguous obs
        consensus[d] = (top, n, len(vals))

    # vision-verified date overrides take priority over the OCR majority, which
    # is wrong for dates where a systematic OCR misread (e.g. 0.9->6.0) dominated
    # the vote. Force these to ground truth everywhere.
    overrides = {}
    ovpath = os.path.join(ROOT, "_msr_rv_date_overrides.csv")
    if os.path.exists(ovpath):
        for r in csv.DictReader(open(ovpath, encoding="utf-8")):
            v = f(r["return_pct"])
            if r["date"] and v is not None:
                overrides[r["date"]] = v
                consensus[r["date"]] = (round(v, 1), 999, 999)  # unbeatable support

    corrected = 0
    filled = 0
    out = []
    for r in rows:
        flags = [t for t in r["flag"].split(";") if t]
        for dcol, vcol, tag in (("date_1m", "drop_1m_pct", "1m"),
                                ("date_3m", "drop_3m_pct", "3m")):
            d, v = r[dcol], f(r[vcol])
            if not d or d not in consensus:
                continue
            cons, n, total = consensus[d]
            if v is None and n >= 2:
                # value dropped by OCR but the date is known -> fill from the
                # consensus return for that calendar date.
                r[vcol] = cons
                flags = [t for t in flags if t not in (f"missing:{vcol}", "partial_row")]
                flags.append(f"consensus_fill:{tag}")
                filled += 1
            elif v is not None and n >= 2 and abs(round(v, 1) - cons) > 0.11:
                # consensus is well-supported and disagrees -> correct it
                r[vcol] = cons
                flags.append(f"consensus_fix:{tag}")
                corrected += 1
        # drop a now-stale partial_row tag if nothing is actually missing anymore
        if "partial_row" in flags and all(r[c] not in ("", None)
                                          for c in ("date_1m", "drop_1m_pct", "date_3m", "drop_3m_pct")):
            flags = [t for t in flags if t != "partial_row"]
        r["flag"] = ";".join(sorted(set(flags)))
        out.append(r)

    # ---- derive missing dates from the master trading-day calendar ----
    # 3m/1m dates within a report are consecutive trading days, so a dropped
    # date is the unique master date strictly between its row neighbours
    # (or the immediate predecessor/successor at the ends).
    master = sorted(consensus.keys())
    mset = master
    import bisect
    derived = 0
    byrep0 = defaultdict(list)
    for r in out:
        byrep0[r["report_date"]].append(r)
    for rep, rs in byrep0.items():
        rs.sort(key=lambda r: int(r["dropoff"].replace("T+", "")))
        for col, vcol, tag in (("date_1m", "drop_1m_pct", "1m"),
                               ("date_3m", "drop_3m_pct", "3m")):
            # null any date that breaks strict ordering (a misread date, often a
            # duplicate of the row's other column) so it gets re-derived below.
            dates = [r[col] for r in rs]
            for i, r in enumerate(rs):
                d = r[col]
                if not d:
                    continue
                prevs = [x for x in dates[:i] if x]
                nexts = [x for x in dates[i + 1:] if x]
                if (prevs and d <= prevs[-1]) or (nexts and d >= nexts[0]):
                    r[col] = ""
                    r["flag"] = ";".join(sorted(set(
                        [t for t in r["flag"].split(";") if t] + [f"reorder:{col}"])))
            for i, r in enumerate(rs):
                if r[col]:
                    continue
                prev_d = rs[i - 1][col] if i > 0 else None
                next_d = rs[i + 1][col] if i + 1 < len(rs) else None
                cand = None
                if prev_d and next_d:
                    between = [m for m in master if prev_d < m < next_d]
                    if len(between) == 1:
                        cand = between[0]
                elif next_d:  # first row: master date just before next
                    j = bisect.bisect_left(mset, next_d)
                    if j > 0:
                        cand = mset[j - 1]
                elif prev_d:  # last row: master date just after prev
                    j = bisect.bisect_right(mset, prev_d)
                    if j < len(mset):
                        cand = mset[j]
                if cand:
                    r[col] = cand
                    # now that the date is known, fill OR correct its drop from
                    # consensus (the consensus loop ran before this date existed)
                    if cand in consensus and consensus[cand][1] >= 2:
                        cv = consensus[cand][0]
                        cur = f(r[vcol])
                        if cur is None or abs(round(cur, 1) - cv) > 0.11:
                            r[vcol] = cv
                    flags = [t for t in r["flag"].split(";") if t
                             and t not in (f"missing:{col}", f"missing:{vcol}", "partial_row")]
                    flags.append(f"derived:{col}")
                    r["flag"] = ";".join(sorted(set(flags)))
                    derived += 1

    # intra-report date ordering check (per column)
    byrep = defaultdict(list)
    for r in out:
        byrep[r["report_date"]].append(r)
    order_issues = 0
    for rep, rs in byrep.items():
        rs.sort(key=lambda r: r["dropoff"].replace("T+", "").zfill(2))
        for col in ("date_1m", "date_3m"):
            prev = None
            for r in rs:
                d = r[col]
                if d and prev and d <= prev:
                    if "order:" + col not in r["flag"]:
                        r["flag"] = ";".join(sorted(set([t for t in r["flag"].split(";") if t] + [f"order:{col}"])))
                        order_issues += 1
                if d:
                    prev = d

    fields = list(rows[0].keys())
    with open(OUT, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields); w.writeheader(); w.writerows(out)

    # bonus: consensus daily-return series
    with open(SERIES, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["date", "spx_daily_return_pct", "n_observations", "agreement"])
        for d in sorted(consensus):
            top, n, total = consensus[d]
            agree = "vision-verified" if d in overrides else f"{n}/{total}"
            w.writerow([d, top, (total if d not in overrides else len(obs.get(d, []))), agree])

    n = len(out)
    clean = sum(1 for r in out if not r["flag"])
    print(f"rows: {n}")
    print(f"  clean: {clean} ({100*clean/n:.1f}%)")
    print(f"  consensus auto-fixes: {corrected}")
    print(f"  consensus fills (missing): {filled}")
    print(f"  dates derived from calendar: {derived}")
    print(f"  date-order issues   : {order_issues}")
    print(f"  unique dates in series: {len(consensus)}")
    print(f"-> {OUT}\n-> {SERIES}")


if __name__ == "__main__":
    main()
