"""
eod_report.py — the end-of-day digest.

Runs LAST in the day (after the forward collector finishes) and emails ONE concise
status email covering every daily activity. It does not re-run anything — it READS
each subsystem's status artifact (the small JSONs in status.py, plus native files
like the supervisor heartbeat and the Tiingo manifest) and renders a section per
job. A job that crashed or never ran shows as ❌/stale rather than taking the
report down.

Sections today: IBKR forward collector · ThetaData warehouse · Tiingo refresh ·
System/Gateway health · (Strategies — reserved, lights up once strategies run).
Adding a section later = write one build_*() and append it to SECTIONS.

Run manually any time:  <venv python> eod_report.py
"""

from __future__ import annotations

import datetime as dt
import json
import os
import shutil
from pathlib import Path

import mailer
import status

WAREHOUSE = Path(r"C:\TradingDesk-Local\warehouse")
RAW_OPTIONS = WAREHOUSE / "raw" / "options"
SUPERVISOR_HB = WAREHOUSE / "supervisor_heartbeat.txt"
TIINGO_MANIFEST = Path(r"C:\Users\andre\My Drive (andrew@surberhc.com)"
                       r"\TradingDesk\backtester\data\_manifest.json")
LOG = Path(r"C:\TradingDesk-Local\state\dailyreport\eod_report.log")

TODAY = dt.date.today()
TODAY_STR = TODAY.strftime("%Y%m%d")

# status -> (dot color, label). Severity order used for the overall headline.
DOT = {"ok": "#22c55e", "info": "#3b82f6", "stale": "#9ca3af",
       "warn": "#f59e0b", "fail": "#ef4444"}
SEVERITY = ["ok", "info", "stale", "warn", "fail"]


def _log(msg: str) -> None:
    line = f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    try:
        print(line, flush=True)
    except Exception:
        pass
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _sec(key, title, st, headline, rows):
    return {"key": key, "title": title, "status": st, "headline": headline, "rows": rows}


# --------------------------------------------------------------------------- #
# Section builders — each returns a section dict. Never raise.
# --------------------------------------------------------------------------- #
def build_forward():
    s = status.read("forward")
    if not s:
        return _sec("forward", "IBKR Forward Collector", "stale",
                    "No status written — did the 5:30 PM run fire?", [])
    m = s.get("metrics", {})
    fresh = s.get("date") == TODAY_STR
    st = s.get("status", "fail") if fresh else "stale"
    rows = [("Run date", s.get("date")), ("Roots written", m.get("ok")),
            ("Already had", m.get("skip")), ("Empty/holiday", m.get("empty")),
            ("Failed roots", m.get("fail")), ("Real errors", m.get("real_errors")),
            ("Last update", s.get("ts"))]
    headline = s.get("message", "") + ("" if fresh else "  ⚠ status is from a previous day")
    return _sec("forward", "IBKR Forward Collector", st, headline, rows)


def build_thetadata():
    files = 0
    size = 0
    if RAW_OPTIONS.exists():
        for root, _dirs, fnames in os.walk(RAW_OPTIONS):
            for fn in fnames:
                if fn.endswith(".parquet"):
                    files += 1
                    try:
                        size += os.path.getsize(os.path.join(root, fn))
                    except OSError:
                        pass
    hb = SUPERVISOR_HB.read_text().strip() if SUPERVISOR_HB.exists() else "(no heartbeat)"
    complete = "COMPLETE" in hb
    st = "ok" if complete else ("info" if files > 0 else "warn")
    headline = "One-time grab COMPLETE" if complete else "One-time grab in progress"
    rows = [("Warehouse files", f"{files:,}"), ("Warehouse size", f"{size/1e9:.2f} GB"),
            ("Supervisor heartbeat", hb)]
    return _sec("thetadata", "ThetaData Warehouse (one-time grab)", st, headline, rows)


def build_tiingo():
    s = status.read("tiingo")
    if s:
        fresh = s.get("date") == TODAY_STR
        st = s.get("status", "fail") if fresh else "stale"
        m = s.get("metrics", {})
        rows = [("Run date", s.get("date")), ("Tickers", m.get("tickers")),
                ("QC flags", m.get("qc_flags")), ("Data end", m.get("data_end")),
                ("Last update", s.get("ts"))]
        head = s.get("message", "") + ("" if fresh else "  ⚠ status is from a previous day")
        return _sec("tiingo", "Tiingo Data Refresh", st, head, rows)
    # Fallback: read the backtester manifest directly.
    if TIINGO_MANIFEST.exists():
        try:
            mani = json.loads(TIINGO_MANIFEST.read_text())
            gen = mani.get("generated_at", "")
            fresh = gen[:10] == TODAY.isoformat()
            st = "ok" if fresh else "stale"
            n = len(mani.get("tickers", {}))
            return _sec("tiingo", "Tiingo Data Refresh", st,
                        f"manifest generated {gen[:19]}",
                        [("Tickers", n), ("Data end", mani.get("data_end"))])
        except Exception:
            pass
    return _sec("tiingo", "Tiingo Data Refresh", "stale",
                "No Tiingo status or manifest found", [])


