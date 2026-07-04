r"""
s7_run.py — full pre-registered S7 grid + evaluation + report generator.

Runs the DTE x delta x management x fill grid on the QUOTE-CLEAN window, computes all
pre-registered stats (equity, win rate, distribution, drawdown, Sharpe/Sortino, OOS split,
per-crisis breakout), runs the random-exit placebo for net-positive managed arms, and
writes backtester/output/s7_income_condor_20260704.md.

READ-ONLY on the warehouse. Synchronous (supervised in-foreground).
"""
from __future__ import annotations

import datetime as _dt
import copy
from pathlib import Path

import numpy as np
import pandas as pd

import s7_income_condor as s7

RNG = np.random.default_rng(20260704)
OUT_MD = Path(__file__).resolve().parent / "output" / "s7_income_condor_20260704.md"
OUT_CSV_DIR = Path(__file__).resolve().parent / "output" / "s7_research"

# Pre-registered grid.
DTES = [30, 45]
DELTAS = [0.10, 0.16]
MGMTS = [("hold", 0.0), ("managed", 0.25), ("managed", 0.50)]
FILLS = [0.0, 0.25, 0.50, 1.0]
HEADLINE = dict(dte=45, delta=0.16, mgmt="managed", frac=0.50, fill=0.50)

# OOS split + crisis windows.
TRAIN = (_dt.date(2018, 6, 1), _dt.date(2021, 12, 31))
TEST = (_dt.date(2022, 1, 1), _dt.date(2026, 7, 31))
CRISES = {
    "2018-Q4": (_dt.date(2018, 10, 1), _dt.date(2018, 12, 31)),
    "2020-COVID": (_dt.date(2020, 2, 1), _dt.date(2020, 4, 30)),
    "2022-bear": (_dt.date(2022, 1, 1), _dt.date(2022, 12, 31)),
}
ANNUAL_RF = 0.03   # ~3% cash/T-bill benchmark (declared, not tuned)


def _entry_dates_only(trades: pd.DataFrame) -> pd.Series:
    return pd.to_datetime(trades["entry_day"])


def stats_for(trades: pd.DataFrame) -> dict:
    """Portfolio stats on a set of condor trades, indexed by ENTRY day (equity as realized
    P&L accrues at entry order; each trade is 1 lot). Returns a flat dict."""
    if trades is None or len(trades) == 0:
        return {"trades": 0, "total_pnl": 0.0, "win_rate": float("nan")}
    t = trades.copy()
    t["entry_day"] = pd.to_datetime(t["entry_day"])
    t = t.sort_values("entry_day")
    pnl = t["pnl_dollars"].to_numpy(dtype=float)
    n = len(t)
    wins = int((pnl > 0).sum())
    total = float(pnl.sum())
    # Equity curve = cumulative realized P&L in entry order.
    eq = np.cumsum(pnl)
    peak = np.maximum.accumulate(eq)
    dd = eq - peak
    max_dd = float(dd.min()) if len(dd) else 0.0
    # Per-trade return series for Sharpe/Sortino (defined risk per lot = 25pt*100 = $2500).
    risk_per_lot = s7.WING_WIDTH * s7.CONTRACT_MULTIPLIER
    r = pnl / risk_per_lot
    mean_r, sd_r = float(r.mean()), float(r.std(ddof=1)) if n > 1 else 0.0
    downside = r[r < 0]
    sortino_dd = float(downside.std(ddof=1)) if len(downside) > 1 else float("nan")
    # Weekly ladder => ~52 trades/yr; annualize per-trade Sharpe by sqrt(52).
    ann = np.sqrt(52.0)
    sharpe = (mean_r / sd_r * ann) if sd_r > 0 else float("nan")
    sortino = (mean_r / sortino_dd * ann) if sortino_dd and sortino_dd > 0 else float("nan")
    # Annualized $ return vs cash on the risk capital deployed.
    span_days = (t["entry_day"].max() - t["entry_day"].min()).days or 1
    yrs = span_days / 365.25
    ann_pnl = total / yrs if yrs > 0 else float("nan")
    return {
        "trades": n,
        "win_rate": round(wins / n, 4),
        "total_pnl": round(total, 0),
        "ann_pnl": round(ann_pnl, 0),
        "mean_pnl": round(float(pnl.mean()), 1),
        "median_pnl": round(float(np.median(pnl)), 1),
        "worst_trade": round(float(pnl.min()), 0),
        "best_trade": round(float(pnl.max()), 0),
        "pnl_sd": round(float(pnl.std(ddof=1)) if n > 1 else 0.0, 1),
        "max_drawdown": round(max_dd, 0),
        "sharpe": round(sharpe, 3) if np.isfinite(sharpe) else float("nan"),
        "sortino": round(sortino, 3) if np.isfinite(sortino) else float("nan"),
        "net_positive": bool(total > 0),
    }


