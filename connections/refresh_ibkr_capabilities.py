"""
refresh_ibkr_capabilities.py — the machine auto-updater for IBKR_CAPABILITIES.md.

WHAT IT DOES
    Pulls MACHINE TRUTH from the live paper Gateway to keep the capabilities doc honest:
      1. Connects READ-ONLY (registered clientId `capabilities_introspect`, id 41).
      2. Calls reqScannerParameters() and saves the raw XML to
         connections/capabilities/ibkr_scanner_params_<YYYYMMDD>.xml.
      3. Parses the XML for the available scan codes and filter/tag names.
      4. DIFFs that list against the most recent PRIOR snapshot in connections/capabilities/
         and reports any ADDED / REMOVED items (IBKR changes these over time).
      5. Stamps the `Last introspected (machine)` line in IBKR_CAPABILITIES.md, and appends a
         Changelog entry when a diff is found.

WHY
    Scan codes and filter tags are version-specific — the only authoritative source is the live
    Gateway. This script is the "machine line" that keeps IBKR_CAPABILITIES.md from going stale.

SCHEDULE
    Meant to run on a MONTHLY schedule. Scheduling is wired separately — this script does NOT
    create any scheduled task, and never places an order (read-only only).

RUN
    "C:\\TradingDesk-Local\\venv\\Scripts\\python.exe" -m connections.refresh_ibkr_capabilities
    or directly:
    "C:\\TradingDesk-Local\\venv\\Scripts\\python.exe" connections\\refresh_ibkr_capabilities.py

EXIT CODES
    0  success (with or without a diff)
    1  gateway down / connect failed / entitlement missing / any hard error
"""
from __future__ import annotations

import re
import sys
import xml.etree.ElementTree as ET
from datetime import date, datetime
from pathlib import Path

from ib_async import IB

from connections import clientids

HOST = "127.0.0.1"
PAPER_PORT = clientids.PAPER_PORT  # 4002
CONSUMER = "capabilities_introspect"

HERE = Path(__file__).resolve().parent
CAP_DIR = HERE / "capabilities"
DOC = HERE / "IBKR_CAPABILITIES.md"
LOG = CAP_DIR / "refresh.log"

CONNECT_TIMEOUT = 15   # hard timeout on the connect
CALL_TIMEOUT = 60      # hard timeout on reqScannerParameters


def log(msg: str) -> None:
    """Flushed, timestamped print + append to the refresh log (liveness rubric)."""
    line = f"[{datetime.now().isoformat(timespec='seconds')}] {msg}"
    print(line, flush=True)
    try:
        CAP_DIR.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass  # logging must never crash the run


def parse_scanner_params(xml_text: str) -> tuple[list[str], list[str]]:
    """Return (scan_codes, filter_tags) sorted+deduped from the reqScannerParameters XML.

    scan codes  = every <ScanType>/<scanCode> value.
    filter tags = every <AbstractField>/<code> value (the TagValue filter codes) plus any
                  <RangeFilter>/<id>. We stay tolerant of tag-name drift across versions by
                  scanning for the well-known element names rather than a fixed schema path.
    """
    root = ET.fromstring(xml_text)

    scan_codes: set[str] = set()
    for st in root.iter("ScanType"):
        code = st.findtext("scanCode")
        if code and code.strip():
            scan_codes.add(code.strip())

    filter_tags: set[str] = set()
    # AbstractField/code is the canonical filter-tag code (e.g. priceAbove, usdMarketCapAbove).
    for af in root.iter("AbstractField"):
        code = af.findtext("code")
        if code and code.strip():
            filter_tags.add(code.strip())
    # RangeFilter ids are the paired range codes; include their id for completeness.
    for rf in root.iter("RangeFilter"):
        rid = rf.findtext("id")
        if rid and rid.strip():
            filter_tags.add(rid.strip())

    return sorted(scan_codes), sorted(filter_tags)


def latest_prior_snapshot(today_path: Path) -> Path | None:
    """Most recent prior params XML in CAP_DIR (excluding the one we just wrote)."""
    snaps = sorted(CAP_DIR.glob("ibkr_scanner_params_*.xml"))
    snaps = [p for p in snaps if p != today_path]
    return snaps[-1] if snaps else None


def diff_lists(old: list[str], new: list[str]) -> tuple[list[str], list[str]]:
    """(added, removed) between an old and new sorted list."""
    old_s, new_s = set(old), set(new)
    return sorted(new_s - old_s), sorted(old_s - new_s)


def update_doc_introspected_line(stamp: str) -> None:
    """Rewrite the `Last introspected (machine): ...` line in IBKR_CAPABILITIES.md."""
    if not DOC.exists():
        log(f"WARN: {DOC.name} not found; skipping doc stamp")
        return
    text = DOC.read_text(encoding="utf-8")
    new_line = f"`Last introspected (machine): {stamp}`"
    new_text, n = re.subn(
        r"`Last introspected \(machine\): [^`]*`", new_line, text, count=1)
    if n == 0:
        log("WARN: could not find the 'Last introspected (machine)' line to update")
        return
    DOC.write_text(new_text, encoding="utf-8")
    log(f"stamped doc: {new_line}")


def append_changelog(entry: str) -> None:
    """Append a dated bullet under the `## Changelog` section (reverse chronological → on top)."""
    if not DOC.exists():
        return
    text = DOC.read_text(encoding="utf-8")
    marker = "## Changelog\n"
    idx = text.find(marker)
    if idx == -1:
        log("WARN: no '## Changelog' section found; skipping changelog append")
        return
    insert_at = idx + len(marker)
    # keep one blank line after the header, then the newest entry on top
    bullet = f"\n- **{date.today().isoformat()}** — {entry}\n"
    new_text = text[:insert_at] + bullet + text[insert_at:]
    DOC.write_text(new_text, encoding="utf-8")
    log(f"changelog += {entry}")


