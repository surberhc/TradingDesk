r"""
s5_harvest_analysis.py -- honest analysis of the measured S5 0DTE harvest + the
financing-ledger self-funding verdict.

PAPER / research only. OFFLINE. ASCII-only. Reads s5_research/s5_harvest_trades.csv
(the output of s5_harvest_engine.py). Writes output/s5_harvest_engine_<date>.md.

================================================================================
WHAT IT REPORTS (measurement first; verdict second)
================================================================================
1. Per-day NET-CREDIT distribution with honest fills + commission (mean/median/std,
   pctiles), traded-day count, calm-day count.
2. LOSS distribution & CLUSTERING -- the read that matters for a negative-skew seller:
   loss/win ratio, worst days, max consecutive losing days, loss concentration.
   Win rate is reported but explicitly labelled cosmetic.
3. The PLATEAU test (anti-curve-fit): does the net harvest hold across BOTH time-halves
   AND all day-type sub-buckets (calm vs not; VIX terciles)? A financing leg that only
   works in one half or one bucket is not a plateau.
4. FINANCING LEDGER: convert the measured $/condor/day harvest to %/yr of core notional
   at a stated conservative overlay sizing, and ask -- does it cover the VALIDATED tail
   carry (1.56%/yr at the 0.50/25% sweet spot; and the 4.46%/yr full-notional deep tail
   the brief cites) across a FULL CYCLE, including choppy-no-crash stretches?

The sizing convention is STATED, not tuned. We report the break-even overlay size and let
the reader judge; we do not pick a size to make it pass.
================================================================================
"""
from __future__ import annotations

import datetime as _dt
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
RESEARCH = HERE / "output" / "s5_research"
TRADES_CSV = RESEARCH / "s5_harvest_trades.csv"
PARTIAL_CSV = RESEARCH / "s5_harvest_trades_partial.csv"
OUT_MD = HERE / "output" / f"s5_harvest_engine_{_dt.date.today().strftime('%Y%m%d')}.md"

# Core-notional / sizing constants (STATED, not tuned).
SPX_MULT = 100.0                 # SPX options $100/point
CORE_NOTIONAL = 600_000.0        # one SPX core unit ~= index*100 ~ $600k (spec sec 6)
TRADING_DAYS = 252.0
# Validated tail-carry benchmarks (real-skew report, 2026-06-28):
TAIL_CARRY_SWEET = 0.0156        # 0.50 notional / 25% OTM real-skew carry, %/yr
TAIL_CARRY_FULL_DEEP = 0.0446    # full-notional deep tail the brief cites, %/yr


def load_trades() -> pd.DataFrame:
    p = TRADES_CSV if TRADES_CSV.is_file() else PARTIAL_CSV
    df = pd.read_csv(p)
    for b in ("traded", "calm", "calm_loose", "breached_put", "breached_call"):
        if b in df.columns:
            df[b] = df[b].astype(str).str.lower().isin(["true", "1"])
    df["day"] = pd.to_datetime(df["day"])
    return df.sort_values("day").reset_index(drop=True)


def _pctiles(s: pd.Series) -> dict:
    q = s.quantile([0.01, 0.05, 0.25, 0.50, 0.75, 0.95, 0.99])
    return {f"p{int(k*100)}": float(v) for k, v in q.items()}


def dist_block(t: pd.DataFrame, label: str) -> list:
    """Distribution + loss-clustering lines for a traded subset."""
    L = []
    n = len(t)
    if n == 0:
        return [f"{label}: (no traded days)"]
    pnl = t["pnl_dollars"]
    wins = t[pnl > 0]; losses = t[pnl <= 0]
    win_rate = len(wins) / n
    avg_win = wins["pnl_dollars"].mean() if len(wins) else 0.0
    avg_loss = losses["pnl_dollars"].mean() if len(losses) else 0.0
    lw = abs(avg_loss) / avg_win if avg_win > 0 else float("nan")
    # consecutive losing days
    losing = (pnl.values <= 0).astype(int)
    streak = cur = 0
    for v in losing:
        cur = cur + 1 if v else 0
        streak = max(streak, cur)
    # loss concentration: share of total loss from the worst 5% of days
    tot_loss = -losses["pnl_dollars"].sum() if len(losses) else 0.0
    worst5 = losses.nsmallest(max(1, int(np.ceil(0.05 * n))), "pnl_dollars")
    worst5_share = (-worst5["pnl_dollars"].sum() / tot_loss) if tot_loss > 0 else float("nan")
    L.append(f"{label}:")
    L.append(f"  traded days           {n}")
    L.append(f"  net $/day  mean {pnl.mean():+8.2f}  median {pnl.median():+8.2f}  "
             f"std {pnl.std():8.2f}  sum {pnl.sum():+10.2f}")
    pc = _pctiles(pnl)
    L.append(f"  net $/day  p01 {pc['p1']:+7.0f}  p05 {pc['p5']:+7.0f}  p25 {pc['p25']:+7.0f}  "
             f"p50 {pc['p50']:+7.0f}  p75 {pc['p75']:+7.0f}  p95 {pc['p95']:+7.0f}  p99 {pc['p99']:+7.0f}")
    L.append(f"  win rate {win_rate:.1%} (COSMETIC)  avg win {avg_win:+.2f}  "
             f"avg loss {avg_loss:+.2f}  LOSS/WIN {lw:.2f}x")
    L.append(f"  worst day {pnl.min():+.0f}  max consec losing days {streak}  "
             f"worst-5%-of-days = {worst5_share:.0%} of all losses")
    L.append(f"  breach rate  put {t['breached_put'].mean():.1%}  call {t['breached_call'].mean():.1%}  "
             f"either {(t['breached_put']|t['breached_call']).mean():.1%}")
    return L


