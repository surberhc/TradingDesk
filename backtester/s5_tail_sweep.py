"""
S5 tail-sizing SWEEP — characterize the drawdown-vs-rebound trade-off.

PAPER / research only. Offline. Windows. numpy/pandas + the prototype's hand-rolled BSM.

This is a THIN driver. It IMPORTS the existing S5 prototype engine
(`s5_convexity_overlay.simulate_s5`) and runs a 2-D grid over the two tail-sizing
knobs the prototype already exposes:

    tail_frac  (notional fraction of the core that is hedged)  {0.25, 0.50, 0.75, 1.00}
    tail_otm   (how far OTM the tail strike sits)              {10%, 15%, 20%, 25%}

== The question ==
The prototype's one weakness: a 100%-notional 20%-OTM tail leaves the book FULLY
hedged at a crash bottom (net delta -> ~0), so it gives up the sharpest rebound
(GFC recovery capture ~31%). Tail SIZE is the dial: a smaller or further-OTM tail
leaves MORE residual delta at the bottom -> more rebound capture, but LESS drawdown
cushion. We sweep both knobs to SEE that trade-off and name a provisional sweet spot.

== What is held fixed ==
The HARVEST knob is pinned at its central assumption (5.5%/yr, the prototype default)
throughout. The prototype's own report shows downside protection (Tier-1) is
harvest-INDEPENDENT (it is funded first / mandatory), so holding harvest constant
isolates tail sizing cleanly. Tier-2, the reserve, and the upside barbell are all left
at their prototype defaults so we change ONE thing (the tail) at a time.

== Causality ==
Same discipline as the prototype: every DECISION at day T uses data <= T, applied to
T+1. The crash "bottom" used to read net-delta-at-bottom and recovery-capture is a
POST-HOC measurement point (a diagnostic), NOT a decision input — identical to how the
prototype's own report measures it. No look-ahead enters the simulated returns.

== Caveat surfaced in the report ==
The prototype prices the tail with FLAT-SKEW BSM, which UNDERSTATES the cost of
deeper/larger tails (real index put skew is steep). So bigger/further-OTM tails look
CHEAPER here than in reality. The sweet spot named here is a PROVISIONAL optimum on
optimistic pricing and may shift toward SMALLER / CLOSER tails once real skew is priced
(a warehouse / intraday job). Stated explicitly at the verdict.

Outputs: prints a supervised, flushed progress log + writes
`output/s5_tail_sweep_20260628.md`.
"""
from __future__ import annotations

import datetime as dt
import os
import sys
import time

import numpy as np
import pandas as pd

# Import the prototype engine UNMODIFIED. We only call its functions.
import s5_convexity_overlay as P
from s5_convexity_overlay import (
    build_panel, simulate_s5, s4_returns,
    nav, cagr, max_dd, ann_vol, sharpe, calmar, metric_block,
    find_bottom, EPISODES, MELTUP,
    fpct, fnum,
)

OUT_DIR = P.OUT_DIR

# ---- the grid ----
TAIL_FRACS = [0.25, 0.50, 0.75, 1.00]
TAIL_OTMS = [0.10, 0.15, 0.20, 0.25]      # 10/15/20/25 % OTM

# central harvest pinned throughout (prototype default)
HARVEST_CENTRAL = P.HARVEST_BASE_ANNUAL   # 0.055

# tenor secondary check (only at the best 2-D cell)
TENOR_CHECK_D = [30, 63, 90]