def slice_window(trades: pd.DataFrame, lo: _dt.date, hi: _dt.date) -> pd.DataFrame:
    ed = pd.to_datetime(trades["entry_day"]).dt.date
    return trades[(ed >= lo) & (ed <= hi)]


def random_exit_placebo(trades: pd.DataFrame, all_days: list[_dt.date], loader,
                        n_seeds: int = 200) -> dict:
    """Match each managed trade's HOLDING PERIOD but exit on a random day in [entry, exit
    window] and mark at f=0.5. Returns mean total P&L across seeds. Beats-management-if the
    real managed total exceeds this comfortably.

    Holding period = business days from entry to actual exit. For each seed we re-draw an
    exit offset uniformly in [1, hold] and mark the condor's close-debit that day (or the
    nearest quoted day), settling at intrinsic if the drawn day is the expiry.
    """
    managed = trades[trades["exit_reason"].isin(["target", "time_stop", "expiry", "settle"])]
    day_index = {d: i for i, d in enumerate(all_days)}
    totals = []
    # Precompute each trade's Condor + its quoted forward days once.
    recs = []
    for _, row in managed.iterrows():
        c = s7.Condor(
            entry_day=pd.to_datetime(row["entry_day"]).date(),
            expiration=pd.to_datetime(row["expiration"]).date(),
            entry_dte=int(row["entry_dte"]),
            short_put=row["short_put"], long_put=row["long_put"],
            short_call=row["short_call"], long_call=row["long_call"],
            entry_short_put_delta=row["entry_short_put_delta"],
            entry_short_call_delta=row["entry_short_call_delta"],
            entry_credit=row["entry_credit"], used_clean_delta=bool(row["used_clean_delta"]),
        )
        actual_exit = pd.to_datetime(row["exit_day"]).date()
        fwd = [d for d in all_days if c.entry_day < d <= c.expiration]
        # holding length in available trading days up to actual exit
        hold_days = [d for d in fwd if d <= actual_exit]
        recs.append((c, fwd, len(hold_days)))
    for _ in range(n_seeds):
        seed_total = 0.0
        for c, fwd, hold in recs:
            if hold <= 0 or not fwd:
                continue
            k = int(RNG.integers(1, hold + 1))       # random offset within the holding span
            k = min(k, len(fwd))
            # find nearest quoted day at/before the drawn day
            debit = None
            for d in fwd[k - 1::-1]:
                ddf = loader(d)
                if ddf is None:
                    continue
                snap = ddf[ddf["expiration"] == c.expiration]
                if snap.empty:
                    continue
                if d >= c.expiration:
                    settle_price = float(snap["underlying_price"].iloc[0])
                    debit = s7._condor_intrinsic(settle_price, c)
                    break
                dd = s7._condor_close_debit(snap, c, 0.5)
                if dd is not None:
                    debit = dd
                    break
            if debit is None:
                # settle at expiry intrinsic
                sdf = loader(c.expiration)
                if sdf is not None:
                    s = sdf[sdf["expiration"] == c.expiration]
                    if not s.empty:
                        debit = s7._condor_intrinsic(float(s["underlying_price"].iloc[0]), c)
            if debit is None:
                continue
            seed_total += (c.entry_credit - debit) * s7.CONTRACT_MULTIPLIER
        totals.append(seed_total)
    totals = np.array(totals)
    return {"placebo_mean_total": round(float(totals.mean()), 0),
            "placebo_sd": round(float(totals.std()), 0),
            "n_seeds": n_seeds}


def cfg_label(dte, delta, mgmt, frac, fill):
    m = mgmt if mgmt == "hold" else f"managed{int(frac*100)}"
    return f"dte{dte}_d{int(delta*100)}_{m}_f{fill}"


