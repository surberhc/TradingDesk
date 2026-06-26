#!/usr/bin/env python3
"""
Extract the 'SPX Realized Vol Data In Sample' table from every MSR report.

Table lives on the right side of the 'Systematic Positioning' page. 10 rows
(T+1..T+10), each:  dropoff | 1m_date | 1m_drop% | 3m_date | 3m_drop%

The rows have a rigid content shape (exactly two dates + two percentages), so
we parse by CONTENT TYPE (dates vs percents, left-to-right) rather than fragile
column anchors. Dropoff is positional (row order). A given calendar date's daily
return is fixed and recurs across many reports (rolling off the 1m window ~1mo
later, the 3m window ~3mo later), so a later consensus pass can cross-validate
drop% values by date across the whole corpus.

Output: _msr_realized_vol.csv (long format, one row per dropoff per report).
"""
import csv
import importlib.util
import os
import re
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("lib", os.path.join(ROOT, "_msr_ocr_lib.py"))
lib = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lib)
import fitz

INVENTORY = os.path.join(ROOT, "_msr_canonical_daily.csv")
OUTPUT = os.path.join(ROOT, "_msr_realized_vol.csv")

DATE_RE = re.compile(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})")
PCT_RE = re.compile(r"-?\d{1,3}\.?\d*\s*%")


def norm_date(s):
    m = DATE_RE.search(s.replace(" ", ""))
    if not m:
        return None
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if not (2024 <= y <= 2027 and 1 <= mo <= 12 and 1 <= d <= 31):
        return None
    return f"{y:04d}-{mo:02d}-{d:02d}"


def parse_pct(s):
    s = s.replace(" ", "")
    m = re.search(r"-?\d{1,3}\.?\d*", s.replace("%", ""))
    if not m:
        return None
    v = float(m.group())
    # OCR sometimes drops the leading minus or a decimal; gate to plausible range
    return v if -15 <= v <= 15 else None


def process(doc, date):
    p = lib.find_page(doc, "Systematic Positioning")
    if p is None:
        return [], "no_syspos_page"
    boxes, w, h = lib.ocr_crop(doc, p, 0.50, 0.40, 1.0, 0.86)
    rows = lib.cluster_rows(boxes, y_tol=16)

    # find the header row ('Dropoff' / 'Drop %')
    start = None
    for i, row in enumerate(rows):
        joined = " ".join(b["text"].lower() for b in row)
        if "dropoff" in joined or ("drop" in joined and "dates" in joined):
            start = i + 1
            break
    if start is None:
        return [], "header_not_found"

    results = []
    for row in rows[start:]:
        toks = sorted(row, key=lambda b: b["x"])
        dates, pcts = [], []
        for b in toks:
            d = norm_date(b["text"])
            if d:
                dates.append((b["x"], d))
                continue
            if "%" in b["text"] or PCT_RE.search(b["text"]):
                v = parse_pct(b["text"])
                if v is not None:
                    pcts.append((b["x"], v))
        # a valid data row has both dates and both percents
        if len(dates) >= 2 and len(pcts) >= 2:
            dates.sort()
            pcts.sort()
            results.append({
                "date_1m": dates[0][1], "drop_1m_pct": pcts[0][1],
                "date_3m": dates[1][1], "drop_3m_pct": pcts[1][1],
                "partial": "",
            })
        elif dates or pcts:
            # keep partial rows flagged for repair/vision
            results.append({
                "date_1m": dates[0][1] if len(dates) > 0 else "",
                "drop_1m_pct": pcts[0][1] if len(pcts) > 0 else "",
                "date_3m": dates[1][1] if len(dates) > 1 else "",
                "drop_3m_pct": pcts[1][1] if len(pcts) > 1 else "",
                "partial": "partial",
            })
        if len(results) >= 10:
            break

    out = []
    for i, r in enumerate(results, 1):
        flags = []
        if r["partial"]:
            flags.append("partial_row")
        for k in ("date_1m", "date_3m"):
            if not r[k]:
                flags.append(f"missing:{k}")
        for k in ("drop_1m_pct", "drop_3m_pct"):
            if r[k] == "":
                flags.append(f"missing:{k}")
        out.append({
            "report_date": date,
            "dropoff": f"T+{i}",
            "date_1m": r["date_1m"], "drop_1m_pct": r["drop_1m_pct"],
            "date_3m": r["date_3m"], "drop_3m_pct": r["drop_3m_pct"],
            "src_page": p + 1,
            "flag": ";".join(flags),
        })
    status = "ok" if len(out) == 10 else f"rows={len(out)}"
    return out, status


def main():
    rows_in = list(csv.DictReader(open(INVENTORY, encoding="utf-8-sig")))
    if len(sys.argv) > 1:
        rows_in = rows_in[:int(sys.argv[1])]
    all_out = []
    bad = []
    for n, r in enumerate(rows_in, 1):
        date = r["ReportDate"]
        try:
            doc = fitz.open(r["FullPath"])
            try:
                recs, status = process(doc, date)
            finally:
                doc.close()
        except Exception as e:
            recs, status = [], f"error:{type(e).__name__}"
        all_out.extend(recs)
        if status != "ok":
            bad.append((date, status))
        print(f"[{n}/{len(rows_in)}] {date}: {status} rows={len(recs)}", flush=True)

    fields = ["report_date", "dropoff", "date_1m", "drop_1m_pct",
              "date_3m", "drop_3m_pct", "src_page", "flag"]
    with open(OUTPUT, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(all_out)
    print("\n==== SUMMARY ====")
    print(f"reports: {len(rows_in)}  rows: {len(all_out)}  reports != 10 rows: {len(bad)}")
    for b in bad[:25]:
        print("   ", b)
    print(f"-> {OUTPUT}")


if __name__ == "__main__":
    main()
