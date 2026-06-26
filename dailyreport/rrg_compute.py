#!/usr/bin/env python3
"""
rrg_compute.py — RRG computation + multi-view output layer for TFR market-data pipeline.

Reads bars from rrg.db, computes RS-Ratio and RS-Momentum (classic Julius/StockCharts
method) for each sector ETF vs SPY, on BOTH daily and weekly timeframes. Stores results
back into rrg.db (rrg_values table) and emits four views:
  1. Plain-English readout  (rrg_readout.txt)  -- includes regime summary at top
  2. Ranked rotation table  (rrg_table.csv)
  3. Quadrant chart         (rrg_quadrant.png)
  4. Regime summary         (top of readout)
Daily vs weekly shown side by side, with a divergence flag where the two disagree.

Read-only on the bars table. Creates/replaces its own rrg_values table only.
"""

import sqlite3
import sys
import os
import csv
from datetime import datetime, timedelta

# ---- config ----------------------------------------------------------------
DB_PATH = sys.argv[1] if len(sys.argv) > 1 else "rrg.db"
OUT_DIR = sys.argv[2] if len(sys.argv) > 2 else "."
if OUT_DIR and not os.path.isdir(OUT_DIR):
    os.makedirs(OUT_DIR, exist_ok=True)
BENCHMARK = "SPY"
RS_RATIO_WINDOW = 10      # smoothing window for the relative-strength ratio
RS_MOM_WINDOW = 10        # smoothing window for momentum (ROC of the ratio)
TAIL_LEN = 8              # how many periods of "tail" to keep per symbol/timeframe
HISTORY_DAYS = 190        # ~6 months of regime-tilt history to retain
WHIPSAW_DAILY = 5         # a daily regime shorter than this many days is flagged
WHIPSAW_WEEKLY = 3        # a weekly regime shorter than this many weeks is flagged
LEAD_MIN = 7              # sectors (of 11) that must agree to call ON/OFF, else NEUTRAL
CONFIRM = 2               # consecutive periods a new state must hold before it commits

SECTOR_NAMES = {
    "XLB": "Materials", "XLC": "Communication Svcs", "XLE": "Energy",
    "XLF": "Financials", "XLI": "Industrials", "XLK": "Technology",
    "XLP": "Consumer Staples", "XLRE": "Real Estate", "XLU": "Utilities",
    "XLV": "Health Care", "XLY": "Consumer Discretionary",
}

# ---- tiny stats helpers (no numpy dependency assumed) ----------------------
def sma(values, window):
    out = [None] * len(values)
    for i in range(len(values)):
        if i + 1 >= window:
            seg = values[i + 1 - window:i + 1]
            if all(v is not None for v in seg):
                out[i] = sum(seg) / window
    return out

def stdev(seg):
    n = len(seg)
    if n < 2:
        return None
    m = sum(seg) / n
    var = sum((x - m) ** 2 for x in seg) / (n - 1)
    return var ** 0.5

def quadrant(ratio, mom):
    # JdK convention: ratio/mom centered on 100
    if ratio >= 100 and mom >= 100:
        return "Leading"
    if ratio >= 100 and mom < 100:
        return "Weakening"
    if ratio < 100 and mom < 100:
        return "Lagging"
    return "Improving"

# ---- regime tilt history ---------------------------------------------------
def regime_series(full, sectors, tf):
    """Per-date risk-on/off tilt by simple majority of the 11 sectors' quadrants."""
    tally = {}
    for sym in sectors:
        for (d, r, m) in full[sym].get(tf, []):
            q = quadrant(r, m)
            t = tally.setdefault(d, [0, 0, 0])   # [risk_on, risk_off, total]
            if q in ("Leading", "Improving"):
                t[0] += 1
            elif q in ("Lagging", "Weakening"):
                t[1] += 1
            t[2] += 1
    out = []
    for d in sorted(tally):
        on, off, tot = tally[d]
        if tot < 10:                              # skip warmup dates missing sectors
            continue
        tilt = "RISK-ON" if on > off else "RISK-OFF" if off > on else "BALANCED"
        out.append((d, tilt, on, off))
    return out

