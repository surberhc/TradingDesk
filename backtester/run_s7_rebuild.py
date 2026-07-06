r"""
run_s7_rebuild.py — S7 delta-wing REBUILD runner (pre-registered 2026-07-06).

Runs the full pre-registered grid + benchmark arms ONCE, caching each warehouse day file
at most once (the day load is the bottleneck), computes the full metric suite / OOS split /
per-crisis / placebo, and writes:
  - backtester/output/s7_income_condor_rebuild_2026-07-06.md   (report + VERDICT)
  - backtester/output/s7_research/*.csv                        (headline + CSP per-trade)

PAPER / research only. OFFLINE. STRICTLY READ-ONLY on the warehouse. No tuning to data.
"""

from __future__ import annotations

import datetime as _dt
import time
from pathlib import Path

import numpy as np
import pandas as pd

import s7_income_condor as s7

REPORT = Path(__file__).resolve().parent / "output" / "s7_income_condor_rebuild_2026-07-06.md"
CSV_DIR = Path(__file__).resolve().parent / "output" / "s7_research"
CSV_DIR.mkdir(parents=True, exist_ok=True)

WINDOW_START = _dt.date(2018, 6, 1)
WINDOW_END = _dt.date(2026, 7, 31)
OOS_SPLIT = _dt.date(2022, 1, 1)   # train < split <= test

# Pre-registered grid axes
DTES = [30, 45]
SHORT_DELTAS = [0.16, 0.20]
WINGS = [("delta", 0.05), ("points", 25.0), ("points", 50.0)]
MANAGEMENTS = [("managed", 0.50), ("hold", 0.0)]
IVR_FILTERS = ["always", "high"]
FILLS = [0.0, 0.25, 0.50, 1.0]

HEADLINE = dict(dte=45, delta=0.16, wing=("delta", 0.05), mgmt=("managed", 0.50),
                ivr="always", f=0.50)

PLACEBO_SEED = 20260706
PLACEBO_N = 200


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #
def _equity_curve(trades: pd.DataFrame) -> pd.Series:
    """Realized-P&L equity curve ordered by exit day (cash accrues when a trade closes)."""
    if trades.empty:
        return pd.Series(dtype=float)
    t = trades.dropna(subset=["exit_day", "pnl_dollars"]).copy()
    t["exit_day"] = pd.to_datetime(t["exit_day"])
    daily = t.groupby("exit_day")["pnl_dollars"].sum().sort_index()
    return daily.cumsum()


def _max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    peak = equity.cummax()
    dd = equity - peak
    return float(dd.min())


def metrics(trades: pd.DataFrame) -> dict:
    """Metric suite for one config's trade ledger."""
    out = dict(n=0, total_pnl=0.0, win_rate=float("nan"), mean=float("nan"),
               sd=float("nan"), skew=float("nan"), worst=float("nan"),
               max_dd=0.0, ann_ret=float("nan"), sharpe=float("nan"),
               sortino=float("nan"), med_cml=float("nan"))
    if trades is None or trades.empty:
        return out
    p = trades["pnl_dollars"].dropna()
    if p.empty:
        return out
    out["n"] = int(len(p))
    out["total_pnl"] = float(p.sum())
    out["win_rate"] = float((p > 0).mean())
    out["mean"] = float(p.mean())
    out["sd"] = float(p.std(ddof=1)) if len(p) > 1 else 0.0
    out["skew"] = float(p.skew()) if len(p) > 2 else float("nan")
    out["worst"] = float(p.min())
    eq = _equity_curve(trades)
    out["max_dd"] = _max_drawdown(eq)
    # Sharpe/Sortino on per-trade P&L (unitless, excess over 0 — these are absolute $ premium
    # trades with no capital base defined; report the trade-level risk-adjusted ratio).
    if out["sd"] and out["sd"] > 0:
        out["sharpe"] = out["mean"] / out["sd"] * np.sqrt(52.0)  # ~weekly cadence annualize
    downside = p[p < 0]
    dd_sd = float(downside.std(ddof=1)) if len(downside) > 1 else 0.0
    if dd_sd > 0:
        out["sortino"] = out["mean"] / dd_sd * np.sqrt(52.0)
    # annualized total return in $ terms per year of coverage
    if not eq.empty:
        span_days = (eq.index.max() - eq.index.min()).days
        yrs = max(span_days / 365.25, 1e-6)
        out["ann_ret"] = out["total_pnl"] / yrs
    # median credit / max-loss ratio (condor only; CSP has no wing)
    if "put_wing_width" in trades.columns:
        w = trades.dropna(subset=["put_wing_width", "call_wing_width", "entry_credit"]).copy()
        if not w.empty:
            max_loss = np.maximum(w["put_wing_width"], w["call_wing_width"]) - w["entry_credit"]
            ratio = w["entry_credit"] / np.maximum(max_loss, 1e-9)
            out["med_cml"] = float(ratio.median())
    return out