def harvest_pct_of_notional(t: pd.DataFrame) -> dict:
    """Convert mean net $/condor/day into %/yr of core notional at a STATED overlay size.

    Sizing convention (stated, not tuned): sell ONE condor per core unit per calm sell-day.
    Annual harvest $ = mean_net_$/day * sell_days_per_year. As a fraction of one core unit
    ($600k), that is the %/yr the overlay contributes to financing. We report it for:
      - one condor / core unit / day (the natural 1:1 overlay), and
      - the break-even multiple: how many condors/core-unit/day are needed to cover each
        tail-carry benchmark (if mean net is positive; N/A if negative -- no size covers a
        negative mean).
    """
    n = len(t)
    if n == 0:
        return {}
    # sell-days per year on THIS subset (its own cadence within the sample)
    span_days = (t["day"].max() - t["day"].min()).days
    yrs = max(span_days / 365.25, 1e-9)
    sell_days_per_yr = n / yrs
    mean_net = t["pnl_dollars"].mean()
    ann_harvest_dollars = mean_net * sell_days_per_yr
    pct_of_core = ann_harvest_dollars / CORE_NOTIONAL
    out = {
        "sell_days_per_yr": sell_days_per_yr,
        "mean_net_per_day": mean_net,
        "ann_harvest_$": ann_harvest_dollars,
        "pct_of_core_1to1": pct_of_core,
        "covers_sweet": pct_of_core >= TAIL_CARRY_SWEET,
        "covers_full_deep": pct_of_core >= TAIL_CARRY_FULL_DEEP,
    }
    if mean_net > 0:
        out["condors_needed_sweet"] = TAIL_CARRY_SWEET * CORE_NOTIONAL / ann_harvest_dollars
        out["condors_needed_full"] = TAIL_CARRY_FULL_DEEP * CORE_NOTIONAL / ann_harvest_dollars
    else:
        out["condors_needed_sweet"] = float("inf")
        out["condors_needed_full"] = float("inf")
    return out


def per_year_ledger(t: pd.DataFrame) -> pd.DataFrame:
    """Net harvest $ per calendar year -- the full-cycle deficit read (Design Rule B)."""
    g = t.copy()
    g["year"] = g["day"].dt.year
    rows = []
    for y, sub in g.groupby("year"):
        pnl = sub["pnl_dollars"]
        rows.append({
            "year": y, "sell_days": len(sub),
            "net_$": round(pnl.sum(), 0),
            "mean_$/day": round(pnl.mean(), 2),
            "breach_rate": round((sub["breached_put"] | sub["breached_call"]).mean(), 3),
            "worst_day_$": round(pnl.min(), 0),
        })
    return pd.DataFrame(rows)