def main() -> int:
    log("heartbeat: refresh_ibkr_capabilities starting")
    CAP_DIR.mkdir(parents=True, exist_ok=True)

    client_id = clientids.get(CONSUMER)
    ib = IB()

    # --- connect (read-only, hard timeout) ---
    try:
        log(f"connecting READ-ONLY to paper Gateway {HOST}:{PAPER_PORT} clientId={client_id}")
        ib.connect(HOST, PAPER_PORT, clientId=client_id, readonly=True, timeout=CONNECT_TIMEOUT)
    except Exception as exc:
        log(f"ERROR: could not connect to paper Gateway ({type(exc).__name__}: {exc}). "
            "Is it up? Exiting non-zero, NOT hanging.")
        try:
            ib.disconnect()
        except Exception:
            pass
        return 1

    # --- reqScannerParameters (hard timeout; entitlement may be missing) ---
    try:
        log("heartbeat: calling reqScannerParameters()")
        xml_text = ib.reqScannerParameters()
        # ib_async returns the XML synchronously; guard against empty/entitlement failure
        if not xml_text or not xml_text.strip():
            log("ERROR: reqScannerParameters() returned empty — likely missing market-data "
                "entitlement on this account. Exiting non-zero.")
            return 1
    except Exception as exc:
        log(f"ERROR: reqScannerParameters() failed ({type(exc).__name__}: {exc}). "
            "Could be a missing entitlement or a farm hiccup. Exiting non-zero, NOT hanging.")
        return 1
    finally:
        try:
            ib.disconnect()
            log("disconnected")
        except Exception:
            pass

    # --- save snapshot ---
    stamp_day = date.today().strftime("%Y%m%d")
    out_xml = CAP_DIR / f"ibkr_scanner_params_{stamp_day}.xml"
    out_xml.write_text(xml_text, encoding="utf-8")
    log(f"saved scanner params XML: {out_xml.name} ({len(xml_text):,} chars)")

    # --- parse ---
    try:
        scan_codes, filter_tags = parse_scanner_params(xml_text)
    except Exception as exc:
        log(f"ERROR: failed to parse scanner params XML ({type(exc).__name__}: {exc}). "
            "Snapshot saved; exiting non-zero.")
        return 1
    log(f"parsed {len(scan_codes)} scan codes, {len(filter_tags)} filter tags")

    # --- diff against prior snapshot ---
    prior = latest_prior_snapshot(out_xml)
    stamp_iso = date.today().isoformat()
    if prior is None:
        log("no prior snapshot to diff against — this is the first machine introspection")
        update_doc_introspected_line(stamp_iso)
        append_changelog(
            f"first machine introspection — {len(scan_codes)} scan codes, "
            f"{len(filter_tags)} filter tags snapshotted to capabilities/{out_xml.name}")
        _print_summary(scan_codes, filter_tags)
        log("heartbeat: done (first run, no diff)")
        return 0

    try:
        old_codes, old_tags = parse_scanner_params(prior.read_text(encoding="utf-8"))
    except Exception as exc:
        log(f"WARN: could not parse prior snapshot {prior.name} ({exc}); treating as no-diff base")
        old_codes, old_tags = scan_codes, filter_tags

    codes_added, codes_removed = diff_lists(old_codes, scan_codes)
    tags_added, tags_removed = diff_lists(old_tags, filter_tags)

    update_doc_introspected_line(stamp_iso)

    has_diff = any((codes_added, codes_removed, tags_added, tags_removed))
    if not has_diff:
        log(f"no scan-code/filter changes vs {prior.name}")
        _print_summary(scan_codes, filter_tags)
        log("heartbeat: done (no diff)")
        return 0

    # report + changelog the diff
    parts: list[str] = []
    if codes_added:
        log(f"SCAN CODES ADDED ({len(codes_added)}): {', '.join(codes_added)}")
        parts.append(f"scan codes +[{', '.join(codes_added)}]")
    if codes_removed:
        log(f"SCAN CODES REMOVED ({len(codes_removed)}): {', '.join(codes_removed)}")
        parts.append(f"scan codes -[{', '.join(codes_removed)}]")
    if tags_added:
        log(f"FILTER TAGS ADDED ({len(tags_added)}): {', '.join(tags_added)}")
        parts.append(f"filter tags +[{', '.join(tags_added)}]")
    if tags_removed:
        log(f"FILTER TAGS REMOVED ({len(tags_removed)}): {', '.join(tags_removed)}")
        parts.append(f"filter tags -[{', '.join(tags_removed)}]")

    append_changelog(
        "machine introspection detected scanner-parameter changes vs "
        f"{prior.name}: " + "; ".join(parts))
    _print_summary(scan_codes, filter_tags)
    log("heartbeat: done (diff found + changelogged)")
    return 0


def _print_summary(scan_codes: list[str], filter_tags: list[str]) -> None:
    log("--- scan codes (first 30) ---")
    log(", ".join(scan_codes[:30]) + (" ..." if len(scan_codes) > 30 else ""))
    log("--- filter tags (first 30) ---")
    log(", ".join(filter_tags[:30]) + (" ..." if len(filter_tags) > 30 else ""))


if __name__ == "__main__":
    sys.exit(main())
