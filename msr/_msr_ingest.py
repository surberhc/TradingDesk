#!/usr/bin/env python3
"""
Auto-ingest new MSR report PDFs into the dataset.

Drop new daily-MSR PDFs anywhere under the folder, then run:  py _msr_ingest.py

It finds dated MSR PDFs not yet in the dataset, OCR-extracts only those (sector
bands, realized vol, front-page key levels + regimes), appends them to the raw
CSVs, then re-runs the (fast) downstream steps — reconstruct, validate, apply
vision fixes, rebuild msr.db. Idempotent: re-running with no new files is a
no-op. The slow OCR only touches genuinely new reports.

NOTE: only the daily "MSR_2.0 …YYYY.MM.DD" reports are ingested; SITREP recaps
and other PDFs are skipped (same rule as the original inventory).
"""
import csv
import glob
import importlib.util
import os
import re
import subprocess
import sys
import hashlib
import fitz

ROOT = os.path.dirname(os.path.abspath(__file__))


def _load(mod):
    spec = importlib.util.spec_from_file_location(mod, os.path.join(ROOT, mod + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def parse_date(name):
    m = re.search(r"(\d{4})\.(\d{2})\.(\d{2})", name)
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None


def sha(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def main():
    sb = _load("_msr_sector_bands")
    rv = _load("_msr_realized_vol")
    fp = _load("_msr_front_page")

    # existing report dates already in the raw sector file
    raw_sb = os.path.join(ROOT, "_msr_sector_bands.csv")
    have = set()
    if os.path.exists(raw_sb):
        have = {r["report_date"] for r in csv.DictReader(open(raw_sb, encoding="utf-8"))}

    # discover candidate daily-MSR PDFs (dated, "MSR" in name), dedup by date
    cands = {}
    for p in glob.glob(os.path.join(ROOT, "**", "*.pdf"), recursive=True):
        base = os.path.basename(p)
        if "MSR" not in base.upper():
            continue
        d = parse_date(base)
        if not d or d in have:
            continue
        cands.setdefault(d, p)  # first file wins per date

    new_dates = sorted(cands)
    if not new_dates:
        print("No new MSR reports to ingest. Dataset is up to date.")
        return
    print(f"Found {len(new_dates)} new report(s): {', '.join(new_dates)}")

    # append rows to each raw CSV
    def append(path, fieldnames, rows):
        exists = os.path.exists(path)
        with open(path, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            if not exists:
                w.writeheader()
            for r in rows:
                w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in fieldnames})

    SB_F = ["report_date", "asset", "last", "upside_pct", "downside_pct", "upper_pvb",
            "lower_pvb", "spread_pct", "rvol_1m", "beta_2y", "ocr_upper", "ocr_lower",
            "ocr_spread", "src_page", "flag"]
    RV_F = ["report_date", "dropoff", "date_1m", "drop_1m_pct", "date_3m", "drop_3m_pct", "src_page", "flag"]
    KL_F = ["report_date"] + [x for x, _ in fp.KL_LABELS] + ["flag"]
    RG_F = ["report_date"] + [x for x, _, _ in fp.REGIMES] + ["flag"]

    inv_rows = []
    for d in new_dates:
        path = cands[d]
        doc = fitz.open(path)
        try:
            sbres, _ = sb.process(doc, d)
            rvres, _ = rv.process(doc, d)
            pg = doc[0]
            kl = fp.extract_key_levels(pg)
            rg = fp.extract_regimes(pg)
        finally:
            doc.close()
        append(raw_sb, SB_F, sbres)
        append(os.path.join(ROOT, "_msr_realized_vol.csv"), RV_F, rvres)
        klrow = {"report_date": d}; klrow.update({f: kl.get(f) for f, _ in fp.KL_LABELS}); klrow["flag"] = ""
        append(os.path.join(ROOT, "_msr_spx_key_levels.csv"), KL_F, [klrow])
        rgrow = {"report_date": d}; rgrow.update({f: rg.get(f) for f, _, _ in fp.REGIMES}); rgrow["flag"] = ""
        append(os.path.join(ROOT, "_msr_regimes.csv"), RG_F, [rgrow])
        # inventory row
        sz = round(os.path.getsize(path) / 1048576, 3)
        npg = fitz.open(path).page_count
        inv_rows.append({"DocType": "MSR_Daily", "ReportDate": d, "FileName": os.path.basename(path),
                         "RelPath": os.path.relpath(path, ROOT), "SizeMB": sz, "PageCount": npg,
                         "DupCopiesRemoved": 0, "Flags": "", "SHA256": sha(path), "FullPath": path})
        print(f"  ingested {d}: sector={len(sbres)} rv={len(rvres)} KL={sum(1 for f,_ in fp.KL_LABELS if kl.get(f) is not None)}/12 RG={sum(1 for f,_,_ in fp.REGIMES if rg.get(f))}/4")

    # extend canonical inventory
    inv_path = os.path.join(ROOT, "_msr_canonical_daily.csv")
    if os.path.exists(inv_path) and inv_rows:
        existing = list(csv.DictReader(open(inv_path, encoding="utf-8-sig")))
        fn = existing[0].keys() if existing else inv_rows[0].keys()
        with open(inv_path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(fn)); w.writeheader()
            w.writerows(existing)
            for r in inv_rows:
                w.writerow({k: r.get(k, "") for k in fn})

    # re-run downstream (fast): reconstruct -> validators -> fixes -> build db
    print("\nRebuilding downstream tables...")
    for step in ["_msr_reconstruct.py", "_msr_apply_fixes.py", "_msr_rv_validate.py",
                 "_msr_keylevels_validate.py", "_msr_build_db.py"]:
        r = subprocess.run([sys.executable, os.path.join(ROOT, step)], capture_output=True, text=True)
        tag = "ok" if r.returncode == 0 else "FAILED"
        print(f"  {step}: {tag}")
        if r.returncode != 0:
            print(r.stderr[-500:])
    print("\nDone. Review any rows flagged 'anomaly:'/'missing:' in the *_clean.csv worklists; "
          "vision-fix as needed and append to _msr_manual_fixes.csv / _msr_keylevels_fixes.csv.")


if __name__ == "__main__":
    main()