def build_report(df: pd.DataFrame) -> str:
    traded = df[df["traded"]].copy()
    calm = traded[traded["calm"]]
    calm_loose = traded[traded["calm_loose"]]
    not_calm = traded[~traded["calm_loose"]]

    # time halves (by traded-day order)
    mid = len(traded) // 2
    h1 = traded.iloc[:mid]; h2 = traded.iloc[mid:]

    L = []
    A = L.append
    A("# S5 real harvest engine -- honest measurement + self-funding verdict")
    A("")
    A(f"*Generated {_dt.date.today().isoformat()} | OFFLINE | 1-min SPXW 0DTE warehouse | "
      f"window {df['day'].min().date()} -> {df['day'].max().date()} | "
      f"{len(df)} days, {len(traded)} traded*")
    A("")
    A("**The rule (FROZEN, not swept):** fixed 0.15 short-leg delta iron condor, ~14:00 ET "
      "entry, HONEST fills (sell bid / buy ask), defined-risk 5-wide wings, HELD FLAT TO "
      "16:00 PM settlement (NO 2x stop -- the S5 tail is the catastrophe backstop), "
      "$0.65/leg commission on entry legs plus any ITM cash-settled legs. 1 contract.")
    A("")
    A("**Why this is not the refuted S6 experiment:** every prior S6 arm used a 2x-credit "
      "intraday STOP, which manufactures the negative skew that sank it. This measures the "
      "hold-to-settle, defined-risk rule S5 actually implies. The exit regime is different.")
    A("")

    A("## 1-2. Net-credit distribution + loss clustering (honest fills, net of commission)")
    A("")
    A("```")
    for line in dist_block(traded, "ALL traded days"):
        A(line)
    A("")
    for line in dist_block(calm, f"CALM days (prior VIX <= 15)"):
        A(line)
    A("")
    for line in dist_block(calm_loose, f"CALM-loose days (prior VIX <= 20)"):
        A(line)
    A("")
    for line in dist_block(not_calm, f"NOT-calm days (prior VIX > 20)"):
        A(line)
    A("```")
    A("")

    A("## 3. Plateau test (anti-curve-fit) -- both time-halves AND all day-type buckets")
    A("")
    A("```")
    for line in dist_block(h1, f"TIME HALF 1 ({h1['day'].min().date() if len(h1) else '-'} "
                              f"-> {h1['day'].max().date() if len(h1) else '-'})"):
        A(line)
    A("")
    for line in dist_block(h2, f"TIME HALF 2 ({h2['day'].min().date() if len(h2) else '-'} "
                              f"-> {h2['day'].max().date() if len(h2) else '-'})"):
        A(line)
    A("")
    # VIX terciles among traded days
    if traded["prior_vix"].notna().sum() > 10:
        tv = traded.dropna(subset=["prior_vix"]).copy()
        qs = tv["prior_vix"].quantile([1/3, 2/3]).values
        tv["vix_tercile"] = np.where(tv["prior_vix"] <= qs[0], "low",
                             np.where(tv["prior_vix"] <= qs[1], "mid", "high"))
        for terc in ("low", "mid", "high"):
            for line in dist_block(tv[tv["vix_tercile"] == terc],
                                   f"VIX TERCILE {terc} (cuts {qs[0]:.1f}/{qs[1]:.1f})"):
                A(line)
            A("")
    A("```")
    A("")

    A("## 4. Financing ledger -- does measured harvest cover the tail carry?")
    A("")
    A(f"Tail-carry benchmarks (validated, real-skew report 2026-06-28): "
      f"**sweet spot 0.50/25%-OTM = {TAIL_CARRY_SWEET:.2%}/yr** of NAV; "
      f"full-notional deep tail (brief) = {TAIL_CARRY_FULL_DEEP:.2%}/yr. "
      f"Core unit assumed ${CORE_NOTIONAL:,.0f} (index*100).")
    A("")
    A("**Sizing convention (STATED, not tuned):** one condor per core unit per sell-day. "
      "We report the resulting %/yr and the break-even condor multiple.")
    A("")
    A("| Sell-day set | sell-days/yr | mean net $/day | ann harvest $ | % of core (1:1) | "
      "covers sweet 1.56%? | covers full 4.46%? |")
    A("|:--|--:|--:|--:|--:|:--:|:--:|")
    for name, sub in [("ALL traded", traded), ("CALM (VIX<=15)", calm),
                      ("CALM-loose (VIX<=20)", calm_loose)]:
        h = harvest_pct_of_notional(sub)
        if not h:
            A(f"| {name} | - | - | - | - | - | - |")
            continue
        A(f"| {name} | {h['sell_days_per_yr']:.0f} | {h['mean_net_per_day']:+.2f} | "
          f"{h['ann_harvest_$']:+,.0f} | {h['pct_of_core_1to1']:+.2%} | "
          f"{'YES' if h['covers_sweet'] else 'NO'} | "
          f"{'YES' if h['covers_full_deep'] else 'NO'} |")
    A("")

    A("### Per-year ledger (the full-cycle / choppy-no-crash deficit read, Design Rule B)")
    A("")
    A("ALL traded days, net harvest $ per calendar year (1 condor, $100 mult):")
    A("")
    yl = per_year_ledger(traded)
    A("| year | sell-days | net $ | mean $/day | breach rate | worst day $ |")
    A("|--:|--:|--:|--:|--:|--:|")
    for _, r in yl.iterrows():
        A(f"| {int(r['year'])} | {int(r['sell_days'])} | {r['net_$']:+,.0f} | "
          f"{r['mean_$/day']:+.2f} | {r['breach_rate']:.1%} | {r['worst_day_$']:+,.0f} |")
    A("")
    if len(calm):
        A("CALM days only (VIX<=15), net harvest $ per calendar year:")
        A("")
        ylc = per_year_ledger(calm)
        A("| year | sell-days | net $ | mean $/day | breach rate | worst day $ |")
        A("|--:|--:|--:|--:|--:|--:|")
        for _, r in ylc.iterrows():
            A(f"| {int(r['year'])} | {int(r['sell_days'])} | {r['net_$']:+,.0f} | "
              f"{r['mean_$/day']:+.2f} | {r['breach_rate']:.1%} | {r['worst_day_$']:+,.0f} |")
        A("")

    # --- verdict ---
    all_h = harvest_pct_of_notional(traded)
    calm_h = harvest_pct_of_notional(calm) if len(calm) else {}
    A("## VERDICT")
    A("")
    verdict_lines = _verdict(traded, calm, all_h, calm_h, h1, h2, yl)
    for v in verdict_lines:
        A(v)
    A("")
    return "\n".join(L) + "\n"