def _subset_by_entry(trades: pd.DataFrame, lo: _dt.date, hi: _dt.date) -> pd.DataFrame:
    if trades.empty:
        return trades
    ed = pd.to_datetime(trades["entry_day"]).dt.date
    return trades[(ed >= lo) & (ed <= hi)]


# --------------------------------------------------------------------------- #
# Placebo — random-exit matched holding-period, TOTAL P&L
# --------------------------------------------------------------------------- #
def placebo_total_pnl(trades: pd.DataFrame, day_cache: dict, price_maps: dict, all_days: list,
                      f: float, rng: np.random.Generator, n_seeds: int = PLACEBO_N) -> float:
    """Mean TOTAL P&L of a random-exit book matched to each managed condor's holding period.

    For each managed condor, keep its ENTRY (same strikes/credit), but replace the rule-based
    exit with an exit on a RANDOM future trading day at the same holding-period LAG (in
    trading days) — averaged over n_seeds random offsets bounded by the real holding period.
    Uses only the cached day marks (no new loads). Causal by construction (exit day is drawn
    from the condor's own [entry, expiry] forward window)."""
    if trades.empty:
        return float("nan")

    def loader(d):
        return day_cache.get(d)

    def pm(d):
        return price_maps.get(d)

    day_index = {d: i for i, d in enumerate(all_days)}
    totals = np.zeros(n_seeds)
    # Precompute per-condor the forward trading-day list once.
    recs = trades.to_dict("records")
    for rec in recs:
        entry_day = rec["entry_day"]
        exp = rec["expiration"]
        if entry_day not in day_index:
            continue
        future = [d for d in all_days if entry_day < d < exp]
        if not future:
            continue
        # real holding period in trading-day steps (bounded)
        exit_day = rec.get("exit_day")
        try:
            hp = future.index(exit_day) + 1 if exit_day in future else len(future)
        except ValueError:
            hp = len(future)
        hp = max(1, min(hp, len(future)))
        # reconstruct a Condor to reprice the close
        c = s7.Condor(
            entry_day=entry_day, expiration=exp, entry_dte=rec["entry_dte"],
            short_put=rec["short_put"], long_put=rec["long_put"],
            short_call=rec["short_call"], long_call=rec["long_call"],
            entry_short_put_delta=rec["entry_short_put_delta"],
            entry_short_call_delta=rec["entry_short_call_delta"],
            entry_credit=rec["entry_credit"], used_clean_delta=rec["used_clean_delta"],
            put_wing_width=rec.get("put_wing_width", float("nan")),
            call_wing_width=rec.get("call_wing_width", float("nan")),
        )
        # draw n_seeds random exit lags in [1, hp]; reprice each
        lags = rng.integers(1, hp + 1, size=n_seeds)
        for si, lag in enumerate(lags):
            rexit = future[lag - 1]
            pmp = pm(rexit)
            debit = s7._close_debit_pm(pmp, c, f) if pmp is not None else None
            if debit is None:
                # fall back to intrinsic settle at expiry underlying
                spm = pm(exp)
                sp = spm.get("_spot", {}).get(exp) if spm is not None else None
                debit = s7._condor_intrinsic(sp, c) if sp is not None else 0.0
            totals[si] += (c.entry_credit - debit) * s7.CONTRACT_MULTIPLIER
    return float(totals.mean())


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    t0 = time.time()
    print("[S7 rebuild] loading day universe...", flush=True)
    all_days = [d for d in s7.available_days() if WINDOW_START <= d <= WINDOW_END]
    print(f"[S7 rebuild] {len(all_days)} trading days in window "
          f"{all_days[0]}..{all_days[-1]}", flush=True)

    # Coverage / blackout accounting on the weekly ladder.
    entries = s7.weekly_entry_days(all_days)
    quoted = [d for d in entries if s7.day_quote_ok(d)]
    blackout_weeks = len(entries) - len(quoted)
    print(f"[S7 rebuild] weekly entries={len(entries)}  quoted={len(quoted)}  "
          f"blackout-skipped={blackout_weeks}", flush=True)

    day_cache: dict = {}
    price_maps: dict = {}   # shared per-day (strike,right)->(bid,ask) cache across all configs
    ivr_series = s7.load_ivr_series()
    ivr_ok = ivr_series is not None
    print(f"[S7 rebuild] IVR series loaded: {ivr_ok}", flush=True)

    # ------------------------------------------------------------------ #
    # Run the grid. Primary hypothesis (5-delta wings) FIRST, then controls.
    # ------------------------------------------------------------------ #
    wing_order = [("delta", 0.05), ("points", 25.0), ("points", 50.0)]
    grid_rows = []
    trade_store: dict[str, pd.DataFrame] = {}
    n_cfg = 0
    for wing in wing_order:
        for dte in DTES:
            for sd in SHORT_DELTAS:
                for (mgmt, frac) in MANAGEMENTS:
                    for ivrf in IVR_FILTERS:
                        if ivrf == "high" and not ivr_ok:
                            continue
                        for f in FILLS:
                            n_cfg += 1
                            tr = s7.run_config(
                                dte, sd, mgmt, frac, f,
                                wing_spec=wing, ivr_filter=ivrf,
                                ivr_series=ivr_series,
                                days=all_days, day_cache=day_cache,
                                price_maps=price_maps)
                            tag = s7.config_tag(dte, sd, wing, mgmt, frac, ivrf, f)
                            trade_store[tag] = tr
                            m = metrics(tr)
                            row = dict(tag=tag, wing=s7._wing_tag(wing), dte=dte,
                                       short_delta=sd, mgmt=mgmt + (str(int(frac*100))
                                       if mgmt == "managed" else ""), ivr=ivrf, f=f, **m)
                            grid_rows.append(row)
                            if f == 0.50:
                                print(f"  [cfg {n_cfg}] {tag}  n={m['n']} "
                                      f"pnl=${m['total_pnl']:,.0f} win={m['win_rate']:.2f}",
                                      flush=True)
    print(f"[S7 rebuild] grid done: {n_cfg} configs, {len(day_cache)} days cached, "
          f"{time.time()-t0:.0f}s", flush=True)

    grid = pd.DataFrame(grid_rows)

    # ------------------------------------------------------------------ #
    # Benchmark arms
    # ------------------------------------------------------------------ #
    bench_rows = []
    bench_store = {}
    # CBOE CNDR replica: 20-delta shorts / 5-delta wings / 30 DTE / hold / always
    for f in FILLS:
        tr = s7.run_config(30, 0.20, "hold", 0.0, f, wing_spec=("delta", 0.05),
                           ivr_filter="always", ivr_series=ivr_series,
                           days=all_days, day_cache=day_cache, price_maps=price_maps)
        m = metrics(tr)
        bench_store[f"cndr_f{f}"] = tr
        bench_rows.append(dict(arm="CBOE-CNDR (20d/5d wing/30DTE/hold)", f=f, **m))
    # ATM CSP for DTE {30, 45}
    for dte in DTES:
        for f in FILLS:
            tr = s7.run_csp_config(dte, f, days=all_days, day_cache=day_cache)
            m = metrics(tr)
            bench_store[f"csp{dte}_f{f}"] = tr
            bench_rows.append(dict(arm=f"ATM CSP {dte}DTE hold", f=f, **m))
    bench = pd.DataFrame(bench_rows)
    print(f"[S7 rebuild] benchmarks done, {time.time()-t0:.0f}s", flush=True)

    # ------------------------------------------------------------------ #
    # OOS + per-crisis for the delta-wing sub-grid (headline-family) at f=0.50
    # ------------------------------------------------------------------ #
    def oos_crisis(tr: pd.DataFrame) -> dict:
        train = _subset_by_entry(tr, WINDOW_START, _dt.date(2021, 12, 31))
        test = _subset_by_entry(tr, OOS_SPLIT, WINDOW_END)
        q4_18 = _subset_by_entry(tr, _dt.date(2018, 10, 1), _dt.date(2018, 12, 31))
        covid = _subset_by_entry(tr, _dt.date(2020, 2, 1), _dt.date(2020, 4, 30))
        bear22 = _subset_by_entry(tr, _dt.date(2022, 1, 1), _dt.date(2022, 12, 31))
        return dict(
            train_pnl=metrics(train)["total_pnl"], train_n=metrics(train)["n"],
            test_pnl=metrics(test)["total_pnl"], test_n=metrics(test)["n"],
            q4_18_pnl=metrics(q4_18)["total_pnl"], q4_18_n=metrics(q4_18)["n"],
            covid_pnl=metrics(covid)["total_pnl"], covid_n=metrics(covid)["n"],
            bear22_pnl=metrics(bear22)["total_pnl"], bear22_n=metrics(bear22)["n"])

    # OOS/crisis on the delta-wing arms at f=0.50 (both managements, both ivr, dte45 focus)
    oos_rows = []
    for tag, tr in trade_store.items():
        if "_f0.5" in tag and "w5d" in tag:
            oc = oos_crisis(tr)
            oos_rows.append(dict(tag=tag, **oc))
    oos = pd.DataFrame(oos_rows)

    # ------------------------------------------------------------------ #
    # Placebo — for the 5-delta MANAGED cells at f=0.50 (both DTE/delta/ivr)
    # ------------------------------------------------------------------ #
    placebo_rows = []
    rng = np.random.default_rng(PLACEBO_SEED)
    for tag, tr in trade_store.items():
        if "w5d" in tag and "managed50" in tag and "_f0.5" in tag:
            managed_total = metrics(tr)["total_pnl"]
            plc = placebo_total_pnl(tr, day_cache, price_maps, all_days, 0.50, rng, PLACEBO_N)
            placebo_rows.append(dict(tag=tag, managed_total=managed_total,
                                     placebo_mean=plc,
                                     managed_beats=bool(managed_total > plc)))
    placebo = pd.DataFrame(placebo_rows)
    print(f"[S7 rebuild] placebo done, {time.time()-t0:.0f}s", flush=True)

    # ------------------------------------------------------------------ #
    # Per-trade CSVs: headline config + CSP arm
    # ------------------------------------------------------------------ #
    hl_tag = s7.config_tag(HEADLINE["dte"], HEADLINE["delta"], HEADLINE["wing"],
                           HEADLINE["mgmt"][0], HEADLINE["mgmt"][1], HEADLINE["ivr"],
                           HEADLINE["f"])
    if hl_tag in trade_store and not trade_store[hl_tag].empty:
        trade_store[hl_tag].to_csv(CSV_DIR / "headline_delta_wing_trades.csv", index=False)
    if "csp45_f0.5" in bench_store and not bench_store["csp45_f0.5"].empty:
        bench_store["csp45_f0.5"].to_csv(CSV_DIR / "csp_45dte_trades.csv", index=False)

    # ------------------------------------------------------------------ #
    # Report
    # ------------------------------------------------------------------ #
    write_report(grid, bench, oos, placebo, all_days, entries, quoted, blackout_weeks,
                 hl_tag, ivr_ok, time.time() - t0)
    print(f"[S7 rebuild] DONE {time.time()-t0:.0f}s -> {REPORT}", flush=True)