def main():
    OUT_CSV_DIR.mkdir(parents=True, exist_ok=True)
    all_days = s7.available_days()
    # Quote-clean days only (exclude the 2020-08→2021-12 blackout + holiday half-days).
    quote_ok = {d: s7.day_quote_ok(d) for d in all_days}
    clean_days = [d for d in all_days if quote_ok[d]]
    print(f"total days={len(all_days)}  quote-clean={len(clean_days)}  "
          f"blackout/holiday-excluded={len(all_days)-len(clean_days)}", flush=True)

    day_cache: dict = {}

    def loader(d):
        if d not in day_cache:
            day_cache[d] = s7.load_day(d)
        return day_cache[d]

    # Restrict to the pre-registered window start (2018-06) onward.
    run_days = [d for d in clean_days if d >= _dt.date(2018, 6, 1)]

    grid_rows = []
    all_trades = {}
    total_cfgs = len(DTES) * len(DELTAS) * len(MGMTS) * len(FILLS)
    ci = 0
    for dte in DTES:
        for delta in DELTAS:
            for (mgmt, frac) in MGMTS:
                for fill in FILLS:
                    ci += 1
                    label = cfg_label(dte, delta, mgmt, frac, fill)
                    print(f"[{ci}/{total_cfgs}] {label} ...", flush=True)
                    tr = s7.run_config(dte, delta, mgmt, frac, fill,
                                       days=run_days, day_cache=day_cache, verbose=False)
                    all_trades[label] = tr
                    st = stats_for(tr)
                    row = dict(config=label, dte=dte, delta=delta, mgmt=mgmt,
                               frac=frac if mgmt == "managed" else np.nan, fill=fill, **st)
                    grid_rows.append(row)
    grid = pd.DataFrame(grid_rows)
    grid.to_csv(OUT_CSV_DIR / "s7_grid.csv", index=False)

    # Save headline trades detail.
    hl_label = cfg_label(HEADLINE["dte"], HEADLINE["delta"], HEADLINE["mgmt"],
                         HEADLINE["frac"], HEADLINE["fill"])
    hl_trades = all_trades[hl_label]
    hl_trades.to_csv(OUT_CSV_DIR / "s7_headline_trades.csv", index=False)

    # OOS + crisis + placebo at HEADLINE config and its hold-to-expiry sibling.
    hold_label = cfg_label(HEADLINE["dte"], HEADLINE["delta"], "hold", 0.0, HEADLINE["fill"])
    hl_full = stats_for(hl_trades)
    hl_train = stats_for(slice_window(hl_trades, *TRAIN))
    hl_test = stats_for(slice_window(hl_trades, *TEST))
    hold_full = stats_for(all_trades[hold_label])
    crisis_stats = {name: stats_for(slice_window(hl_trades, lo, hi))
                    for name, (lo, hi) in CRISES.items()}

    # Placebo for the headline managed arm IF net-positive at f=0.50.
    placebo = None
    if hl_full["net_positive"]:
        print("running random-exit placebo for headline managed arm ...", flush=True)
        placebo = random_exit_placebo(hl_trades, run_days, loader, n_seeds=150)

    write_report(grid, all_trades, hl_full, hl_train, hl_test, hold_full,
                 crisis_stats, placebo, hl_label, hold_label,
                 len(all_days), len(clean_days), run_days)
    print(f"\nReport written: {OUT_MD}", flush=True)
    return grid, all_trades


def _fmt(st: dict, keys) -> str:
    return " | ".join(str(st.get(k, "")) for k in keys)


