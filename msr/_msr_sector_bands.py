#!/usr/bin/env python3
"""
Extract the 'Sector Probable Volatility Bands' table from every MSR report.

Strategy (hybrid OCR per the agreed plan):
  1. Locate the Sector PVB page by OCR'ing each Probable-Volatility-Bands page
     title and picking the one containing 'Sector' (works for both the 9-page
     and 11-page formats; the 11-page Top-Stocks page is ignored).
  2. OCR the bottom table region at 300 dpi.
  3. Assign every OCR box to a column by nearest header-anchor x (robust to
     dropped cells, which otherwise shift a naive row join).
  4. Validate arithmetically: the table is internally redundant -
        upper  ~= last * (1 + upside%/100)
        lower  ~= last * (1 + downside%/100)
        spread ~= upside% - downside%
     Rows whose OCR'd upper/lower agree with the recomputed values are trusted;
     others are flagged for a vision fix. Derived columns are recomputed from
     the (more reliable) primaries; beta with a dropped decimal is repaired.

Output: _msr_sector_bands.csv (long format, one row per asset per report) with
raw OCR preserved and a per-row `flag` column listing what failed.
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
OUTPUT = os.path.join(ROOT, "_msr_sector_bands.csv")

COLS = ["asset", "last", "lower", "upper", "upside", "downside", "spread", "rvol_1m", "beta_2y"]
# header keyword -> canonical column index, by x-anchor
HEADER_KEYS = [
    ("asset", 0), ("last", 1), ("lower", 2), ("upper", 3),
    ("upside", 4), ("downside", 5), ("spread", 6), ("rvol", 7),
    ("1m", 7), ("beta", 8), ("2y", 8),
]


def _title_has_sector(doc, pidx):
    boxes, _, _ = lib.ocr_crop(doc, pidx, 0.0, 0.06, 1.0, 0.16)
    title = " ".join(b["text"] for b in sorted(boxes, key=lambda b: b["x"])).lower()
    return "sector" in title


def locate_sector_page(doc):
    hdrs = lib.page_headers(doc)
    # Fast path (9-page format): the page before Market Breadth is the sector
    # table. Confirm via its title; only fall back to a full scan if not.
    cand = lib.find_sector_bands_page(doc)
    if cand is not None and _title_has_sector(doc, cand):
        return cand
    pvb = [i for i, h in hdrs if "probable volatility" in h.lower()]
    for pidx in pvb:
        if pidx == cand:
            continue
        if _title_has_sector(doc, pidx):
            return pidx
    return cand


def find_header_anchors(rows):
    """Find the row containing the column headers; return {colidx: x_anchor}."""
    for ri, row in enumerate(rows):
        joined = " ".join(b["text"].lower() for b in row)
        if "asset" in joined and ("beta" in joined or "2y" in joined) and "upside" in joined:
            anchors = {}
            for b in row:
                t = b["text"].lower()
                for key, ci in HEADER_KEYS:
                    if key in t and ci not in anchors:
                        anchors[ci] = b["x"]
            # Interpolate any missing INTERIOR anchor (cols are evenly spaced).
            # The 'rvol' header ("1m rVol") is the one OCR most often drops,
            # which otherwise mis-buckets rvol values toward the beta column.
            for c in range(1, 8):
                if c not in anchors and (c - 1) in anchors and (c + 1) in anchors:
                    anchors[c] = (anchors[c - 1] + anchors[c + 1]) / 2.0
            return ri, anchors
    return None, None


def assign_columns(row, anchors):
    """Bucket each box into the nearest column anchor."""
    cells = {ci: [] for ci in anchors}
    for b in row:
        ci = min(anchors, key=lambda c: abs(anchors[c] - b["x"]))
        cells[ci].append(b)
    out = {}
    for ci, bxs in cells.items():
        bxs.sort(key=lambda b: b["x"])
        out[ci] = " ".join(b["text"] for b in bxs).strip()
    return out


TICKER_RE = re.compile(r"^[A-Z]{2,5}(-[A-Z])?$")


def clean_ticker(s):
    s = s.upper().replace("0", "O").strip()
    # common OCR noise
    s = re.sub(r"[^A-Z\-]", "", s)
    return s if TICKER_RE.match(s) else s  # keep even if odd; flag later


def junky(raw):
    """True if a raw OCR token still has letters/odd chars after stripping the
    expected numeric punctuation ($ , % + - .) -> the value is untrustworthy."""
    if raw is None:
        return True
    s = re.sub(r"[\d\$,%\+\-\.\s]", "", raw)
    return len(s) > 0


def _in_band(last, lo, hi):
    """Is `last` plausibly between the OCR'd lower/upper band (with slack)?"""
    if last is None:
        return False
    if lo is None or hi is None:
        return True  # can't judge -> don't call it bad on this basis alone
    return lo * 0.85 <= last <= hi * 1.15