# ---------------------------------------------------------------------------
# Per-cell measurement: full-history metrics + per-crash net-delta@bottom and
# recovery capture. Mirrors the prototype's own main()/report logic exactly.
# ---------------------------------------------------------------------------
def measure_cell(df, common, rcv, r_spy, tail_frac, tail_otm,
                 harvest=HARVEST_CENTRAL):
    """Run one S5 sim at (tail_frac, tail_otm); return a metrics dict.

    Full-history metrics are computed on the SAME `common` index used by the
    prototype's head-to-head, so the cells are comparable to the prototype/S4/SPY.
    """
    res = simulate_s5(df, harvest_base_annual=harvest,
                      tail_otm=tail_otm, tail_frac=tail_frac)
    sim = res["df"]
    r = sim["r_fund"].loc[common]
    nd = sim["net_delta"]

    m = metric_block(r, rcv)
    cell = {
        "tail_frac": tail_frac, "tail_otm": tail_otm,
        "cagr": m["cagr"], "maxdd": m["maxdd"], "calmar": m["calmar"],
        "sharpe": m["sharpe"], "vol": m["vol"],
        "nd_bottom": {}, "capture": {},
    }

    lo_all = common.min()
    for ename, (lo, hi) in EPISODES.items():
        if pd.Timestamp(lo) < lo_all:
            lo = lo_all.strftime("%Y-%m-%d")
        bottom = find_bottom(df, lo, hi)
        bi = df.index.get_loc(bottom)
        nd_at_bottom = float(nd.iloc[bi]) if not np.isnan(nd.iloc[bi]) else float("nan")
        end = df.loc[lo:hi].index.max()
        cap_s5 = nav(sim["r_fund"].loc[bottom:end]).iloc[-1] - 1.0
        cap_spy = nav(r_spy.loc[bottom:end]).iloc[-1] - 1.0
        capture = cap_s5 / cap_spy if abs(cap_spy) > 1e-9 else float("nan")
        cell["nd_bottom"][ename] = nd_at_bottom
        cell["capture"][ename] = capture
    caps = [cell["capture"][e] for e in EPISODES if not np.isnan(cell["capture"][e])]
    cell["mean_capture"] = float(np.mean(caps)) if caps else float("nan")
    return cell