def write_report(grid, all_trades, hl_full, hl_train, hl_test, hold_full,
                 crisis_stats, placebo, hl_label, hold_label,
                 n_all_days, n_clean_days, run_days):
    lines = []
    A = lines.append
    A("# S7 — SPX 45-DTE Managed Premium-Income Condor — Honest Backtest")
    A("")
    A(f"**Run date:** 2026-07-04  |  **Engine:** `backtester/s7_income_condor.py`  |  "
      f"**Pre-reg:** `docs/PREREG_S7_income_condor_2026-07-04.md`")
    A(f"**Window run complete through:** {run_days[-1]} (final quote-clean day in the window).")
    A("")
    A("PAPER / research only. READ-ONLY warehouse. Frozen S0–S6 / regime config untouched.")
    A("")
    A("---")
    A("## 0. DATA-CLEANING NOTE (material — read first)")
    A("")
    A("Two data problems in the warehouse EOD SPX chains, verified empirically at build time:")
    A("")
    A("1. **Vendor greeks (delta / implied_vol) corrupt** for 2020 (partial) and **all of "
      "2021** (degenerate: ~49% of rows read delta exactly 0 or ±1, IV exactly 0). Handled "
      "by a per-day degeneracy flag (`|delta|∈{0,1}` share > 35%) + a clean BSM re-inversion "
      "of delta from mid/spot/T (audited `s6_recon`). **Reported honestly: this path is "
      "unit-tested but never fires on a *tradeable* day** (see #2).")
    A("2. **BID/ASK NBBO QUOTES ARE ENTIRELY MISSING (all-zero)** for the contiguous window "
      "**2020-08-13 → 2021-12-31** (~333 trading days). Only last-trade `close` and "
      "`underlying_price` survive. This is a genuine quote **BLACKOUT**, not merely bad "
      "greeks — and it is **perfectly coincident** with the delta corruption.")
    A("")
    A("**Honest-testing consequence (anti-curve-fit rule #1):** we do **not** fabricate a "
      "spread where none was quoted. The engine drops unquoted rows, so blackout days load "
      "as unquotable and are skipped for entry, marking, and management. S7 is therefore run "
      "on the **quote-clean window only**:")
    A("")
    A(f"- total trading days with data: **{n_all_days}**")
    A(f"- quote-clean (usable) days: **{n_clean_days}**")
    A(f"- excluded (blackout + holiday half-days): **{n_all_days - n_clean_days}**")
    A("")
    A("**This is a real limitation, stated plainly:** the ledger below **omits mid-2020 "
      "through end-2021** — i.e. the back half of the COVID recovery and the entire 2021 "
      "low-vol melt-up. The pre-registered 2020-COVID crisis breakout therefore covers only "
      "**Feb–Aug 12, 2020** (the crash and first bounce, which ARE quoted); the deep-2020 "
      "grind and 2021 calm are absent. A short-vol income strategy would likely have done "
      "*well* in the missing 2021 calm — so this exclusion, if anything, makes the surviving "
      "ledger a **conservative** read, not a flattering one. Days needing the clean-delta "
      "re-inversion on a tradeable day: **0** (corruption ⊆ blackout).")
    A("")
    A("---")
    A("## 1. HEADLINE — 45 DTE / 16-delta / 50%-target-or-21DTE / f=0.50 (half-spread)")
    A("")
    keys = ["trades", "win_rate", "total_pnl", "ann_pnl", "mean_pnl", "median_pnl",
            "worst_trade", "max_drawdown", "sharpe", "sortino", "net_positive"]
    A("| window | " + " | ".join(keys) + " |")
    A("|" + "---|" * (len(keys) + 1))
    A(f"| **full (quote-clean)** | {_fmt(hl_full, keys)} |")
    A(f"| train 2018-06→2021 | {_fmt(hl_train, keys)} |")
    A(f"| test 2022→2026 | {_fmt(hl_test, keys)} |")
    A("")
    A("## 2. Managed vs hold-to-expiry (same 45/16, f=0.50) — TOTAL P&L test")
    A("")
    A("| arm | " + " | ".join(keys) + " |")
    A("|" + "---|" * (len(keys) + 1))
    A(f"| managed 50% (headline) | {_fmt(hl_full, keys)} |")
    A(f"| hold-to-expiry (control) | {_fmt(hold_full, keys)} |")
    A("")
    if placebo is not None:
        A("## 3. Random-exit placebo (headline managed arm, f=0.50)")
        A("")
        A(f"- managed-arm TOTAL P&L: **${hl_full['total_pnl']:,.0f}**")
        A(f"- placebo mean TOTAL P&L (same holding-period distribution, {placebo['n_seeds']} "
          f"seeds): **${placebo['placebo_mean_total']:,.0f}** (sd ${placebo['placebo_sd']:,.0f})")
        edge = hl_full["total_pnl"] - placebo["placebo_mean_total"]
        A(f"- managed − placebo = **${edge:,.0f}** — "
          + ("management timing ADDS value" if edge > 0 else
             "management timing does NOT beat a random exit of the same holding period"))
        A("")
    A("## 4. Per-crisis breakout (headline config)")
    A("")
    A("| crisis | " + " | ".join(keys) + " |")
    A("|" + "---|" * (len(keys) + 1))
    for name, st in crisis_stats.items():
        A(f"| {name} | {_fmt(st, keys)} |")
    A("")
    A("## 5. FULL GRID (plateau check — all configs)")
    A("")
    gk = ["config", "trades", "win_rate", "total_pnl", "ann_pnl", "max_drawdown",
          "sharpe", "net_positive"]
    A("| " + " | ".join(gk) + " |")
    A("|" + "---|" * len(gk))
    for _, r in grid.sort_values(["dte", "delta", "mgmt", "frac", "fill"]).iterrows():
        A("| " + " | ".join(str(r[k]) for k in gk) + " |")
    A("")
    A("### Fill-band robustness (headline 45/16/managed-50%, across f)")
    A("")
    sub = grid[(grid["dte"] == 45) & (grid["delta"] == 0.16)
               & (grid["mgmt"] == "managed") & (grid["frac"] == 0.50)]
    A("| fill f | total_pnl | win_rate | net_positive |")
    A("|---|---|---|---|")
    for _, r in sub.sort_values("fill").iterrows():
        A(f"| {r['fill']} | {r['total_pnl']} | {r['win_rate']} | {r['net_positive']} |")
    A("")
    A("---")
    A("## VERDICT")
    A("")
    lines_verdict = build_verdict(grid, hl_full, hl_train, hl_test, hold_full,
                                  crisis_stats, placebo)
    for v in lines_verdict:
        A(v)
    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.write_text("\n".join(lines), encoding="utf-8")