def _fmt(x, nd=2):
    if x is None or (isinstance(x, float) and not np.isfinite(x)):
        return "n/a"
    return f"{x:,.{nd}f}"


def _grid_table(df: pd.DataFrame) -> str:
    cols = ["wing", "dte", "short_delta", "mgmt", "ivr", "f", "n", "total_pnl",
            "win_rate", "worst", "max_dd", "sharpe", "sortino", "med_cml"]
    d = df[cols].copy().sort_values(["wing", "dte", "short_delta", "mgmt", "ivr", "f"])
    lines = ["| wing | dte | sΔ | mgmt | ivr | f | n | total $ | win | worst $ | maxDD $ | Sharpe | Sortino | cr/maxloss |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for _, r in d.iterrows():
        lines.append(f"| {r['wing']} | {r['dte']} | {r['short_delta']:.2f} | {r['mgmt']} "
                     f"| {r['ivr']} | {r['f']} | {r['n']} | {_fmt(r['total_pnl'],0)} "
                     f"| {_fmt(r['win_rate'],2)} | {_fmt(r['worst'],0)} "
                     f"| {_fmt(r['max_dd'],0)} | {_fmt(r['sharpe'])} | {_fmt(r['sortino'])} "
                     f"| {_fmt(r['med_cml'],3)} |")
    return "\n".join(lines)


def _bench_table(df: pd.DataFrame) -> str:
    lines = ["| arm | f | n | total $ | win | worst $ | maxDD $ | Sharpe | Sortino |",
             "|---|---|---|---|---|---|---|---|---|"]
    for _, r in df.sort_values(["arm", "f"]).iterrows():
        lines.append(f"| {r['arm']} | {r['f']} | {r['n']} | {_fmt(r['total_pnl'],0)} "
                     f"| {_fmt(r['win_rate'],2)} | {_fmt(r['worst'],0)} | {_fmt(r['max_dd'],0)} "
                     f"| {_fmt(r['sharpe'])} | {_fmt(r['sortino'])} |")
    return "\n".join(lines)


def _oos_table(df: pd.DataFrame) -> str:
    if df.empty:
        return "_(no delta-wing cells)_"
    lines = ["| tag | train $ (n) | test $ (n) | 2018Q4 $ | COVID $ | 2022 $ |",
             "|---|---|---|---|---|---|"]
    for _, r in df.iterrows():
        lines.append(f"| {r['tag']} | {_fmt(r['train_pnl'],0)} ({r['train_n']}) "
                     f"| {_fmt(r['test_pnl'],0)} ({r['test_n']}) | {_fmt(r['q4_18_pnl'],0)} "
                     f"| {_fmt(r['covid_pnl'],0)} | {_fmt(r['bear22_pnl'],0)} |")
    return "\n".join(lines)


def _placebo_table(df: pd.DataFrame) -> str:
    if df.empty:
        return "_(no managed 5-delta cells)_"
    lines = ["| tag | managed total $ | placebo mean $ | managed beats placebo? |",
             "|---|---|---|---|"]
    for _, r in df.iterrows():
        lines.append(f"| {r['tag']} | {_fmt(r['managed_total'],0)} "
                     f"| {_fmt(r['placebo_mean'],0)} | {'YES' if r['managed_beats'] else 'NO'} |")
    return "\n".join(lines)


def write_report(grid, bench, oos, placebo, all_days, entries, quoted, blackout_weeks,
                 hl_tag, ivr_ok, runtime_s):
    # ---- verdict computation ----
    hl = grid[grid["tag"] == hl_tag]
    hl_row = hl.iloc[0] if not hl.empty else None
    # headline across fill band
    hl_family = grid[(grid["wing"] == "w5d") & (grid["dte"] == 45) &
                     (grid["short_delta"] == 0.16) & (grid["mgmt"] == "managed50") &
                     (grid["ivr"] == "always")].sort_values("f")
    band_pnls = {float(r["f"]): float(r["total_pnl"]) for _, r in hl_family.iterrows()}

    # Criterion 1: net-positive at headline AND across mid->0.50 band
    c1_vals = [band_pnls.get(x, float("-inf")) for x in (0.0, 0.25, 0.50)]
    c1 = all(v > 0 for v in c1_vals)

    # Criterion 2: OOS both halves positive (headline tag)
    oos_hl = oos[oos["tag"] == hl_tag]
    if not oos_hl.empty:
        c2 = bool(oos_hl.iloc[0]["train_pnl"] > 0 and oos_hl.iloc[0]["test_pnl"] > 0)
        c2_detail = (oos_hl.iloc[0]["train_pnl"], oos_hl.iloc[0]["test_pnl"])
    else:
        c2, c2_detail = False, (float("nan"), float("nan"))

    # Criterion 3: plateau — share of 5-delta cells at f=0.50 that are net-positive
    dw50 = grid[(grid["wing"] == "w5d") & (grid["f"] == 0.50)]
    plateau_share = float((dw50["total_pnl"] > 0).mean()) if not dw50.empty else 0.0
    c3 = plateau_share >= 0.5

    # Criterion 4: management beats hold + beats placebo (5d, dte45, d16, always, f=0.5)
    def _pnl(wing, dte, sd, mgmt, ivrf, f):
        r = grid[(grid["wing"] == wing) & (grid["dte"] == dte) &
                 (grid["short_delta"] == sd) & (grid["mgmt"] == mgmt) &
                 (grid["ivr"] == ivrf) & (grid["f"] == f)]
        return float(r.iloc[0]["total_pnl"]) if not r.empty else float("nan")
    mgd = _pnl("w5d", 45, 0.16, "managed50", "always", 0.50)
    held = _pnl("w5d", 45, 0.16, "hold", "always", 0.50)
    plc_hl = placebo[placebo["tag"] == hl_tag]
    plc_mean = float(plc_hl.iloc[0]["placebo_mean"]) if not plc_hl.empty else float("nan")
    c4 = bool(np.isfinite(mgd) and np.isfinite(held) and np.isfinite(plc_mean)
              and mgd > held and mgd > plc_mean)

    # Criterion 5: beats reference bar (ATM CSP) on Sharpe at f=0.50
    csp45 = bench[(bench["arm"].str.contains("CSP 45")) & (bench["f"] == 0.50)]
    csp_sharpe = float(csp45.iloc[0]["sharpe"]) if not csp45.empty else float("nan")
    hl_sharpe = float(hl_row["sharpe"]) if hl_row is not None else float("nan")
    c5 = bool(np.isfinite(hl_sharpe) and np.isfinite(csp_sharpe) and hl_sharpe >= csp_sharpe)

    # Criterion 6: crisis survivability — full-cycle ledger net-positive (headline tag total)
    c6 = bool(hl_row is not None and hl_row["total_pnl"] > 0)

    core_pass = c1 and c2 and c3 and c6
    verdict = "PASS (genuine income edge)" if (core_pass and c4 and c5) else None
    if verdict is None:
        if not core_pass:
            verdict = "REFUTED"
        elif core_pass and not c4:
            verdict = "PARTIAL — hold condor may have an edge, managed overlay REFUTED"
        elif core_pass and c4 and not c5:
            verdict = "PARTIAL — condor works but ATM cash-secured put is the better vehicle"
        else:
            verdict = "REFUTED"

    def yn(b):
        return "PASS" if b else "FAIL"

    L = []
    L.append("# S7 REBUILD — delta-based wings — RESULTS + VERDICT\n")
    L.append(f"**Run:** 2026-07-06  |  **Runtime:** {runtime_s:.0f}s  |  "
             f"pre-registered in `docs/PREREG_S7_rebuild_delta_wings_2026-07-06.md` "
             f"(committed BEFORE this run).\n")
    L.append("## VERDICT (lead)\n")
    L.append(f"### **{verdict}**\n")
    L.append(f"Headline config `{hl_tag}` "
             f"(45 DTE / 0.16 short delta / 5-delta wings / 50%-target-or-21-DTE / "
             f"enter-always / f=0.50).\n")
    if hl_row is not None:
        L.append(f"- Headline total P&L @ f=0.50: **${hl_row['total_pnl']:,.0f}** "
                 f"over n={hl_row['n']} condors, win rate {hl_row['win_rate']:.1%}, "
                 f"worst trade ${hl_row['worst']:,.0f}, maxDD ${hl_row['max_dd']:,.0f}, "
                 f"Sharpe {_fmt(hl_row['sharpe'])}, median credit/max-loss "
                 f"{_fmt(hl_row['med_cml'],3)}.\n")
    L.append("**Fill band (headline family, dte45/0.16/5d-wing/managed50/always):** "
             + "  ".join(f"f={k}: ${v:,.0f}" for k, v in sorted(band_pnls.items())) + "\n")
    L.append("### Six pass criteria\n")
    L.append(f"1. **Net-positive at realistic fills (mid->0.50 band):** {yn(c1)} — "
             f"f0=${band_pnls.get(0.0,float('nan')):,.0f}, "
             f"f0.25=${band_pnls.get(0.25,float('nan')):,.0f}, "
             f"f0.50=${band_pnls.get(0.50,float('nan')):,.0f}.")
    L.append(f"2. **OOS both halves positive:** {yn(c2)} — "
             f"train ${c2_detail[0]:,.0f}, test ${c2_detail[1]:,.0f}.")
    L.append(f"3. **Plateau (>=50% of 5-delta cells net-positive @ f=0.50):** {yn(c3)} — "
             f"{plateau_share:.0%} of 5-delta cells positive.")
    L.append(f"4. **Management beats hold AND placebo:** {yn(c4)} — "
             f"managed ${mgd:,.0f} vs hold ${held:,.0f} vs placebo ${plc_mean:,.0f}.")
    L.append(f"5. **Beats ATM CSP on Sharpe:** {yn(c5)} — "
             f"condor Sharpe {_fmt(hl_sharpe)} vs CSP-45 Sharpe {_fmt(csp_sharpe)}.")
    L.append(f"6. **Crisis survivability (full-cycle net-positive):** {yn(c6)} — "
             f"full ledger ${hl_row['total_pnl'] if hl_row is not None else float('nan'):,.0f}.\n")

    L.append("## Data window & coverage\n")
    L.append(f"- Trading days in window: {len(all_days)} "
             f"({all_days[0]} .. {all_days[-1]}).")
    L.append(f"- Weekly ladder entries: {len(entries)}; genuinely quoted: {len(quoted)}; "
             f"**blackout-skipped weeks: {blackout_weeks}** "
             f"(the 2020-08-13 -> 2021-12-31 all-zero-NBBO quote blackout).")
    L.append(f"- IV-rank filter: {'ENABLED (VIX daily close found)' if ivr_ok else 'SKIPPED — VIX series not located; IVR arm omitted (never fabricated)'}.\n")

    L.append("## Full grid x fill band\n")
    L.append(_grid_table(grid) + "\n")
    L.append("## Benchmark arms (CBOE-CNDR replica + ATM CSP)\n")
    L.append(_bench_table(bench) + "\n")
    L.append("## OOS split + per-crisis (5-delta wing cells, f=0.50)\n")
    L.append(_oos_table(oos) + "\n")
    L.append("## Placebo — random-exit matched holding (5-delta managed, f=0.50)\n")
    L.append(f"Seed={PLACEBO_SEED}, {PLACEBO_N} draws/condor. Management is only real if it "
             f"beats a random exit of the same average holding period on TOTAL P&L.\n")
    L.append(_placebo_table(placebo) + "\n")
    L.append("## Notes on method\n")
    L.append("- Honest net-combo fills on all 4 legs, propagated through the profit-target "
             "trigger. No mid-only claims. Corruption guard: strikes never selected off "
             "degenerate vendor delta (BSM re-inversion). No look-ahead (pytest-guarded). "
             "No parameter tuned to the data; grid pre-registered.\n")

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text("\n".join(L), encoding="utf-8")


if __name__ == "__main__":
    main()
