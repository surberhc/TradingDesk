#!/usr/bin/env python3
"""
Band calibration backtest: did the Probable Volatility Bands contain price?

For each report day d and ticker, the band is
    upper = last * (1 + upside_pct/100)   lower = last * (1 + downside_pct/100)
We then check the ticker's close h report-days later against [lower, upper].
A well-calibrated "probable" band should contain price most of the time at its
intended horizon; the containment-vs-horizon curve reveals what that horizon is.

Outputs PNG charts to the folder (band_*.png) and prints summary stats.
"""
import os
import sqlite3
import pandas as pd
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

ROOT = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(ROOT, "msr.db")
plt.rcParams.update({"figure.dpi": 130, "axes.grid": True, "grid.alpha": 0.3,
                     "font.size": 10})

con = sqlite3.connect(DB)
df = pd.read_sql("SELECT report_date,asset,last,upside_pct,downside_pct,spread_pct,rvol_1m FROM sector_bands", con)
con.close()
df = df.dropna(subset=["last", "upside_pct", "downside_pct"]).copy()
df["date"] = pd.to_datetime(df["report_date"])
df = df.sort_values(["asset", "date"]).reset_index(drop=True)
df["upper"] = df["last"] * (1 + df["upside_pct"] / 100)
df["lower"] = df["last"] * (1 + df["downside_pct"] / 100)

# forward close h report-days ahead, per ticker
for h in range(1, 11):
    df[f"fwd{h}"] = df.groupby("asset")["last"].shift(-h)

# Guard against the 2025-12-08 source rebasing (several ETFs re-based ~50% in a
# single step) and any residual >25%/day artifacts: no ETF truly moves that much
# day-to-day, so such "moves" are data steps, not returns — exclude from the test.
_step = (df["fwd1"] / df["last"] - 1).abs() > 0.25
df.loc[_step, [f"fwd{h}" for h in range(1, 11)]] = np.nan
print(f"excluded {int(_step.sum())} source-rebasing/step transitions from the backtest")

def contained(row, h):
    fp = row[f"fwd{h}"]
    if pd.isna(fp):
        return np.nan
    lo, hi = min(row["lower"], row["upper"]), max(row["lower"], row["upper"])
    return 1.0 if lo <= fp <= hi else 0.0

# ---- 1) hit rate by ticker (1-day-ahead) ----
df["hit1"] = df.apply(lambda r: contained(r, 1), axis=1)
by_tkr = df.groupby("asset")["hit1"].agg(["mean", "count"]).sort_values("mean")
fig, ax = plt.subplots(figsize=(9, 5.5))
ax.barh(by_tkr.index, by_tkr["mean"] * 100, color="#3b7dd8")
ax.axvline(68, color="#d8723b", ls="--", lw=1.4, label="68% (≈1σ)")
ax.axvline(90, color="#c0392b", ls="--", lw=1.4, label="90%")
ax.set_xlabel("% of next-report-day closes inside the band")
ax.set_title("Band calibration — next-day containment by ticker")
for i, (m, n) in enumerate(zip(by_tkr["mean"], by_tkr["count"])):
    ax.text(m * 100 + 1, i, f"{m*100:.0f}%", va="center", fontsize=8)
ax.legend(loc="lower right")
ax.set_xlim(0, 105)
fig.tight_layout(); fig.savefig(os.path.join(ROOT, "band_hitrate_by_ticker.png")); plt.close(fig)

# ---- 2) containment vs horizon ----
horizons = range(1, 11)
pooled = [df.apply(lambda r: contained(r, h), axis=1).mean() * 100 for h in horizons]
spy = df[df.asset == "SPY"]
spy_h = [spy.apply(lambda r: contained(r, h), axis=1).mean() * 100 for h in horizons]
fig, ax = plt.subplots(figsize=(8, 5))
ax.plot(list(horizons), pooled, "-o", label="All sectors (pooled)", color="#3b7dd8")
ax.plot(list(horizons), spy_h, "-o", label="SPY", color="#16a085")
ax.axhline(68, color="#d8723b", ls="--", lw=1.2, label="68% (≈1σ)")
ax.set_xlabel("Horizon (report-days ahead the band is tested against)")
ax.set_ylabel("% of closes still inside the band")
ax.set_title("How long do the bands hold? Containment vs horizon")
ax.legend(); ax.set_ylim(0, 100)
fig.tight_layout(); fig.savefig(os.path.join(ROOT, "band_containment_vs_horizon.png")); plt.close(fig)