def repair_beta(v):
    """Beta is ~0-3; a dropped decimal yields e.g. 138 -> 1.38, 115 -> 1.15."""
    if v is None:
        return None
    if v > 5:  # clearly a dropped decimal
        while v > 5:
            v /= 10.0
    return round(v, 2)


def process(doc, date):
    pidx = locate_sector_page(doc)
    if pidx is None:
        return [], "no_sector_page"
    boxes, w, h = lib.ocr_crop(doc, pidx, 0.0, 0.42, 1.0, 1.0)
    rows = lib.cluster_rows(boxes)
    hdr_ri, anchors = find_header_anchors(rows)
    if anchors is None or len(anchors) < 7:
        return [], f"header_not_found(p{pidx+1})"

    results = []
    for row in rows[hdr_ri + 1:]:
        cells = assign_columns(row, anchors)
        raw_asset = cells.get(0, "")
        ticker = clean_ticker(raw_asset)
        if not ticker or not re.search(r"[A-Z]", ticker):
            continue
        # skip footer/legend rows
        if ticker in ("DATE", "SOURCE", "ASSET"):
            continue

        raw_last, raw_up, raw_dn = cells.get(1, ""), cells.get(4, ""), cells.get(5, "")
        raw_upper, raw_lower = cells.get(3, ""), cells.get(2, "")
        last = lib.to_float(raw_last)
        up = lib.to_float(raw_up)
        dn = lib.to_float(raw_dn)
        ocr_lower = lib.to_float(raw_lower)
        ocr_upper = lib.to_float(raw_upper)
        ocr_spread = lib.to_float(cells.get(6, ""))
        rvol = lib.to_float(cells.get(7, ""))
        beta = repair_beta(lib.to_float(cells.get(8, "")))

        # downside is reported negative
        if dn is not None and dn > 0:
            dn = -dn

        flags = []
        for name, v in [("last", last), ("upside", up), ("downside", dn)]:
            if v is None:
                flags.append(f"missing:{name}")

        rec_upper = rec_lower = rec_spread = None
        if last is not None and up is not None:
            rec_upper = round(last * (1 + up / 100.0))
        if last is not None and dn is not None:
            rec_lower = round(last * (1 + dn / 100.0))
        if up is not None and dn is not None:
            rec_spread = round(up - dn, 2)

        def mism(ocr_v, rec_v):
            if ocr_v is None or rec_v is None:
                return False
            tol = max(2.0, abs(rec_v) * 0.02)
            return abs(ocr_v - rec_v) > tol

        # A cross-check mismatch means either the PRIMARY (last/upside/downside)
        # is wrong, or the discarded OCR'd derived cell was garbled. Only treat
        # it as a real error when the suspect primary's raw token looks junky;
        # if instead the OCR'd derived cell is the junky one, the kept
        # (recomputed) value is fine and we don't flag.
        up_mismatch = mism(ocr_upper, rec_upper)
        lo_mismatch = mism(ocr_lower, rec_lower)
        if up_mismatch and lo_mismatch:
            # both sides off -> 'last' is the common cause
            if junky(raw_last) or not _in_band(last, ocr_lower, ocr_upper):
                flags.append("bad:last")
            else:
                flags.append("chk:both")
        else:
            if up_mismatch:
                flags.append("bad:upside" if junky(raw_up) else
                             ("chk:upper" if not junky(raw_upper) else ""))
            if lo_mismatch:
                flags.append("bad:downside" if junky(raw_dn) else
                             ("chk:lower" if not junky(raw_lower) else ""))
        flags = [f for f in flags if f]

        # ---- cautious auto-recovery via the table's internal redundancy ----
        # Each of last / upside / downside is independently encoded (upper/lower
        # bands). When one primary is missing/garbled but the clean OCR'd bands
        # let us reconstruct it, do so and mark it 'recovered:*' (inferred, not
        # read). Only recover from non-junky band values.
        clean_up = ocr_upper if (ocr_upper and not junky(raw_upper)) else None
        clean_lo = ocr_lower if (ocr_lower and not junky(raw_lower)) else None

        last_bad = (last is None) or ("bad:last" in flags)
        if last_bad and up is not None and dn is not None and clean_up and clean_lo:
            cand_hi = clean_up / (1 + up / 100.0)
            cand_lo = clean_lo / (1 + dn / 100.0)
            if abs(cand_hi - cand_lo) / max(cand_hi, 1) < 0.03:
                last = round((cand_hi + cand_lo) / 2, 2)
                flags = [f for f in flags if f not in ("bad:last", "missing:last")]
                flags.append("recovered:last")

        up_bad = (up is None) or ("bad:upside" in flags) or (up is not None and not (0 <= up <= 60))
        if up_bad and last and clean_up:
            cand = round((clean_up / last - 1) * 100, 2)
            if 0 <= cand <= 60:
                up = cand
                flags = [f for f in flags if f not in ("bad:upside", "range:upside")]
                flags.append("recovered:upside")

        dn_bad = (dn is None) or ("bad:downside" in flags) or (dn is not None and not (-60 <= dn <= 0))
        if dn_bad and last and clean_lo:
            cand = round((clean_lo / last - 1) * 100, 2)
            if -60 <= cand <= 0:
                dn = cand
                flags = [f for f in flags if f not in ("bad:downside", "range:downside")]
                flags.append("recovered:downside")

        # recompute derived from (possibly recovered) primaries
        if last is not None and up is not None:
            rec_upper = round(last * (1 + up / 100.0))
        if last is not None and dn is not None:
            rec_lower = round(last * (1 + dn / 100.0))
        if up is not None and dn is not None:
            rec_spread = round(up - dn, 2)

        # standalone last sanity vs clean bands (catches single-band dropped
        # decimals that don't trip the two-sided cross-check)
        if last is not None and "recovered:last" not in flags and (clean_up or clean_lo):
            hi_ok = (clean_up is None) or (last <= clean_up * 1.15)
            lo_ok = (clean_lo is None) or (last >= clean_lo * 0.85)
            if not (hi_ok and lo_ok) and "bad:last" not in flags:
                flags.append("bad:last")

        if rvol is None:
            flags.append("missing:rvol")
        if beta is None:
            flags.append("missing:beta")
        if up is not None and not (0 <= up <= 60):
            flags.append("range:upside")
        if dn is not None and not (-60 <= dn <= 0):
            flags.append("range:downside")

        results.append({
            "report_date": date,
            "asset": ticker,
            "last": last,
            "upside_pct": up,
            "downside_pct": dn,
            "upper_pvb": rec_upper if rec_upper is not None else ocr_upper,
            "lower_pvb": rec_lower if rec_lower is not None else ocr_lower,
            "spread_pct": rec_spread if rec_spread is not None else ocr_spread,
            "rvol_1m": rvol,
            "beta_2y": beta,
            "ocr_upper": ocr_upper, "ocr_lower": ocr_lower, "ocr_spread": ocr_spread,
            "src_page": pidx + 1,
            "flag": ";".join(flags),
        })
    # dedup repeated tickers within one table (OCR occasionally splits/double-
    # reads a row); keep the first, most complete occurrence.
    seen = {}
    deduped = []
    for x in results:
        key = x["asset"]
        if key in seen:
            prev = seen[key]
            # prefer the row with fewer flags / more populated numerics
            if len(x["flag"]) < len(prev["flag"]):
                deduped[deduped.index(prev)] = x
                seen[key] = x
            continue
        seen[key] = x
        deduped.append(x)
    status = "ok" if deduped else "no_rows"
    return deduped, status