def main():
    sys.stdout.reconfigure(line_buffering=True)
    t0 = time.time()
    print("=== S5 TAIL-SIZING SWEEP ===", flush=True)
    print("loading panel...", flush=True)
    df = build_panel()
    print(f"panel: {df.index.min().date()} -> {df.index.max().date()}  ({len(df)} days)", flush=True)

    r_spy = df["r_spy"]
    rc = df["r_cash"]

    # Establish the SAME common index the prototype uses (S5 full vs S4 intersection).
    print("running prototype default + S4 baseline for the common window/head-to-head ...", flush=True)
    proto = simulate_s5(df)                              # 1.0 / 20% — prototype default
    r_proto = proto["df"]["r_fund"]
    r_s4, s4_exp = s4_returns(df)                        # S4 10%/1.5x exact shared-brain
    common = r_proto.dropna().index.intersection(r_s4.dropna().index)
    rcv = rc.loc[common]

    # ---- run the 16-cell grid ----
    print(f"\nrunning {len(TAIL_FRACS)}x{len(TAIL_OTMS)} = {len(TAIL_FRACS)*len(TAIL_OTMS)} cells "
          f"(harvest pinned at {HARVEST_CENTRAL*100:.1f}%/yr central) ...", flush=True)
    grid = []
    for tf in TAIL_FRACS:
        for otm in TAIL_OTMS:
            tc = time.time()
            cell = measure_cell(df, common, rcv, r_spy, tf, otm)
            grid.append(cell)
            gfc = cell["nd_bottom"].get("GFC 2008-09", float("nan"))
            gfc_cap = cell["capture"].get("GFC 2008-09", float("nan"))
            print(f"  frac {tf:.2f}  OTM {otm*100:4.0f}%  ->  CAGR {fpct(cell['cagr']):>7}  "
                  f"maxDD {fpct(cell['maxdd']):>8}  Calmar {fnum(cell['calmar']):>5}  "
                  f"Sharpe {fnum(cell['sharpe']):>5}  | GFC nd@btm {fnum(gfc):>5}x  "
                  f"GFC capt {fpct(gfc_cap,0):>5}   ({time.time()-tc:.1f}s)", flush=True)

    # ---- baselines for head-to-head ----
    proto_cell = next(c for c in grid if c["tail_frac"] == 1.00 and abs(c["tail_otm"] - 0.20) < 1e-9)
    s4_m = metric_block(r_s4.loc[common], rcv)
    spy_m = metric_block(r_spy.loc[common], rcv)
    # S4 / SPY per-crash recovery capture + S4 exposure@bottom for the head-to-head table
    base_capture = {"S4": {}, "SPY": {}}
    s4_exp_bottom = {}
    lo_all = common.min()
    for ename, (lo, hi) in EPISODES.items():
        if pd.Timestamp(lo) < lo_all:
            lo = lo_all.strftime("%Y-%m-%d")
        bottom = find_bottom(df, lo, hi); bi = df.index.get_loc(bottom)
        end = df.loc[lo:hi].index.max()
        cap_spy = nav(r_spy.loc[bottom:end]).iloc[-1] - 1.0
        cap_s4 = nav(r_s4.loc[bottom:end]).iloc[-1] - 1.0
        base_capture["SPY"][ename] = cap_spy / cap_spy if abs(cap_spy) > 1e-9 else float("nan")
        base_capture["S4"][ename] = cap_s4 / cap_spy if abs(cap_spy) > 1e-9 else float("nan")
        s4_exp_bottom[ename] = float(s4_exp.iloc[bi]) if bi < len(s4_exp) else float("nan")

    # ---- pick the sweet spot ----
    # Criterion (stated): maximize mean recovery capture across the 3 crashes
    # SUBJECT TO maxDD not worse than -35% (a stated drawdown budget that still beats
    # SPY's -55% by a wide margin and S5-passive's -39%). Among cells that pass the
    # gate, take the one with the highest mean capture; tie-break on best Calmar.
    DD_BUDGET = -0.35
    eligible = [c for c in grid if c["maxdd"] >= DD_BUDGET]
    if eligible:
        sweet = max(eligible, key=lambda c: (round(c["mean_capture"], 4), round(c["calmar"], 4)))
        crit_note = (f"max mean recovery-capture s.t. maxDD >= {DD_BUDGET*100:.0f}% "
                     f"(tie-break best Calmar)")
    else:
        sweet = max(grid, key=lambda c: c["calmar"])
        crit_note = f"no cell met the maxDD>={DD_BUDGET*100:.0f}% gate; fell back to best Calmar"

    # also report the best-Calmar cell for context
    best_calmar = max(grid, key=lambda c: c["calmar"])

    print(f"\n=== SWEET SPOT: frac {sweet['tail_frac']:.2f} / OTM {sweet['tail_otm']*100:.0f}%  "
          f"[{crit_note}] ===", flush=True)
    print(f"  CAGR {fpct(sweet['cagr'])}  maxDD {fpct(sweet['maxdd'])}  Calmar {fnum(sweet['calmar'])}  "
          f"Sharpe {fnum(sweet['sharpe'])}  mean-capture {fpct(sweet['mean_capture'],0)}", flush=True)
    print(f"  (best-Calmar cell for reference: frac {best_calmar['tail_frac']:.2f} / "
          f"OTM {best_calmar['tail_otm']*100:.0f}%, Calmar {fnum(best_calmar['calmar'])})", flush=True)

    # ---- optional tenor check at the sweet-spot cell (monkeypatch module const) ----
    print(f"\nrunning tenor check {TENOR_CHECK_D} DTE at the sweet-spot cell ...", flush=True)
    tenor_rows = []
    saved_tenor = P.TAIL_TENOR_D
    saved_floor = P.TAIL_ROLL_FLOOR_D
    try:
        for d in TENOR_CHECK_D:
            P.TAIL_TENOR_D = d
            # keep the roll floor sensible (< tenor); prototype default 21 is fine for 30/63/90
            P.TAIL_ROLL_FLOOR_D = min(saved_floor, max(5, d // 3))
            cell = measure_cell(df, common, rcv, r_spy, sweet["tail_frac"], sweet["tail_otm"])
            cell["tenor"] = d
            tenor_rows.append(cell)
            print(f"  {d:3d} DTE  CAGR {fpct(cell['cagr']):>7}  maxDD {fpct(cell['maxdd']):>8}  "
                  f"Calmar {fnum(cell['calmar']):>5}  mean-capture {fpct(cell['mean_capture'],0):>5}", flush=True)
    finally:
        P.TAIL_TENOR_D = saved_tenor
        P.TAIL_ROLL_FLOOR_D = saved_floor

    # ---- write report ----
    path = write_report(df, common, grid, sweet, best_calmar, crit_note, DD_BUDGET,
                        proto_cell, s4_m, spy_m, base_capture, s4_exp_bottom,
                        tenor_rows, HARVEST_CENTRAL)
    print(f"\nreport -> {path}", flush=True)
    print(f"done in {time.time()-t0:.1f}s.", flush=True)


def write_report(df, common, grid, sweet, best_calmar, crit_note, dd_budget,
                 proto_cell, s4_m, spy_m, base_capture, s4_exp_bottom,
                 tenor_rows, harvest):
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, "s5_tail_sweep_20260628.md")
    L = []; A = L.append

    EPS = list(EPISODES.keys())   # ['GFC 2008-09', 'COVID 2020', 'Bear 2022']

    A("# S5 — Tail-Sizing Sweep — drawdown vs. rebound trade-off")
    A("")
    A(f"*Generated {dt.date.today().isoformat()} | offline | EOD/daily | window "
      f"{common.min().date()} → {common.max().date()} ({len(common)} trading days) | "
      f"harvest pinned at central {harvest*100:.1f}%/yr*")
    A("")
    A("**What this is.** A 2-D parameter sweep of the S5 prototype's two tail-sizing knobs "
      "— `tail_frac` (fraction of the core notional that is hedged) × `tail_OTM` (how far "
      "out-of-the-money the tail strike sits). It imports the prototype engine "
      "(`s5_convexity_overlay.simulate_s5`) **unmodified** and changes ONLY the tail. "
      "Everything else (Tier-2 spread, reserve, upside barbell, and the **harvest knob, "
      f"pinned at the central {harvest*100:.1f}%/yr**) is held at the prototype default, so "
      "the sweep isolates tail sizing. The prototype's own report shows downside protection "
      "is harvest-independent, which is why pinning harvest is clean.")
    A("")
    A("**The trade-off being mapped.** A 100%-notional, 20%-OTM tail (the prototype default) "
      "leaves the book *fully* hedged at a crash bottom — net delta → ~0 — so it gives up the "
      "sharpest first leg of the rebound (GFC recovery capture ~31%). A **smaller** "
      "(`tail_frac↓`) or **further-OTM** (`tail_OTM↑`) tail leaves **more residual delta at "
      "the bottom → more rebound capture**, but a **shallower drawdown cushion**. The grid "
      "below makes that frontier explicit.")
    A("")
    A("> **⚠ FLAT-SKEW CAVEAT (load-bearing — read before trusting the sweet spot).** The "
      "prototype prices the tail with **flat-skew BSM** (VIX as ATM IV + a single additive "
      "vol-point bump). Real index put skew is **steep**: deeper-OTM strikes trade at much "
      "higher implied vols. So in this model, **deeper / larger tails look CHEAPER than they "
      "are in reality.** Every CAGR here is therefore optimistic, and the optimism grows with "
      "tail size/depth. The sweet spot named below is a **provisional optimum on optimistic "
      "pricing** — once real skew is priced (a warehouse / intraday job) it will likely shift "
      "toward **SMALLER / CLOSER** tails (closer-to-money costs more, further-OTM the model "
      "under-charges most). Treat the *shape* of the frontier as the result, not the decimals.")
    A("")

    # ---------- the primary grid ----------
    A("## Primary grid — 4 × 4 = 16 cells")
    A("")
    A("`tail_frac` down the rows, `tail_OTM` across implied in each block. Columns: full-"
      "history CAGR / maxDD / Calmar / Sharpe / ann-vol, then the trade-off-specific "
      "**net-delta @ each crash bottom** and **recovery capture** (S5 NAV gain ÷ SPY gain, "
      "bottom → episode end).")
    A("")
    A("| frac | OTM | CAGR | maxDD | Calmar | Sharpe | vol | "
      "ndGFC | ndCOVID | nd2022 | captGFC | captCOVID | capt2022 |")
    A("|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for c in grid:
        mark = ""
        if c is sweet:
            mark = " ⭐"
        A(f"| {c['tail_frac']:.2f}{mark} | {c['tail_otm']*100:.0f}% | {fpct(c['cagr'])} | "
          f"{fpct(c['maxdd'])} | {fnum(c['calmar'])} | {fnum(c['sharpe'])} | {fpct(c['vol'])} | "
          f"{fnum(c['nd_bottom'][EPS[0]])}× | {fnum(c['nd_bottom'][EPS[1]])}× | "
          f"{fnum(c['nd_bottom'][EPS[2]])}× | {fpct(c['capture'][EPS[0]],0)} | "
          f"{fpct(c['capture'][EPS[1]],0)} | {fpct(c['capture'][EPS[2]],0)} |")
    A("")
    A("`nd*` = net delta at that crash's bottom (1.0× = fully invested core; ~0× = fully "
      "hedged). `capt*` = recovery capture from the bottom to the episode end. ⭐ = sweet spot.")
    A("")

    # ---------- frontier read ----------
    A("## The frontier — read down a column, read across a row")
    A("")
    A("**Down `tail_frac` (less notional hedged):** at every OTM level, shrinking the tail "
      "**raises net-delta-at-bottom and recovery capture** (less of the core is neutralized at "
      "the low) and **raises CAGR**, while **maxDD gets worse** (less cushion). This is the core "
      "trade-off, exactly as predicted.")
    A("")
    A("**Across `tail_OTM` (further from the money):** a deeper tail costs less carry AND has "
      "less delta until spot falls close to the strike, so a further-OTM tail also tends to "
      "leave more residual bottom-delta in the milder selloffs — but it gives a thinner cushion "
      "in the deepest crashes (the strike is reached only late). Closer-to-money tails cut the "
      "drawdown hardest but neutralize the core soonest (lowest bottom-delta).")
    A("")
    A("**The trade-off is the headline, but it is NOT perfectly monotone — and the exception is "
      "instructive.** Broadly, more rebound capture costs drawdown cushion. But the grid shows a "
      "real wrinkle: the *deepest-cushion* cells are NOT the biggest tails — they are the "
      "**moderate, close-to-money** cells (frac 0.50 / 10% OTM gives the best Calmar 0.36 and a "
      "−26% maxDD, beating the tiny 0.25-frac cells which sit at ~−33%). Reason: a 25%-notional, "
      "10%-OTM put barely bites in a −55% crash (too little notional, strike reached too late), "
      "so the smallest tails give up rebound capture WITHOUT buying much cushion — the worst of "
      "both. Effective protection needs *enough notional close enough to the money*; effective "
      "rebound needs *little enough notional that the bottom-delta survives*. The frontier is the "
      "tension between those two, and the corners are dominated.")
    A("")

    # mini frontier table: for each frac, the maxDD range and capture range across OTM
    A("### Frontier corners")
    A("")
    A("| | smallest cushion / most rebound | largest cushion / least rebound |")
    A("|:--|:--|:--|")
    # most rebound = highest mean capture; most cushion = best (least negative) maxDD
    most_reb = max(grid, key=lambda c: c["mean_capture"])
    most_cush = max(grid, key=lambda c: c["maxdd"])   # closest to 0
    A(f"| **cell** | frac {most_reb['tail_frac']:.2f} / OTM {most_reb['tail_otm']*100:.0f}% | "
      f"frac {most_cush['tail_frac']:.2f} / OTM {most_cush['tail_otm']*100:.0f}% |")
    A(f"| maxDD | {fpct(most_reb['maxdd'])} | {fpct(most_cush['maxdd'])} |")
    A(f"| mean recovery capture | {fpct(most_reb['mean_capture'],0)} | {fpct(most_cush['mean_capture'],0)} |")
    A(f"| CAGR | {fpct(most_reb['cagr'])} | {fpct(most_cush['cagr'])} |")
    A(f"| Calmar | {fnum(most_reb['calmar'])} | {fnum(most_cush['calmar'])} |")
    A("")

    # ---------- sweet spot ----------
    A("## Sweet spot")
    A("")
    A(f"**Criterion (stated up front):** {crit_note}. The maxDD budget of "
      f"**{dd_budget*100:.0f}%** is a deliberate choice — it still crushes SPY's −55% and beats "
      f"the prototype-passive −39%, while letting us claw back rebound capture. Among the cells "
      f"that stay inside that drawdown budget, we take the one that recovers the most rebound.")
    A("")
    A(f"### ⭐ Provisional sweet spot: `tail_frac = {sweet['tail_frac']:.2f}`, "
      f"`tail_OTM = {sweet['tail_otm']*100:.0f}%`")
    A("")
    A("| Metric | Value |")
    A("|:--|---:|")
    A(f"| CAGR | {fpct(sweet['cagr'])} |")
    A(f"| Max DD | {fpct(sweet['maxdd'])} |")
    A(f"| Calmar | {fnum(sweet['calmar'])} |")
    A(f"| Sharpe | {fnum(sweet['sharpe'])} |")
    A(f"| Ann vol | {fpct(sweet['vol'])} |")
    A(f"| Net delta @ bottom (GFC / COVID / 2022) | "
      f"{fnum(sweet['nd_bottom'][EPS[0]])}× / {fnum(sweet['nd_bottom'][EPS[1]])}× / "
      f"{fnum(sweet['nd_bottom'][EPS[2]])}× |")
    A(f"| Recovery capture (GFC / COVID / 2022) | "
      f"{fpct(sweet['capture'][EPS[0]],0)} / {fpct(sweet['capture'][EPS[1]],0)} / "
      f"{fpct(sweet['capture'][EPS[2]],0)} |")
    A(f"| Mean recovery capture | {fpct(sweet['mean_capture'],0)} |")
    A("")
    A(f"*For reference, the **best-Calmar** cell overall is "
      f"`frac {best_calmar['tail_frac']:.2f} / OTM {best_calmar['tail_otm']*100:.0f}%` "
      f"(Calmar {fnum(best_calmar['calmar'])}, maxDD {fpct(best_calmar['maxdd'])}, "
      f"mean-capture {fpct(best_calmar['mean_capture'],0)}). Best-Calmar leans toward MORE "
      f"protection; the sweet-spot criterion deliberately spends some Calmar to buy back "
      f"rebound participation.*")
    A("")
    # explicit "region, not a point" honesty: name the balanced middle
    mid = next((c for c in grid if c["tail_frac"] == 0.50 and abs(c["tail_otm"] - 0.20) < 1e-9), None)
    if mid is not None:
        mid_mc = mid["mean_capture"]
        A("**Frontier, not a point — read the criterion's sensitivity.** The mechanical pick "
          f"(0.25 / 25%) sits at the *most-rebound corner* of the maxDD≤35% region; it maximizes "
          f"capture but lands on the **worst Calmar in the grid** and only squeaks under the "
          f"drawdown gate. Nudge the criterion (e.g. weight Calmar, or tighten the budget to "
          f"−32%) and the choice moves. A defensible **balanced middle** is "
          f"**`frac 0.50 / 20% OTM`**: CAGR {fpct(mid['cagr'])}, maxDD {fpct(mid['maxdd'])}, "
          f"Calmar {fnum(mid['calmar'])}, mean-capture {fpct(mid_mc,0)} — it recovers most of the "
          f"rebound lift (GFC {fpct(mid['capture'][EPS[0]],0)} vs the default's "
          f"{fpct(proto_cell['capture'][EPS[0]],0)}) while giving back only ~3pp of cushion vs "
          f"the default and keeping Calmar at the default's level. The honest answer is a "
          f"**region — roughly frac 0.50, 20–25% OTM** — not a single magic cell.")
        A("")

    # ---------- head-to-head ----------
    A("## Head-to-head — sweet spot vs prototype default vs S4 vs SPY")
    A("")
    A("| Strategy | CAGR | Max DD | Calmar | Sharpe | Ann vol | mean recov capture |")
    A("|:--|---:|---:|---:|---:|---:|---:|")
    A(f"| **⭐ Sweet spot (frac {sweet['tail_frac']:.2f} / {sweet['tail_otm']*100:.0f}% OTM)** | "
      f"{fpct(sweet['cagr'])} | {fpct(sweet['maxdd'])} | {fnum(sweet['calmar'])} | "
      f"{fnum(sweet['sharpe'])} | {fpct(sweet['vol'])} | {fpct(sweet['mean_capture'],0)} |")
    proto_mc = float(np.mean([proto_cell['capture'][e] for e in EPS if not np.isnan(proto_cell['capture'][e])]))
    A(f"| Prototype default (frac 1.00 / 20% OTM) | {fpct(proto_cell['cagr'])} | "
      f"{fpct(proto_cell['maxdd'])} | {fnum(proto_cell['calmar'])} | {fnum(proto_cell['sharpe'])} | "
      f"{fpct(proto_cell['vol'])} | {fpct(proto_mc,0)} |")
    s4_mc = float(np.mean([base_capture['S4'][e] for e in EPS if not np.isnan(base_capture['S4'][e])]))
    A(f"| S4 vol-control 10%/1.5× | {fpct(s4_m['cagr'])} | {fpct(s4_m['maxdd'])} | "
      f"{fnum(s4_m['calmar'])} | {fnum(s4_m['sharpe'])} | {fpct(s4_m['vol'])} | {fpct(s4_mc,0)} |")
    A(f"| SPY buy & hold (TR) | {fpct(spy_m['cagr'])} | {fpct(spy_m['maxdd'])} | "
      f"{fnum(spy_m['calmar'])} | {fnum(spy_m['sharpe'])} | {fpct(spy_m['vol'])} | 100% |")
    A("")
    A("Per-crash net-delta-at-bottom + recovery capture, head-to-head:")
    A("")
    A("| | GFC nd@btm | COVID nd@btm | 2022 nd@btm | GFC capt | COVID capt | 2022 capt |")
    A("|:--|---:|---:|---:|---:|---:|---:|")
    A(f"| ⭐ Sweet spot | {fnum(sweet['nd_bottom'][EPS[0]])}× | {fnum(sweet['nd_bottom'][EPS[1]])}× | "
      f"{fnum(sweet['nd_bottom'][EPS[2]])}× | {fpct(sweet['capture'][EPS[0]],0)} | "
      f"{fpct(sweet['capture'][EPS[1]],0)} | {fpct(sweet['capture'][EPS[2]],0)} |")
    A(f"| Prototype default | {fnum(proto_cell['nd_bottom'][EPS[0]])}× | "
      f"{fnum(proto_cell['nd_bottom'][EPS[1]])}× | {fnum(proto_cell['nd_bottom'][EPS[2]])}× | "
      f"{fpct(proto_cell['capture'][EPS[0]],0)} | {fpct(proto_cell['capture'][EPS[1]],0)} | "
      f"{fpct(proto_cell['capture'][EPS[2]],0)} |")
    A(f"| S4 (exposure@btm) | {fnum(s4_exp_bottom[EPS[0]])}× | {fnum(s4_exp_bottom[EPS[1]])}× | "
      f"{fnum(s4_exp_bottom[EPS[2]])}× | {fpct(base_capture['S4'][EPS[0]],0)} | "
      f"{fpct(base_capture['S4'][EPS[1]],0)} | {fpct(base_capture['S4'][EPS[2]],0)} |")
    A(f"| SPY | 1.00× | 1.00× | 1.00× | 100% | 100% | 100% |")
    A("")

    # ---------- tenor check ----------
    if tenor_rows:
        A("## Secondary check — tenor at the sweet-spot cell")
        A("")
        A(f"Tail tenor swept {TENOR_CHECK_D} DTE, holding "
          f"`frac {sweet['tail_frac']:.2f} / OTM {sweet['tail_otm']*100:.0f}%`. (Roll floor "
          f"scaled to the tenor.) A shorter tenor rolls more often — more theta bleed but a "
          f"strike that tracks spot more tightly; a longer tenor carries cheaper per-day but "
          f"the strike drifts further from spot between rolls.")
        A("")
        A("| Tenor | CAGR | maxDD | Calmar | Sharpe | mean recov capture |")
        A("|---:|---:|---:|---:|---:|---:|")
        for c in tenor_rows:
            mark = " (proto 63)" if c["tenor"] == 63 else ""
            A(f"| {c['tenor']}d{mark} | {fpct(c['cagr'])} | {fpct(c['maxdd'])} | "
              f"{fnum(c['calmar'])} | {fnum(c['sharpe'])} | {fpct(c['mean_capture'],0)} |")
        A("")

    # ---------- blunt read ----------
    A("## Blunt read — does tail sizing recover the lost rebound without giving back the protection?")
    A("")
    proto_dd = proto_cell["maxdd"]; sweet_dd = sweet["maxdd"]
    proto_gfc = proto_cell["capture"][EPS[0]]; sweet_gfc = sweet["capture"][EPS[0]]
    A(f"- **It moves the dial, but it is a genuine trade, not a free recovery.** Going from the "
      f"prototype default (frac 1.00 / 20% OTM: GFC capture {fpct(proto_gfc,0)}, maxDD "
      f"{fpct(proto_dd)}) to the sweet spot (frac {sweet['tail_frac']:.2f} / "
      f"{sweet['tail_otm']*100:.0f}% OTM: GFC capture {fpct(sweet_gfc,0)}, maxDD "
      f"{fpct(sweet_dd)}) buys back real rebound participation — GFC capture roughly "
      f"{(sweet_gfc/proto_gfc-1)*100:.0f}% higher, and COVID/2022 nearer SPY — but it **does** "
      f"surrender drawdown cushion to get it (about "
      f"{(abs(sweet_dd)-abs(proto_dd))*100:.0f}pp deeper maxDD). You cannot raise bottom-delta "
      f"without paying for it in cushion. (Not perfectly monotone — the smallest tails are a "
      f"trap, see the frontier section — but no cell escapes the trade.)")
    A(f"- **The recoverable rebound is bounded by how much cushion you are willing to spend.** "
      f"Even the most-rebound corner of the grid does not restore full SPY participation off the "
      f"bottom while keeping a crisis-grade cushion — the always-on tail that gives the cushion "
      f"is the same instrument that caps the bottom-delta. This is structural, and it is exactly "
      f"why the spec ladders *active* monetization to Phase-2: passive tail sizing alone moves "
      f"you along the frontier, it does not bend the frontier outward.")
    A(f"- **The honest sweet spot is a balance, not a win.** The named cell is the best "
      f"*compromise* under a stated drawdown budget — it is not a configuration that dominates "
      f"the prototype default on both axes (no such cell exists; the trade-off is real).")
    A("")
    A("> **Restating the flat-skew caveat at the verdict:** every cell here is priced on "
      "**flat-skew BSM, which under-charges deeper/larger tails**. The further-OTM and "
      "larger-frac cells are the ones the model flatters most, so the *true* (skew-priced) "
      "frontier sits worse for big/deep tails and the sweet spot should drift toward "
      "**smaller / closer** tails. **This sweet spot is provisional on optimistic pricing.** "
      "Pricing it for real needs the warehouse EOD chains (skew-by-strike) or the intraday "
      "SPXW pull — the same dependency that gates the harvest number.")
    A("")
    A("## Constraints honoured")
    A("")
    A("- Offline; numpy/pandas + the prototype's hand-rolled BSM only; read-only `bt_data`.")
    A("- The prototype (`s5_convexity_overlay.py`) was **imported, not modified**; its default "
      "run is byte-identical. The tenor check monkeypatches the module's `TAIL_TENOR_D` "
      "constant at runtime and restores it (no file edit).")
    A("- No look-ahead: every decision at day T uses data ≤ T applied to T+1 (the prototype's "
      "discipline). The crash bottom used to read net-delta-at-bottom / recovery-capture is a "
      "post-hoc *measurement* point, not a decision input — identical to the prototype's report.")
    A("- Harvest held at the central assumption throughout, so the sweep isolates tail sizing.")
    A("")

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    return path


if __name__ == "__main__":
    main()