# ---- 3) breach asymmetry (1-day) ----
def breach_dir(row):
    fp = row["fwd1"]
    if pd.isna(fp) or pd.isna(row["hit1"]):
        return None
    if row["hit1"] == 1.0:
        return "in"
    return "up" if fp > max(row["lower"], row["upper"]) else "down"
df["breach"] = df.apply(breach_dir, axis=1)
bt = df[df.breach.notna()].groupby(["asset", "breach"]).size().unstack(fill_value=0)
bt = bt.reindex(columns=["down", "in", "up"], fill_value=0)
bt_pct = bt.div(bt.sum(axis=1), axis=0) * 100
bt_pct = bt_pct.sort_values("down")
fig, ax = plt.subplots(figsize=(9, 5.5))
ax.barh(bt_pct.index, bt_pct["down"], color="#c0392b", label="breach DOWN")
ax.barh(bt_pct.index, bt_pct["up"], left=bt_pct["down"], color="#27ae60", label="breach UP")
ax.set_xlabel("% of next-day closes that breached the band (red=down, green=up)")
ax.set_title("Breach asymmetry — which way does price escape the band?")
ax.legend(loc="lower right")
fig.tight_layout(); fig.savefig(os.path.join(ROOT, "band_breach_asymmetry.png")); plt.close(fig)

# ---- 4) SPY bands vs price timeline ----
s = df[df.asset == "SPY"].sort_values("date")
fig, ax = plt.subplots(figsize=(11, 5.2))
ax.fill_between(s["date"], s["lower"], s["upper"], color="#3b7dd8", alpha=0.18, label="Probable band")
ax.plot(s["date"], s["last"], color="#16a085", lw=1.3, label="SPY close (report day)")
brk = s[s["hit1"] == 0.0]
ax.scatter(brk["date"], brk["last"], color="#c0392b", s=14, zorder=5, label="next-day breach")
ax.xaxis.set_major_formatter(mdates.DateFormatter("%b'%y"))
ax.set_title("SPY: probable band vs realized price")
ax.legend(loc="upper left")
fig.tight_layout(); fig.savefig(os.path.join(ROOT, "band_spy_timeline.png")); plt.close(fig)

# ---- 5) calibration: predicted expected move vs realized abs move ----
df["pred_halfwidth"] = (df["upside_pct"] - df["downside_pct"]) / 2  # ~ expected move %
df["realized_move"] = (df["fwd1"] / df["last"] - 1) * 100
sp = df[df.asset == "SPY"].dropna(subset=["realized_move"])
fig, ax = plt.subplots(figsize=(8, 5))
ax.hist(sp["realized_move"], bins=40, color="#3b7dd8", alpha=0.8)
mu_up = sp["upside_pct"].mean(); mu_dn = sp["downside_pct"].mean()
ax.axvline(mu_up, color="#27ae60", ls="--", label=f"avg upside band +{mu_up:.1f}%")
ax.axvline(mu_dn, color="#c0392b", ls="--", label=f"avg downside band {mu_dn:.1f}%")
ax.set_xlabel("SPY actual next-day move (%)")
ax.set_ylabel("frequency")
ax.set_title("SPY realized next-day moves vs the average predicted band")
ax.legend()
fig.tight_layout(); fig.savefig(os.path.join(ROOT, "band_spy_calibration.png")); plt.close(fig)

# ---- summary ----
print("=== Band calibration backtest ===")
print(f"observations (ticker-days with next-day close): {int(df['hit1'].notna().sum())}")
print(f"pooled next-day containment: {df['hit1'].mean()*100:.1f}%")
print(f"SPY next-day containment   : {spy['hit1'].mean()*100:.1f}%")
print("\ncontainment by horizon (pooled):")
for h, p in zip(horizons, pooled):
    print(f"  +{h}d: {p:.0f}%")
print("\nbreach direction (pooled): "
      f"down {df[df.breach=='down'].shape[0]}, up {df[df.breach=='up'].shape[0]}")
print("\nleast-contained tickers (next-day):")
print((by_tkr.head(5)["mean"] * 100).round(0).to_string())
print("\ncharts: band_hitrate_by_ticker.png, band_containment_vs_horizon.png,")
print("        band_breach_asymmetry.png, band_spy_timeline.png, band_spy_calibration.png")