def within_days(history, days):
    """Keep only rows whose date is within the last `days` calendar days."""
    cutoff = datetime.now() - timedelta(days=days)
    out = []
    for row in history:
        try:
            dt = datetime.strptime(row[0][:10], "%Y-%m-%d")
        except ValueError:
            out.append(row)                       # unparseable date -> keep, don't lose data
            continue
        if dt >= cutoff:
            out.append(row)
    return out

def stabilize(history, lead_min, confirm):
    """Clear-lead neutral band + confirmation hysteresis over a raw tilt series.
    A side is only called when >= lead_min of the 11 sectors agree, and the
    committed tilt only changes after the new state holds `confirm` periods in a
    row. Returns [(date, committed, on, off), ...] with committed in
    RISK-ON / RISK-OFF / NEUTRAL."""
    out = []
    committed = "NEUTRAL"
    run_state, run_len = None, 0
    for (d, _raw, on, off) in history:
        if on >= lead_min:
            raw = "RISK-ON"
        elif off >= lead_min:
            raw = "RISK-OFF"
        else:
            raw = "NEUTRAL"
        if raw == run_state:
            run_len += 1
        else:
            run_state, run_len = raw, 1
        if run_len >= confirm:
            committed = raw
        out.append((d, committed, on, off))
    return out


def build_runs(history):
    """Compress a per-date tilt series into consecutive runs (the flip log)."""
    runs = []
    for (d, tilt, on, off) in history:
        if runs and runs[-1]["tilt"] == tilt:
            runs[-1]["end"] = d
            runs[-1]["n"] += 1
        else:
            runs.append({"tilt": tilt, "start": d, "end": d, "n": 1})
    return runs

# ---- core RRG math (classic method) ----------------------------------------
def compute_rrg(closes_sym, closes_bench):
    """
    closes_sym, closes_bench: equal-length lists of aligned closes (oldest->newest).
    Returns lists (ratio, momentum) normalized ~100, classic JdK RRG method:
      RS        = 100 * price_sym / price_bench
      RS-Ratio  = 100 + (RS - SMA(RS)) / stdev(RS) * 1   (z-score recentred on 100)
      RS-Mom    = 100 + ROC of RS-Ratio, z-scored and recentred on 100
    """
    n = len(closes_sym)
    rs = [100.0 * s / b if b else None for s, b in zip(closes_sym, closes_bench)]

    # RS-Ratio: z-score of RS over rolling window, recentred on 100
    rs_ratio = [None] * n
    for i in range(n):
        if i + 1 >= RS_RATIO_WINDOW:
            seg = rs[i + 1 - RS_RATIO_WINDOW:i + 1]
            if all(v is not None for v in seg):
                m = sum(seg) / RS_RATIO_WINDOW
                sd = stdev(seg)
                if sd and sd > 0:
                    rs_ratio[i] = 100 + (rs[i] - m) / sd

    # RS-Momentum: ROC of the RS-Ratio, z-scored, recentred on 100
    roc = [None] * n
    for i in range(1, n):
        if rs_ratio[i] is not None and rs_ratio[i - 1] is not None and rs_ratio[i - 1] != 0:
            roc[i] = (rs_ratio[i] - rs_ratio[i - 1])
    rs_mom = [None] * n
    for i in range(n):
        if i + 1 >= RS_MOM_WINDOW:
            seg = roc[i + 1 - RS_MOM_WINDOW:i + 1]
            if all(v is not None for v in seg):
                m = sum(seg) / RS_MOM_WINDOW
                sd = stdev(seg)
                if sd and sd > 0:
                    rs_mom[i] = 100 + (roc[i] - m) / sd
    return rs_ratio, rs_mom

# ---- load ------------------------------------------------------------------
def load_closes(conn, symbol, timeframe):
    rows = conn.execute(
        "SELECT date, close FROM bars WHERE symbol=? AND timeframe=? ORDER BY date ASC",
        (symbol, timeframe),
    ).fetchall()
    return [r[0] for r in rows], [r[1] for r in rows]

def align(dates_a, vals_a, dates_b, vals_b):
    """Align two date-series on common dates, oldest->newest."""
    mb = dict(zip(dates_b, vals_b))
    cd, va, vb = [], [], []
    for d, v in zip(dates_a, vals_a):
        if d in mb:
            cd.append(d); va.append(v); vb.append(mb[d])
    return cd, va, vb

