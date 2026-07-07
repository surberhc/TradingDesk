#!/usr/bin/env python3
"""
rrg_report.py — build the one-file HTML "morning/evening report" from the
outputs rrg_compute.py already produces (rrg_readout.txt, rrg_table.csv,
rrg_quadrant.png) in a given directory.

Two consumers:
  * standalone  -> python rrg_report.py <out_dir> [dest.html]   (chart as data-URI)
  * the emailer -> build_report_html(out_dir, embed='cid')      (chart as cid:rrgchart)
"""

import csv, base64, os, sys, re

CHART_CID = "rrgchart"          # Content-ID used when embedding inline in email

QCOLOR = {
    "Leading":   ("#1b7a3d", "#e3f5e9"),
    "Improving": ("#1f5fa6", "#e4eefb"),
    "Weakening": ("#8a5a06", "#faf0d8"),
    "Lagging":   ("#a32d2d", "#fbe7e7"),
    "n/a":       ("#666666", "#f0f0f0"),
}


def _read(out_dir):
    with open(os.path.join(out_dir, "rrg_readout.txt"), encoding="utf-8", errors="replace") as f:
        readout = f.read()
    with open(os.path.join(out_dir, "rrg_table.csv"), newline="") as f:
        rows = list(csv.reader(f))
    return readout, rows


def _grab(readout, pat, default=""):
    m = re.search(pat, readout)
    return m.group(1).strip() if m else default


def _section(readout, title):
    m = re.search(re.escape(title) + r"\n(.*?)(?:\n\n|\Z)", readout, re.S)
    return m.group(1).rstrip() if m else ""


def _cell(q):
    fg, bg = QCOLOR.get(q, QCOLOR["n/a"])
    return (f'<td style="padding:6px 10px;border-bottom:1px solid #eee;">'
            f'<span style="background:{bg};color:{fg};padding:2px 8px;border-radius:10px;'
            f'font-size:12px;font-weight:600;">{q}</span></td>')


# ---- regime history (strip + flip log) -------------------------------------
TILT_COLOR = {"RISK-ON": "#1b7a3d", "RISK-OFF": "#a32d2d", "BALANCED": "#888888"}


def _read_regime(out_dir):
    path = os.path.join(out_dir, "rrg_regime.csv")
    data = {"weekly": [], "daily": []}
    if not os.path.isfile(path):
        return data
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            data.setdefault(row["timeframe"], []).append((row["date"], row["tilt"]))
    return data


def _runs(seq):
    runs = []
    for (d, tilt) in seq:
        if runs and runs[-1]["tilt"] == tilt:
            runs[-1]["end"] = d
            runs[-1]["n"] += 1
        else:
            runs.append({"tilt": tilt, "start": d, "end": d, "n": 1})
    return runs


def _strip_html(seq):
    if not seq:
        return '<div style="color:#999;font-size:12px;">(no history)</div>'
    w = max(3, min(20, int(560 / len(seq))))
    cells = "".join(
        f'<td bgcolor="{TILT_COLOR.get(t, "#888")}" width="{w}" '
        f'style="height:16px;background:{TILT_COLOR.get(t, "#888")};" title="{d} {t}">&nbsp;</td>'
        for (d, t) in seq)
    return ('<table cellpadding="0" cellspacing="0" border="0" '
            f'style="border-collapse:collapse;width:100%;"><tr>{cells}</tr></table>')


def _log_html(seq, unit, whip):
    runs = _runs(seq)
    if not runs:
        return '<div style="color:#999;font-size:12px;">(insufficient history)</div>'
    rows = []
    for i, r in enumerate(runs):
        end = "now" if i == len(runs) - 1 else r["end"]
        fg = TILT_COLOR.get(r["tilt"], "#888")
        whipmark = ('<span style="color:#a32d2d;font-weight:600;"> &#9888; whipsaw</span>'
                    if r["n"] < whip else "")
        rows.append(
            f'<tr><td style="padding:2px 10px 2px 0;"><span style="color:{fg};font-weight:600;">{r["tilt"]}</span></td>'
            f'<td style="padding:2px 10px;color:#444;">{r["start"]} &rarr; {end}</td>'
            f'<td style="padding:2px 0;color:#444;">{r["n"]} {unit}{whipmark}</td></tr>')
    return f'<table style="font-size:12px;border-collapse:collapse;">{"".join(rows)}</table>'


def _regime_section(out_dir):
    data = _read_regime(out_dir)
    wk, dl = data.get("weekly", []), data.get("daily", [])
    if not wk and not dl:
        return ""

    def block(title, seq, unit, whip):
        start = seq[0][0] if seq else ""
        return (
            '<div style="margin-bottom:14px;">'
            f'<div style="font-size:13px;font-weight:600;color:#222;margin-bottom:4px;">{title}</div>'
            f'{_strip_html(seq)}'
            f'<div style="font-size:10px;color:#999;margin:2px 0 6px;">{start} &nbsp;&middot;&nbsp; '
            'green = risk-on &nbsp; red = risk-off &nbsp;&middot;&nbsp; now</div>'
            f'{_log_html(seq, unit, whip)}'
            '</div>')

    return (
        '<div style="padding:6px 22px 4px;">'
        '<div style="font-size:15px;font-weight:600;margin:6px 0 8px;">Regime history (trailing ~6 months)</div>'
        f'{block("Weekly tilt", wk, "wk", 3)}'
        f'{block("Daily tilt", dl, "d", 5)}'
        '</div>')