def build_verdict(grid, hl_full, hl_train, hl_test, hold_full, crisis_stats, placebo):
    v = []
    # Fill band (headline row set).
    band = grid[(grid["dte"] == 45) & (grid["delta"] == 0.16)
                & (grid["mgmt"] == "managed") & (grid["frac"] == 0.50)
                & (grid["fill"] <= 0.50)]
    band_ok = bool((band["total_pnl"] > 0).all()) and len(band) > 0
    oos_ok = bool(hl_train.get("net_positive")) and bool(hl_test.get("net_positive"))
    # Plateau: share of managed cells net-positive at f<=0.5.
    plat = grid[(grid["fill"] <= 0.50)]
    plat_share = float((plat["total_pnl"] > 0).mean())
    mgmt_beats_hold = hl_full["total_pnl"] > hold_full["total_pnl"]
    placebo_ok = (placebo is not None
                  and hl_full["total_pnl"] > placebo["placebo_mean_total"])
    crisis_ok = hl_full["net_positive"]

    v.append(f"- **Net-positive at realistic half-spread fills (f≤0.50):** "
             f"{'PASS' if band_ok else 'FAIL'}  (headline full-window total "
             f"${hl_full['total_pnl']:,.0f}).")
    v.append(f"- **OOS survival (train AND test net-positive):** {'PASS' if oos_ok else 'FAIL'}  "
             f"(train ${hl_train.get('total_pnl',0):,.0f}, test ${hl_test.get('total_pnl',0):,.0f}).")
    v.append(f"- **Plateau (share of cells net-positive at f≤0.50):** {plat_share:.0%}.")
    v.append(f"- **Managed beats hold-to-expiry on TOTAL P&L:** {'PASS' if mgmt_beats_hold else 'FAIL'}  "
             f"(managed ${hl_full['total_pnl']:,.0f} vs hold ${hold_full['total_pnl']:,.0f}).")
    if placebo is not None:
        v.append(f"- **Managed beats random-exit placebo on TOTAL P&L:** "
                 f"{'PASS' if placebo_ok else 'FAIL'}  (managed ${hl_full['total_pnl']:,.0f} "
                 f"vs placebo ${placebo['placebo_mean_total']:,.0f}).")
    else:
        v.append("- **Random-exit placebo:** not run (headline arm not net-positive at f=0.50).")
    v.append(f"- **Crisis-survivable (full-cycle ledger net-positive incl. quoted crises):** "
             f"{'PASS' if crisis_ok else 'FAIL'}.")
    v.append("")
    all_pass = band_ok and oos_ok and plat_share >= 0.5 and mgmt_beats_hold and crisis_ok
    if all_pass and (placebo is None or placebo_ok):
        v.append("**OVERALL: S7 SURVIVES the pre-registered bar** on the quote-clean window. "
                 "Note the material 2020H2–2021 data gap: this is a *survivor pending* the "
                 "missing window, not a fully-validated all-cycle edge.")
    else:
        v.append("**OVERALL: S7 does NOT clear the full pre-registered bar** — see the failed "
                 "criteria above. Reported as-is (a refutation / partial result is a valid "
                 "outcome; no edge was manufactured).")
    return v


if __name__ == "__main__":
    main()