# ---- main ------------------------------------------------------------------
def main():
    conn = sqlite3.connect(DB_PATH)
    timeframes = ["daily", "weekly"]
    sectors = sorted(SECTOR_NAMES.keys())

    # results[symbol][timeframe] = list of (date, ratio, mom) for the tail
    results = {s: {} for s in sectors}
    full = {s: {} for s in sectors}        # untruncated series, for regime history

    for tf in timeframes:
        bd, bc = load_closes(conn, BENCHMARK, tf)
        for sym in sectors:
            sd, sc = load_closes(conn, sym, tf)
            cd, va, vb = align(sd, sc, bd, bc)
            if len(cd) < RS_RATIO_WINDOW + RS_MOM_WINDOW + 2:
                results[sym][tf] = []
                full[sym][tf] = []
                continue
            ratio, mom = compute_rrg(va, vb)
            series = [(cd[i], ratio[i], mom[i]) for i in range(len(cd))
                      if ratio[i] is not None and mom[i] is not None]
            full[sym][tf] = series
            results[sym][tf] = series[-TAIL_LEN:]

    # ---- store back into rrg_values -------------------------------------
    conn.execute("DROP TABLE IF EXISTS rrg_values")
    conn.execute("""CREATE TABLE rrg_values(
        symbol TEXT, date TEXT, timeframe TEXT,
        rs_ratio REAL, rs_momentum REAL, quadrant TEXT,
        computed_at TEXT,
        PRIMARY KEY(symbol, date, timeframe))""")
    now = datetime.now().isoformat()
    for sym in sectors:
        for tf in timeframes:
            for (d, r, m) in results[sym][tf]:
                conn.execute(
                    "INSERT OR REPLACE INTO rrg_values VALUES (?,?,?,?,?,?,?)",
                    (sym, d, tf, round(r, 3), round(m, 3), quadrant(r, m), now))
    conn.commit()

    # ---- regime tilt history (daily + weekly, trailing ~6 months) -------
    # stabilize over FULL history (so the state entering the window is correct),
    # then trim to the trailing window.
    regime = {tf: within_days(
                  stabilize(regime_series(full, sectors, tf), LEAD_MIN, CONFIRM),
                  HISTORY_DAYS)
              for tf in timeframes}

    conn.execute("DROP TABLE IF EXISTS rrg_regime")
    conn.execute("""CREATE TABLE rrg_regime(
        date TEXT, timeframe TEXT, tilt TEXT,
        risk_on INTEGER, risk_off INTEGER, computed_at TEXT,
        PRIMARY KEY(date, timeframe))""")
    for tf in timeframes:
        for (d, tilt, on, off) in regime[tf]:
            conn.execute("INSERT OR REPLACE INTO rrg_regime VALUES (?,?,?,?,?,?)",
                         (d, tf, tilt, on, off, now))
    conn.commit()

    regime_csv = os.path.join(OUT_DIR, "rrg_regime.csv")
    with open(regime_csv, "w", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(["date", "timeframe", "tilt", "risk_on", "risk_off"])
        for tf in timeframes:
            for (d, tilt, on, off) in regime[tf]:
                wr.writerow([d, tf, tilt, on, off])

    # ---- assemble latest snapshot ---------------------------------------
    def latest(sym, tf):
        s = results[sym][tf]
        return s[-1] if s else None

    def prev(sym, tf):
        s = results[sym][tf]
        return s[-2] if len(s) >= 2 else None

    snapshot = []
    for sym in sectors:
        ld = latest(sym, "daily")
        lw = latest(sym, "weekly")
        snapshot.append({
            "sym": sym, "name": SECTOR_NAMES[sym],
            "d": ld, "w": lw,
            "dq": quadrant(ld[1], ld[2]) if ld else "n/a",
            "wq": quadrant(lw[1], lw[2]) if lw else "n/a",
        })

    # ---- VIEW: ranked rotation table (CSV) ------------------------------
    csv_path = os.path.join(OUT_DIR, "rrg_table.csv")
    with open(csv_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Symbol", "Sector",
                    "Daily_Quadrant", "Daily_RS-Ratio", "Daily_RS-Mom",
                    "Weekly_Quadrant", "Weekly_RS-Ratio", "Weekly_RS-Mom",
                    "Divergence"])
        # rank by weekly RS-Ratio desc (strength)
        for row in sorted(snapshot, key=lambda x: (x["w"][1] if x["w"] else -999), reverse=True):
            div = "YES" if row["dq"] != row["wq"] else ""
            w.writerow([
                row["sym"], row["name"],
                row["dq"], f'{row["d"][1]:.2f}' if row["d"] else "n/a",
                f'{row["d"][2]:.2f}' if row["d"] else "n/a",
                row["wq"], f'{row["w"][1]:.2f}' if row["w"] else "n/a",
                f'{row["w"][2]:.2f}' if row["w"] else "n/a",
                div])

    # ---- VIEW: regime summary + plain-English readout (TXT) -------------
    def count_q(key):
        c = {"Leading": 0, "Weakening": 0, "Lagging": 0, "Improving": 0}
        for row in snapshot:
            q = row[key]
            if q in c:
                c[q] += 1
        return c

    wc = count_q("wq")
    # headline tilt = latest STABILIZED weekly state (matches the history below)
    tilt = regime["weekly"][-1][1] if regime["weekly"] else "NEUTRAL"

    lines = []
    lines.append("=" * 70)
    lines.append("RRG MARKET READOUT  —  generated " + datetime.now().strftime("%Y-%m-%d %H:%M"))
    lines.append("Benchmark: SPY   |   Method: classic JdK   |   Timeframes: daily + weekly")
    lines.append("=" * 70)
    lines.append("")
    lines.append("REGIME SUMMARY (weekly)")
    lines.append(f"  Overall tilt: {tilt}   "
                 f"(stabilized: {LEAD_MIN}-of-11 lead, {CONFIRM}-period confirm)")
    lines.append(f"  Leading: {wc['Leading']}   Improving: {wc['Improving']}   "
                 f"Weakening: {wc['Weakening']}   Lagging: {wc['Lagging']}")
    lines.append("")

    # movement narration: compare latest vs prev weekly quadrant
    def moved(sym):
        s = results[sym]["weekly"]
        if len(s) < 2:
            return None
        pq = quadrant(s[-2][1], s[-2][2])
        cq = quadrant(s[-1][1], s[-1][2])
        return (pq, cq) if pq != cq else None

    leading = [r["name"] for r in snapshot if r["wq"] == "Leading"]
    improving = [r["name"] for r in snapshot if r["wq"] == "Improving"]
    weakening = [r["name"] for r in snapshot if r["wq"] == "Weakening"]
    lagging = [r["name"] for r in snapshot if r["wq"] == "Lagging"]

    def joinlist(x):
        return ", ".join(x) if x else "(none)"

    lines.append("PLAIN-ENGLISH READOUT (weekly positions)")
    lines.append(f"  Leading & strong:   {joinlist(leading)}")
    lines.append(f"  Improving (rising): {joinlist(improving)}")
    lines.append(f"  Weakening (rolling over): {joinlist(weakening)}")
    lines.append(f"  Lagging & weak:     {joinlist(lagging)}")
    lines.append("")
    lines.append("ROTATION THIS WEEK (weekly quadrant changes)")
    any_move = False
    for sym in sectors:
        mv = moved(sym)
        if mv:
            any_move = True
            lines.append(f"  {SECTOR_NAMES[sym]:24s} {mv[0]} -> {mv[1]}")
    if not any_move:
        lines.append("  (no weekly quadrant changes in the latest period)")
    lines.append("")
    lines.append("DAILY vs WEEKLY DIVERGENCE (fast signal disagrees with slow)")
    any_div = False
    for row in snapshot:
        if row["dq"] != row["wq"]:
            any_div = True
            lines.append(f"  {row['name']:24s} daily={row['dq']:10s} weekly={row['wq']}")
    if not any_div:
        lines.append("  (daily and weekly agree across all sectors)")
    lines.append("")

    # ---- regime tilt history (flip log) ---------------------------------
    def flip_log(tf, label, unit, whip):
        out = [f"{label} TILT HISTORY (trailing ~6 months, oldest first)"]
        runs = build_runs(regime[tf])
        if not runs:
            out.append("  (insufficient history)")
            return out
        for i, r in enumerate(runs):
            end = "now" if i == len(runs) - 1 else r["end"]
            mark = "   <- whipsaw" if r["n"] < whip else ""
            out.append(f"  {r['tilt']:8s} {r['start']} -> {end:10s} ({r['n']} {unit}){mark}")
        return out

    lines.append("REGIME HISTORY")
    lines += flip_log("weekly", "WEEKLY", "wk", WHIPSAW_WEEKLY)
    lines.append("")
    lines += flip_log("daily", "DAILY", "d", WHIPSAW_DAILY)
    lines.append("")
    lines.append("Full numbers in rrg_table.csv  |  quadrant chart in rrg_quadrant.png")
    lines.append("Regime strip + history in the HTML report  |  rrg_regime.csv")
    lines.append("Stored numbers queryable in rrg.db -> rrg_values / rrg_regime")

    txt_path = os.path.join(OUT_DIR, "rrg_readout.txt")
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    # ---- VIEW: quadrant chart (PNG) -------------------------------------
    chart_ok = False
    chart_msg = ""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig, axes = plt.subplots(1, 2, figsize=(16, 8))
        for ax, tf in zip(axes, ["daily", "weekly"]):
            ax.axhline(100, color="#888", lw=0.8)
            ax.axvline(100, color="#888", lw=0.8)
            # quadrant tints
            ax.fill_betweenx([100, 200], 100, 200, color="#C8F8C8", alpha=0.25)  # leading
            ax.fill_betweenx([0, 100], 100, 200, color="#F8F0C8", alpha=0.25)    # weakening
            ax.fill_betweenx([0, 100], 0, 100, color="#F8C8C8", alpha=0.25)      # lagging
            ax.fill_betweenx([100, 200], 0, 100, color="#C8D8F8", alpha=0.25)    # improving
            rr_all, mm_all = [], []
            for sym in sectors:
                s = results[sym][tf]
                if not s:
                    continue
                rr = [p[1] for p in s]; mm = [p[2] for p in s]
                rr_all += rr; mm_all += mm
                ax.plot(rr, mm, "-", color="#002070", lw=1, alpha=0.5)
                ax.plot(rr[-1], mm[-1], "o", color="#002070", ms=8)
                ax.annotate(sym, (rr[-1], mm[-1]),
                            textcoords="offset points", xytext=(6, 4), fontsize=9)
            if rr_all:
                pad = 1.0
                ax.set_xlim(min(rr_all) - pad, max(rr_all) + pad)
                ax.set_ylim(min(mm_all) - pad, max(mm_all) + pad)
            ax.set_title(f"RRG — {tf}", fontsize=13, color="#002070")
            ax.set_xlabel("RS-Ratio (relative strength)")
            ax.set_ylabel("RS-Momentum")
            ax.text(0.98, 0.98, "LEADING", transform=ax.transAxes, ha="right", va="top", color="#2a7", fontsize=9)
            ax.text(0.02, 0.98, "IMPROVING", transform=ax.transAxes, ha="left", va="top", color="#47a", fontsize=9)
            ax.text(0.98, 0.02, "WEAKENING", transform=ax.transAxes, ha="right", va="bottom", color="#a92", fontsize=9)
            ax.text(0.02, 0.02, "LAGGING", transform=ax.transAxes, ha="left", va="bottom", color="#a22", fontsize=9)
        fig.suptitle("Sector Rotation vs SPY", fontsize=15, color="#002070")
        fig.tight_layout()
        png_path = os.path.join(OUT_DIR, "rrg_quadrant.png")
        fig.savefig(png_path, dpi=110)
        chart_ok = True
    except Exception as e:
        chart_msg = f"chart skipped: {e}"

    # ---- console report --------------------------------------------------
    print("\n".join(lines))
    print("")
    print("OUTPUTS WRITTEN:")
    print(f"  {txt_path}")
    print(f"  {csv_path}")
    print(f"  {regime_csv}")
    if chart_ok:
        print(f"  {png_path}")
    else:
        print(f"  (PNG {chart_msg})")
    print(f"  rrg.db -> rrg_values table ({len(sectors)*2} symbol/timeframe series stored)")

if __name__ == "__main__":
    main()