def build_report_html(out_dir, embed="data"):
    """embed='data' -> standalone (data-URI chart); 'cid' -> inline-email chart."""
    readout, rows = _read(out_dir)

    gen    = _grab(readout, r"generated ([0-9: \-]+)")
    tilt   = _grab(readout, r"Overall tilt: ([A-Z\-]+)")
    counts = _grab(readout, r"(Leading: \d+.*Lagging: \d+)")
    tilt_color = "#a32d2d" if "OFF" in tilt else "#1b7a3d" if "ON" in tilt else "#888888"

    if embed == "cid":
        chart_src = f"cid:{CHART_CID}"
    else:
        with open(os.path.join(out_dir, "rrg_quadrant.png"), "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        chart_src = f"data:image/png;base64,{b64}"

    body = []
    for r in rows[1:]:
        sym, sector, dq, drr, drm, wq, wrr, wrm, div = r
        divmark = '<span style="color:#a32d2d;font-weight:600;">&#9679;</span>' if div == "YES" else ""
        body.append(
            "<tr>"
            f'<td style="padding:6px 10px;border-bottom:1px solid #eee;font-weight:600;">{sym}</td>'
            f'<td style="padding:6px 10px;border-bottom:1px solid #eee;color:#444;">{sector}</td>'
            f"{_cell(dq)}"
            f'<td style="padding:6px 10px;border-bottom:1px solid #eee;text-align:right;">{drr}</td>'
            f"{_cell(wq)}"
            f'<td style="padding:6px 10px;border-bottom:1px solid #eee;text-align:right;">{wrr}</td>'
            f'<td style="padding:6px 10px;border-bottom:1px solid #eee;text-align:center;">{divmark}</td>'
            "</tr>")
    body = "\n".join(body)

    positions = _section(readout, "PLAIN-ENGLISH READOUT (weekly positions)")
    rotation  = _section(readout, "ROTATION THIS WEEK (weekly quadrant changes)")
    regime_html = _regime_section(out_dir)

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>RRG Report</title></head>
<body style="margin:0;padding:0;background:#f4f5f7;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#1a1a1a;">
<div style="max-width:640px;margin:0 auto;background:#ffffff;">
  <div style="background:#002070;color:#fff;padding:18px 22px;">
    <div style="font-size:18px;font-weight:600;">RRG Evening Report &mdash; Sector Rotation vs SPY</div>
    <div style="font-size:13px;opacity:.85;margin-top:3px;">Generated {gen} &nbsp;|&nbsp; Method: classic JdK &nbsp;|&nbsp; daily + weekly</div>
  </div>
  <div style="padding:16px 22px;">
    <div style="display:inline-block;background:{tilt_color};color:#fff;padding:6px 14px;border-radius:6px;font-weight:600;font-size:15px;">Weekly tilt: {tilt}</div>
    <div style="margin-top:8px;color:#444;font-size:14px;">{counts}</div>
  </div>
  {regime_html}
  <div style="padding:0 22px 8px;">
    <img src="{chart_src}" alt="RRG quadrant chart" style="width:100%;height:auto;border:1px solid #eee;border-radius:6px;">
  </div>
  <div style="padding:8px 22px 0;">
    <div style="font-size:15px;font-weight:600;margin:10px 0 6px;">Ranked rotation table</div>
    <table style="border-collapse:collapse;width:100%;font-size:13px;">
      <thead><tr style="text-align:left;color:#666;font-size:12px;">
        <th style="padding:6px 10px;">Sym</th><th style="padding:6px 10px;">Sector</th>
        <th style="padding:6px 10px;">Daily</th><th style="padding:6px 10px;text-align:right;">D-RS</th>
        <th style="padding:6px 10px;">Weekly</th><th style="padding:6px 10px;text-align:right;">W-RS</th>
        <th style="padding:6px 10px;text-align:center;">Div</th>
      </tr></thead>
      <tbody>{body}</tbody>
    </table>
    <div style="color:#888;font-size:11px;margin-top:4px;">Ranked by weekly RS-Ratio (strength). <span style="color:#a32d2d;">&#9679;</span> = daily/weekly disagree.</div>
  </div>
  <div style="padding:14px 22px;">
    <div style="font-size:15px;font-weight:600;margin-bottom:6px;">This week's positions</div>
    <pre style="white-space:pre-wrap;font-family:ui-monospace,Consolas,monospace;font-size:12px;background:#f7f8fa;border:1px solid #eee;border-radius:6px;padding:10px;margin:0;">{positions}</pre>
    <div style="font-size:15px;font-weight:600;margin:14px 0 6px;">Rotation this week</div>
    <pre style="white-space:pre-wrap;font-family:ui-monospace,Consolas,monospace;font-size:12px;background:#f7f8fa;border:1px solid #eee;border-radius:6px;padding:10px;margin:0;">{rotation}</pre>
  </div>
  <div style="padding:12px 22px 22px;color:#999;font-size:11px;border-top:1px solid #eee;">
    Full numbers attached as rrg_table.csv &nbsp;|&nbsp; queryable in rrg.db &rarr; rrg_values.<br>
    Automated RRG pipeline &middot; TFR Market Data.
  </div>
</div>
</body></html>"""


def write_standalone(out_dir, dest=None):
    dest = dest or os.path.join(out_dir, "rrg_report.html")
    html = build_report_html(out_dir, embed="data")
    with open(dest, "w", encoding="utf-8") as f:
        f.write(html)
    return dest


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "."
    dst = sys.argv[2] if len(sys.argv) > 2 else None
    print("wrote", write_standalone(out, dst))