def _verdict(traded, calm, all_h, calm_h, h1, h2, yl) -> list:
    L = []
    all_mean = traded["pnl_dollars"].mean() if len(traded) else float("nan")
    calm_mean = calm["pnl_dollars"].mean() if len(calm) else float("nan")
    h1_mean = h1["pnl_dollars"].mean() if len(h1) else float("nan")
    h2_mean = h2["pnl_dollars"].mean() if len(h2) else float("nan")
    pos_years = int((yl["net_$"] > 0).sum()); tot_years = len(yl)

    self_funds = bool(all_h.get("covers_sweet", False))
    calm_self_funds = bool(calm_h.get("covers_sweet", False)) if calm_h else False

    if all_mean <= 0 and (not calm_h or calm_mean <= 0):
        L.append("**HARVEST DOES NOT SELF-FUND.** The mean net credit per sell-day is "
                 "NEGATIVE with honest fills held to settlement -- on all days AND on the "
                 "pre-specified calm days. A negative mean cannot finance a positive tail "
                 "carry at ANY overlay size. The hold-to-settle rule confirms what the "
                 "2x-stop chassis showed by a different route: the defined-risk 0DTE "
                 "seller's per-day loss on breach (full 5-wide wing) overwhelms the thin "
                 f"~$70 credit. Full-cycle: {pos_years}/{tot_years} years net-positive.")
    elif self_funds or calm_self_funds:
        L.append("**HARVEST PLAUSIBLY SELF-FUNDS AT 1:1** -- but read the plateau and "
                 "full-cycle caveats before trusting it. Mean net credit is positive and "
                 "a 1-condor/core-unit overlay clears the 1.56%/yr sweet-spot carry.")
    else:
        L.append("**HARVEST IS NET-POSITIVE BUT DOES NOT COVER THE TAIL AT 1:1.** Mean net "
                 "credit is positive but below the tail carry at the natural 1:1 overlay; "
                 "covering it needs a larger overlay (see break-even multiple), which raises "
                 "the short-gamma load -- weigh against Design Rule A (net convexity long).")
    L.append("")
    L.append(f"- Mean net $/sell-day: ALL {all_mean:+.2f} | CALM(VIX<=15) "
             f"{calm_mean:+.2f} | half1 {h1_mean:+.2f} | half2 {h2_mean:+.2f}.")
    L.append(f"- Plateau: net harvest sign consistent across halves = "
             f"{'YES' if (h1_mean>0)==(h2_mean>0) else 'NO (fragile)'}.")
    L.append(f"- Full-cycle: {pos_years}/{tot_years} calendar years net-positive.")
    L.append("- Loss/win skew is the driver -- see the loss-clustering block. Win rate is "
             "cosmetic; the negative skew (full-wing breach vs thin credit) is the economics.")
    return L


def main():
    import sys
    sys.stdout.reconfigure(line_buffering=True)
    df = load_trades()
    print(f"[load] {len(df)} rows, {int(df['traded'].sum())} traded", flush=True)
    report = build_report(df)
    OUT_MD.write_text(report, encoding="utf-8")
    print(f"[done] wrote {OUT_MD}", flush=True)
    # echo the verdict block to console (ASCII)
    tail = report.split("## VERDICT")[-1]
    print("\n=== VERDICT ===")
    print(tail.encode("ascii", "replace").decode("ascii"))


if __name__ == "__main__":
    main()