def main():
    rows_in = list(csv.DictReader(open(INVENTORY, encoding="utf-8-sig")))
    only = None
    if len(sys.argv) > 1:
        only = int(sys.argv[1])  # limit N reports for testing
        rows_in = rows_in[:only]

    all_out = []
    summary = []
    for n, r in enumerate(rows_in, 1):
        path = r.get("FullPath") or os.path.join(ROOT, r.get("RelPath", ""))
        date = r.get("ReportDate", "")
        try:
            doc = fitz.open(path)
            try:
                recs, status = process(doc, date)
            finally:
                doc.close()
        except Exception as e:
            recs, status = [], f"error:{type(e).__name__}"
        all_out.extend(recs)
        flagged = sum(1 for x in recs if x["flag"])
        summary.append((date, status, len(recs), flagged))
        print(f"[{n}/{len(rows_in)}] {date}: {status}  rows={len(recs)} flagged={flagged}", flush=True)

    fields = ["report_date", "asset", "last", "upside_pct", "downside_pct",
              "upper_pvb", "lower_pvb", "spread_pct", "rvol_1m", "beta_2y",
              "ocr_upper", "ocr_lower", "ocr_spread", "src_page", "flag"]
    with open(OUTPUT, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(all_out)

    nrep = len(rows_in)
    nrows = len(all_out)
    nflag = sum(1 for x in all_out if x["flag"])
    bad_reports = [s for s in summary if s[1] != "ok"]
    print("\n==== SUMMARY ====")
    print(f"reports: {nrep}   rows extracted: {nrows}   flagged rows: {nflag} ({100*nflag/max(1,nrows):.1f}%)")
    print(f"reports with non-ok status: {len(bad_reports)}")
    for s in bad_reports[:20]:
        print("   ", s)
    print(f"-> {OUTPUT}")


if __name__ == "__main__":
    main()
