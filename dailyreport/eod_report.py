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
import sys
import threading
from pathlib import Path

import mailer
import status

WAREHOUSE = Path(r"C:\TradingDesk-Local\warehouse")
RAW_OPTIONS = WAREHOUSE / "raw" / "options"
DERIVED = WAREHOUSE / "derived"
SUPERVISOR_HB = WAREHOUSE / "supervisor_heartbeat.txt"
FORWARD_HB = WAREHOUSE / "forward_heartbeat.txt"
ALARM_RAN_MARKER = WAREHOUSE / "heartbeat_alarm_ran.txt"
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
def _parse_forward_heartbeat():
    """Parse warehouse\\forward_heartbeat.txt into a dict, or None if absent/unparseable.

    Two shapes the collector writes (datacollector\\forward_daily.py):
      in-progress: "2026-06-26 23:55:10  20260626  43/50 roots  ok=13 skip=0 empty=0 fail=30"
      complete:    "2026-06-26 23:59:00  20260626  COMPLETE ok=.. skip=.. empty=.. fail=.."
    Returns: {ts, date, done(int|None), total(int|None), complete(bool),
              ok, skip, empty, fail}
    """
    if not FORWARD_HB.exists():
        return None
    try:
        text = FORWARD_HB.read_text().strip()
    except Exception:
        return None
    if not text:
        return None
    line = text.splitlines()[-1].strip()
    import re
    # "YYYY-MM-DD HH:MM:SS" then 8-digit run date
    m_head = re.match(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+(\d{8})", line)
    if not m_head:
        return None
    ts, date = m_head.group(1), m_head.group(2)
    complete = "COMPLETE" in line
    m_prog = re.search(r"(\d+)\s*/\s*(\d+)\s+roots", line)
    done = int(m_prog.group(1)) if m_prog else None
    total = int(m_prog.group(2)) if m_prog else None

    def _int(key):
        mm = re.search(rf"{key}=(\d+)", line)
        return int(mm.group(1)) if mm else None

    return {"ts": ts, "date": date, "done": done, "total": total,
            "complete": complete, "ok": _int("ok"), "skip": _int("skip"),
            "empty": _int("empty"), "fail": _int("fail")}


def _build_forward_from_heartbeat():
    """Fallback section for when forward.json is missing/stale: render from the
    heartbeat the collector writes after every root. Returns a section or None."""
    h = _parse_forward_heartbeat()
    if not h:
        return None
    fresh = h["date"] == TODAY_STR
    fail = h.get("fail") or 0
    if not fresh:
        st = "stale"
    elif fail > 0 or not h["complete"]:
        st = "warn"
    else:
        st = "ok"

    if h["done"] is not None and h["total"] is not None:
        progress = f"{h['done']}/{h['total']} roots"
    elif h["complete"]:
        progress = "COMPLETE"
    else:
        progress = None

    parts = []
    if h["complete"]:
        parts.append("collector finished")
    elif progress:
        parts.append(f"partial run reached {progress}")
    else:
        parts.append("collector ran")
    parts.append(f"ok={h.get('ok')} fail={fail}")
    headline = "(from heartbeat — forward.json missing) " + " · ".join(parts)
    if not fresh:
        headline += "  ⚠ heartbeat is from a previous day"

    rows = [("Run date", h["date"]),
            ("Progress", progress or ("COMPLETE" if h["complete"] else "—")),
            ("Roots written", h.get("ok")),
            ("Already had", h.get("skip")),
            ("Empty/holiday", h.get("empty")),
            ("Failed roots", h.get("fail")),
            ("Last update", h["ts"]),
            ("Source", "forward_heartbeat.txt (forward.json absent)")]
    return _sec("forward", "IBKR Forward Collector", st, headline, rows)


def build_forward():
    s = status.read("forward")
    # Prefer forward.json when present AND fresh (it's richer). Otherwise fall back
    # to the heartbeat the collector writes after every root, so a partial/aborted
    # run (which never writes forward.json) still renders a meaningful line instead
    # of reading as "missing/stale".
    if not s or s.get("date") != TODAY_STR:
        hb_sec = _build_forward_from_heartbeat()
        if hb_sec is not None:
            return hb_sec
    if not s:
        return _sec("forward", "Daily Options Grab (ThetaData)", "stale",
                    "No status written — did the 5:30 PM run fire?", [])
    m = s.get("metrics", {})
    fresh = s.get("date") == TODAY_STR
    st = s.get("status", "fail") if fresh else "stale"
    rows = [("Run date", s.get("date")), ("Roots written", m.get("ok")),
            ("Already had", m.get("skip")), ("Empty/holiday", m.get("empty")),
            ("Failed roots", m.get("fail")), ("Real errors", m.get("real_errors")),
            ("Last update", s.get("ts"))]
    headline = s.get("message", "") + ("" if fresh else "  ⚠ status is from a previous day")
    return _sec("forward", "Daily Options Grab (ThetaData)", st, headline, rows)


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


def _port_open(host: str, port: int, timeout: float = 2.0) -> bool:
    """Cheap 'is something listening?' TCP probe — milliseconds, no asyncio, no
    trading session. The EOD digest must NEVER open a live ib_async connection just
    to print gateway up/down: under the non-interactive scheduled-task context that
    full asyncio round-trip crashed the whole process before the email sent (silent
    for 5 nights, 2026-06-27..07-01). A port-open check is a safe proxy for 'up'."""
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def build_system():
    rows = []
    up = _port_open("127.0.0.1", 4002)
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


# Dealer-gamma reads the derived GEX tables (features/gex.py output) directly.
# Index first (the validated MSR signal lives on SPX), ETF second for the cash tape.
GEX_INDEX = ["SPX", "SPXW"]   # use whichever derived table exists; SPX preferred
GEX_ETF = "SPY"
# gamma_state -> dot color. Negative gamma is the fragile/high-vol regime.
GEX_STATE_DOT = {"Positive": "ok", "Neutral": "info", "Negative": "warn"}


def _gex_latest(symbol: str):
    """Return the latest day's GEX row (dict) for a symbol, or None if no table yet."""
    import pandas as pd
    path = DERIVED / f"{symbol}_gex_daily.parquet"
    if not path.exists():
        return None
    df = pd.read_parquet(path)
    if df.empty:
        return None
    # tables are written sorted ascending by date; be defensive and sort anyway.
    df = df.sort_values("date")
    return df.iloc[-1].to_dict()


def _gex_fmt_mag(net_gex):
    """Human $GEX magnitude (per 1% move): $1.2B / $345M / $12K, signed."""
    try:
        v = float(net_gex)
    except (TypeError, ValueError):
        return "—"
    if v != v:  # NaN
        return "—"
    sign = "+" if v >= 0 else "−"
    a = abs(v)
    if a >= 1e9:
        return f"{sign}${a/1e9:.2f}B"
    if a >= 1e6:
        return f"{sign}${a/1e6:.0f}M"
    if a >= 1e3:
        return f"{sign}${a/1e3:.0f}K"
    return f"{sign}${a:,.0f}"


def _gex_num(v, suffix="", nd=2):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "—"
    if f != f:
        return "—"
    return f"{f:,.{nd}f}{suffix}"


def _gex_oneliner(sym, r):
    """A compact spot/state/flip line for a single symbol's row."""
    state = r.get("gamma_state", "?")
    return (f"{sym} {state} · net {_gex_fmt_mag(r.get('net_gex'))} · "
            f"spot {_gex_num(r.get('spot'))} · flip {_gex_num(r.get('gamma_flip'))} "
            f"({_gex_num(r.get('dist_to_flip_pct'), '%', 2)} away) · "
            f"exp move {_gex_num(r.get('expected_move_pct'), '%', 2)}")


def build_gex():
    """Dealer Gamma — latest derived GEX for the index (SPX) + ETF (SPY)."""
    # pick the first index table that exists (SPX preferred over SPXW)
    idx_sym, idx = None, None
    for sym in GEX_INDEX:
        r = _gex_latest(sym)
        if r is not None:
            idx_sym, idx = sym, r
            break
    etf = _gex_latest(GEX_ETF)

    if idx is None and etf is None:
        return _sec("gex", "Dealer Gamma", "info",
                    "No derived GEX tables yet — warehouse build still landing.", [])

    primary = idx if idx is not None else etf
    primary_sym = idx_sym if idx is not None else GEX_ETF
    state = primary.get("gamma_state", "?")
    st = GEX_STATE_DOT.get(state, "info")

    flip_dir = "above" if primary.get("above_flip") else "below"
    headline = (f"{primary_sym} dealer gamma is {state.upper()} "
                f"(spot {flip_dir} the {_gex_num(primary.get('gamma_flip'))} flip)")
    if state == "Negative":
        headline += " — fragile / vol-amplifying regime"
    elif state == "Positive":
        headline += " — pinning / vol-dampening regime"

    rows = []
    if idx is not None:
        rows += [
            (f"{idx_sym} as-of", idx.get("date")),
            (f"{idx_sym} gamma state", idx.get("gamma_state")),
            (f"{idx_sym} net GEX (per 1%)", _gex_fmt_mag(idx.get("net_gex"))),
            (f"{idx_sym} spot", _gex_num(idx.get("spot"))),
            (f"{idx_sym} gamma flip", _gex_num(idx.get("gamma_flip"))),
            (f"{idx_sym} dist to flip", _gex_num(idx.get("dist_to_flip_pct"), "%", 2)),
            (f"{idx_sym} expected move", _gex_num(idx.get("expected_move_pct"), "%", 2)),
            (f"{idx_sym} focal strike", _gex_num(idx.get("focal_strike"), nd=0)),
        ]
    else:
        rows.append(("Index (SPX/SPXW)", "no table yet"))
    if etf is not None:
        rows.append((f"{GEX_ETF} line", _gex_oneliner(GEX_ETF, etf)))
    else:
        rows.append((f"{GEX_ETF} line", "no table yet"))

    # stale if the primary table's last day isn't today's run date.
    if str(primary.get("date")) != TODAY_STR:
        st = "stale" if st in ("ok", "info") else st
        headline += f"  ⚠ latest GEX is {primary.get('date')}, not today"

    return _sec("gex", "Dealer Gamma", st, headline, rows)


def build_alarm():
    """Staleness-alarm watchdog (mutual coverage). The heartbeat_alarm task stamps
    warehouse\\heartbeat_alarm_ran.txt every sweep (~15 min). If that marker is stale
    (>30 min) or missing, the alarm itself may be dead — turn this section red so the
    digest the user reads flags it. This is the reciprocal of the alarm's own
    handle_deadline check on the EOD report."""
    now = dt.datetime.now()
    if not ALARM_RAN_MARKER.exists():
        return _sec("alarm", "Staleness Alarm (watchdog)", "fail",
                    "Staleness alarm has never run — heartbeat_alarm_ran.txt is missing. "
                    "The watchdog itself may be dead (task HeartbeatStalenessAlarm).", [])
    try:
        mtime = dt.datetime.fromtimestamp(ALARM_RAN_MARKER.stat().st_mtime)
        age_min = (now - mtime).total_seconds() / 60.0
        last_line = ALARM_RAN_MARKER.read_text().strip().splitlines()[-1] if \
            ALARM_RAN_MARKER.read_text().strip() else "(empty)"
    except Exception as e:
        return _sec("alarm", "Staleness Alarm (watchdog)", "fail",
                    f"Could not read the alarm marker: {type(e).__name__}: {e}", [])

    rows = [("Last ran", mtime.strftime("%Y-%m-%d %H:%M:%S")),
            ("Age", f"{int(age_min)}m"),
            ("Marker", str(ALARM_RAN_MARKER)),
            ("Owning task", "HeartbeatStalenessAlarm")]
    if age_min > 30:
        return _sec("alarm", "Staleness Alarm (watchdog)", "fail",
                    f"Staleness alarm has not run in {int(age_min)}m — the watchdog "
                    f"itself may be dead. Check task HeartbeatStalenessAlarm.", rows)
    return _sec("alarm", "Staleness Alarm (watchdog)", "ok",
                f"Alarm is alive — last ran {int(age_min)}m ago ({last_line}).", rows)


SECTIONS = [build_forward, build_thetadata, build_tiingo, build_gex, build_system,
            build_strategy, build_alarm]


# --------------------------------------------------------------------------- #
# Render
# --------------------------------------------------------------------------- #
def _overall(sections):
    worst = "ok"
    for s in sections:
        st = s.get("status", "fail")
        idx = SEVERITY.index(st) if st in SEVERITY else len(SEVERITY) - 1
        if idx > SEVERITY.index(worst):
            worst = st if st in SEVERITY else "fail"
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


def _run_section(build, timeout: float = 30.0):
    """Run one section builder with a hard timeout so no single builder (a slow file
    read, a wedged probe) can stall or crash the whole report. Returns a section dict;
    never raises. A builder that overruns is abandoned (daemon thread) and rendered as
    a 'fail: timed out' section so the email still goes out."""
    result: dict = {}

    def worker():
        try:
            result["sec"] = build()
        except Exception as e:
            result["err"] = f"{type(e).__name__}: {e}"

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        return _sec(build.__name__, build.__name__, "fail",
                    f"section timed out after {int(timeout)}s (skipped so the report could send)", [])
    if "sec" in result:
        return result["sec"]
    return _sec(build.__name__, build.__name__, "fail",
                f"section error: {result.get('err', 'unknown')}", [])


def main() -> bool:
    """Build + email the EOD digest. Returns whether the email was actually sent
    (so __main__ can exit non-zero -> Task Scheduler shows the run red)."""
    _log(f"=== EOD report {TODAY_STR} start ===")
    sent = False
    try:
        sections = [_run_section(build) for build in SECTIONS]
        overall = _overall(sections)
        html = render_html(sections, overall)
        subject = f"Trading Desk EOD — {TODAY.strftime('%b %d')} — {overall.upper()}"
        sent = mailer.send_html(subject, html)
        _log(f"sections={[s['status'] for s in sections]} overall={overall} "
             f"emailed={'YES' if sent else 'NO'} -> {mailer.recipient()}")
        status.write("eod_report", "ok" if sent else "fail",
                     metrics={"overall": overall, "emailed": sent}, day=TODAY_STR)
    except Exception as e:
        # Last-resort guard: the generator itself failed. Still send SOMETHING and
        # record a fail status so the independent watchdog also alarms.
        import traceback
        tb = traceback.format_exc()
        _log(f"FATAL in main(): {type(e).__name__}: {e}\n{tb}")
        sent = False   # generator failed; the fallback below re-computes this
        try:
            fb_html = (
                f'<div style="font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;'
                f'max-width:640px;margin:0 auto;color:#111827;">'
                f'<div style="font-size:16px;font-weight:700;color:#ef4444;">'
                f'Trading Desk EOD — generator error</div>'
                f'<div style="font-size:13px;margin-top:8px;">The end-of-day report failed to '
                f'build. Raw error below.</div>'
                f'<pre style="font-size:12px;white-space:pre-wrap;background:#f9fafb;'
                f'border:1px solid #e5e7eb;border-radius:6px;padding:10px;">'
                f'{type(e).__name__}: {e}\n\n{tb}</pre></div>')
            sent = mailer.send_html(f"Trading Desk EOD — {TODAY.strftime('%b %d')} — ERROR", fb_html)
        except Exception as e2:
            _log(f"FATAL fallback email also failed: {type(e2).__name__}: {e2}")
        try:
            status.write("eod_report", "fail",
                         metrics={"overall": "fail", "emailed": sent,
                                  "error": f"{type(e).__name__}: {e}"}, day=TODAY_STR)
        except Exception:
            pass
    _log(f"=== EOD report {TODAY_STR} done (emailed={'YES' if sent else 'NO'}) ===")
    return sent


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