def build_system():
    rows = []
    try:
        from connections import ibkr as gw
        up = gw.gateway_running()
    except Exception:
        up = False
    rows.append(("IB Gateway (paper 4002)", "UP" if up else "DOWN"))
    try:
        _t, _u, free = shutil.disk_usage("C:\\")
        rows.append(("C: free space", f"{free/1e9:.0f} GB"))
    except Exception:
        free = 0
    st = "ok" if (up and free > 5e9) else ("warn" if up else "warn")
    headline = "Gateway up" if up else "Gateway DOWN (forward collector needs it)"
    return _sec("system", "System / Gateway Health", st, headline, rows)


def build_strategy():
    s = status.read("strategy")
    if not s:
        return _sec("strategy", "Strategy EOD Update", "info",
                    "Not yet active — appears here once strategies are running.", [])
    fresh = s.get("date") == TODAY_STR
    st = s.get("status", "fail") if fresh else "stale"
    m = s.get("metrics", {})
    rows = [(k, v) for k, v in m.items()] + [("Last update", s.get("ts"))]
    return _sec("strategy", "Strategy EOD Update", st, s.get("message", ""), rows)


SECTIONS = [build_forward, build_thetadata, build_tiingo, build_system, build_strategy]


# --------------------------------------------------------------------------- #
# Render
# --------------------------------------------------------------------------- #
def _overall(sections):
    worst = "ok"
    for s in sections:
        if SEVERITY.index(s["status"]) > SEVERITY.index(worst):
            worst = s["status"]
    return worst


def render_html(sections, overall):
    def dot(st):
        return (f'<span style="display:inline-block;width:11px;height:11px;'
                f'border-radius:50%;background:{DOT[st]};margin-right:8px;"></span>')

    blocks = []
    for s in sections:
        rows_html = "".join(
            f'<tr><td style="padding:2px 14px 2px 0;color:#6b7280;white-space:nowrap;">{k}</td>'
            f'<td style="padding:2px 0;color:#111827;">{"" if v is None else v}</td></tr>'
            for k, v in s["rows"])
        table = (f'<table style="border-collapse:collapse;font-size:13px;margin-top:6px;">'
                 f'{rows_html}</table>') if s["rows"] else ""
        blocks.append(
            f'<div style="border:1px solid #e5e7eb;border-radius:8px;padding:12px 14px;'
            f'margin:10px 0;background:#fff;">'
            f'<div style="font-size:15px;font-weight:600;color:#111827;">'
            f'{dot(s["status"])}{s["title"]}'
            f'<span style="float:right;font-size:11px;text-transform:uppercase;'
            f'letter-spacing:.05em;color:{DOT[s["status"]]};">{s["status"]}</span></div>'
            f'<div style="font-size:13px;color:#374151;margin-top:4px;">{s["headline"]}</div>'
            f'{table}</div>')

    stamp = dt.datetime.now().strftime("%A %b %d, %Y  %I:%M %p")
    return (
        f'<div style="font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;'
        f'max-width:640px;margin:0 auto;color:#111827;">'
        f'<div style="font-size:18px;font-weight:700;">{dot(overall)}Trading Desk — End of Day</div>'
        f'<div style="font-size:12px;color:#6b7280;margin:2px 0 6px;">{stamp} · '
        f'overall: <b style="color:{DOT[overall]};text-transform:uppercase;">{overall}</b></div>'
        f'{"".join(blocks)}'
        f'<div style="font-size:11px;color:#9ca3af;margin-top:10px;">'
        f'Automated end-of-day digest · TradingDesk\\dailyreport\\eod_report.py</div></div>')


def main() -> None:
    _log(f"=== EOD report {TODAY_STR} start ===")
    sections = []
    for build in SECTIONS:
        try:
            sections.append(build())
        except Exception as e:
            sections.append(_sec(build.__name__, build.__name__, "fail",
                                 f"section error: {type(e).__name__}: {e}", []))
    overall = _overall(sections)
    html = render_html(sections, overall)
    subject = f"Trading Desk EOD — {TODAY.strftime('%b %d')} — {overall.upper()}"
    sent = mailer.send_html(subject, html)
    _log(f"sections={[s['status'] for s in sections]} overall={overall} "
         f"emailed={'YES' if sent else 'NO'} -> {mailer.recipient()}")
    status.write("eod_report", "ok" if sent else "fail",
                 metrics={"overall": overall, "emailed": sent}, day=TODAY_STR)
    _log(f"=== EOD report {TODAY_STR} done ===")


if __name__ == "__main__":
    main()
