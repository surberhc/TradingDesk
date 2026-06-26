#!/usr/bin/env python3
"""
MSR commentary parser.

Reads the canonical daily inventory (_msr_canonical_daily.csv), extracts the
three front-page commentary blocks from each Market Situation/Structure Report,
cleans the text, and writes a structured CSV (_msr_commentary.csv).

The three blocks are the stable "spine" present in 100% of both the 9-page
("Market Situation Report") and 11-page ("Market Structure Report") formats:
    - SPX Gamma Exposure:
    - Systematic Rebalancing:
    - Strategic Allocation:

Everything past the front page is chart imagery (no text layer) and is out of
scope for this parser.
"""

import csv
import os
import re
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
INVENTORY = os.path.join(ROOT, "_msr_canonical_daily.csv")
OUTPUT = os.path.join(ROOT, "_msr_commentary.csv")

# The three commentary labels, in document order.
LABELS = [
    ("gamma_exposure", "SPX Gamma Exposure:"),
    ("systematic_rebalancing", "Systematic Rebalancing:"),
    ("strategic_allocation", "Strategic Allocation:"),
]

# First chart-section header that follows the commentary in both formats.
# Used as the terminator for the final (Strategic Allocation) block.
END_MARKERS = [
    "SPX Short-Dated Volatility",
    "SPX Dealer Gamma Profile",
]


def find_pdftotext():
    """Locate the poppler pdftotext binary."""
    p = shutil.which("pdftotext")
    if p:
        return p
    # Common Git-for-Windows bundled location.
    fallback = r"C:\Program Files\Git\mingw64\bin\pdftotext.exe"
    if os.path.isfile(fallback):
        return fallback
    sys.exit("ERROR: pdftotext not found on PATH or in Git mingw64 bin.")


PDFTOTEXT = find_pdftotext()


def extract_text(pdf_path):
    """Run `pdftotext -layout` and return the text (first page is enough, but we
    take the whole doc and slice off at the first chart header)."""
    result = subprocess.run(
        [PDFTOTEXT, "-layout", pdf_path, "-"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        return None
    return result.stdout


def clean_block(raw_lines):
    """Collapse wrapped, heavily-indented lines into one clean paragraph."""
    text = " ".join(line.strip() for line in raw_lines if line.strip())
    text = re.sub(r"\s+", " ", text).strip()
    return text


def detect_format(text):
    if re.search(r"Market\s+Structure\s+Report", text, re.I):
        return "Structure(11pg)"
    if re.search(r"Market\s+Situation\s+Report", text, re.I):
        return "Situation(9pg)"
    return "unknown"


def parse_commentary(text):
    """Return dict of the three blocks plus a parse status."""
    lines = text.splitlines()

    # Locate each label's line index.
    positions = {}
    for key, label in LABELS:
        for i, line in enumerate(lines):
            if label in line:
                positions[key] = i
                break

    # Locate the first end-marker line (start of chart pages).
    end_idx = len(lines)
    for i, line in enumerate(lines):
        if any(m in line for m in END_MARKERS):
            end_idx = i
            break

    blocks = {}
    missing = []
    keys = [k for k, _ in LABELS]
    for n, (key, label) in enumerate(LABELS):
        if key not in positions:
            blocks[key] = ""
            missing.append(key)
            continue
        start = positions[key] + 1  # text begins after the label line
        # Block ends at the next present label, or at the chart section.
        nxt = len(lines)
        for later_key in keys[n + 1:]:
            if later_key in positions:
                nxt = positions[later_key]
                break
        stop = min(nxt, end_idx) if end_idx > start else nxt
        blocks[key] = clean_block(lines[start:stop])

    status = "ok" if not missing else "missing:" + "|".join(missing)
    # Flag suspiciously short captures (label found but body empty/tiny).
    for key in keys:
        if key not in missing and len(blocks[key]) < 20:
            status = "short:" + key if status == "ok" else status + ";short:" + key
    return blocks, status


def main():
    if not os.path.isfile(INVENTORY):
        sys.exit(f"ERROR: inventory not found: {INVENTORY}")

    rows_in = []
    with open(INVENTORY, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            rows_in.append(r)

    out_rows = []
    n_ok = n_issue = 0
    for r in rows_in:
        path = r.get("FullPath") or os.path.join(ROOT, r.get("RelPath", ""))
        date = r.get("ReportDate", "")
        text = extract_text(path)
        if text is None:
            out_rows.append({
                "report_date": date, "format": "EXTRACT_FAILED",
                "gamma_exposure": "", "systematic_rebalancing": "",
                "strategic_allocation": "", "parse_status": "extract_failed",
                "file_name": r.get("FileName", ""),
            })
            n_issue += 1
            continue
        fmt = detect_format(text)
        blocks, status = parse_commentary(text)
        out_rows.append({
            "report_date": date,
            "format": fmt,
            "gamma_exposure": blocks["gamma_exposure"],
            "systematic_rebalancing": blocks["systematic_rebalancing"],
            "strategic_allocation": blocks["strategic_allocation"],
            "parse_status": status,
            "file_name": r.get("FileName", ""),
        })
        if status == "ok":
            n_ok += 1
        else:
            n_issue += 1

    out_rows.sort(key=lambda x: x["report_date"])
    fields = ["report_date", "format", "gamma_exposure",
              "systematic_rebalancing", "strategic_allocation",
              "parse_status", "file_name"]
    with open(OUTPUT, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(out_rows)

    print(f"Parsed {len(out_rows)} reports -> {OUTPUT}")
    print(f"  clean: {n_ok}   needs-review: {n_issue}")
    if n_issue:
        print("  rows flagged:")
        for r in out_rows:
            if r["parse_status"] != "ok":
                print(f"    [{r['report_date']}] {r['parse_status']}  ({r['file_name']})")


if __name__ == "__main__":
    main()
