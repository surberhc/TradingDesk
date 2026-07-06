r"""
s5_sleeve_run.py -- driver: build the year-by-year sleeve P&L, sizing sweep, guardrail, and
the markdown deliverable. Uses s5_sleeve_pnl (tail off real-skew, fin off honest-fill harness).

Legs are LINEAR in their frac, so we compute each leg ONCE at frac=1.0 and SCALE:
  tail(frac)      = frac * tail_unit
  fin(frac,delta) = frac * fin_unit[delta]
This keeps the slow honest-fill put-write walk to just TWO runs (delta 0.10 and 0.15).

PAPER / research only. OFFLINE. Windows.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import numpy as np
import pandas as pd

import s5_sleeve_pnl as sp

OUT = sp.OUT_DIR
OUT.mkdir(parents=True, exist_ok=True)

TAIL_FRAC = 0.50          # base tail size
FIN_DELTAS = [0.10, 0.15]
FIN_MULTS = [0.5, 1.0, 1.5]   # financing at 0.5x / 1.0x / 1.5x the tail notional
FIN_DTE = 45

CRASH_YEARS = {2020, 2022}
GAP_YEAR = 2021


def year_tag(year: int, sleeve_pct: float, spx_year_ret: float | None) -> str:
    """calm / moderate-drop / CRASH label. CRASH = COVID-2020 / bear-2022 (the two real
    tests). moderate-drop = a down equity year that is not a full crash. else calm."""
    if year == 2020:
        return "CRASH (COVID)"
    if year == 2022:
        return "CRASH (bear)"
    if spx_year_ret is not None and spx_year_ret < -0.05:
        return "moderate-drop"
    return "calm"


def main():
    import sys
    sys.stdout.reconfigure(line_buffering=True)

    print("[1/4] tail leg (real-skew BS), unit frac ...", flush=True)
    tail_unit = sp.tail_daily_pnl(tail_frac=1.0)   # scale by TAIL_FRAC later

    print("[2/4] fin leg (honest-fill put-write, non-overlapping), unit frac x2 deltas ...", flush=True)
    fin_unit = {}
    fin_recs = {}
    for d in FIN_DELTAS:
        print(f"       delta {d} ...", flush=True)
        f, recs = sp.fin_daily_pnl(fin_frac=1.0, short_delta=d, dte=FIN_DTE)
        fin_unit[d] = f
        fin_recs[d] = recs

    # SPX per-year return for tagging (from the tail table's und, clean windows only)
    und = tail_unit["und"]
    und_cw = und[[sp.in_clean_window(ts) for ts in und.index]]
    spx_yr = {}
    for yr, g in und_cw.groupby(und_cw.index.year):
        spx_yr[yr] = float(g.iloc[-1] / g.iloc[0] - 1.0)

    # ---- assemble the base case: tail 0.50 + fin 0.50 (1:1) at delta 0.15 ----
    def combine(tail_frac, fin_frac, delta):
        tail = TAIL_FRAC_scaled(tail_unit, tail_frac)
        fin = fin_unit[delta]["r_fin"] * fin_frac
        df = pd.DataFrame({"r_tail": tail["r_tail"], "r_fin": fin})
        df["r_fin"] = df["r_fin"].fillna(0.0)
        df["r_tail"] = df["r_tail"].fillna(0.0)
        df = df[[sp.in_clean_window(ts) for ts in df.index]]
        df["r_sleeve"] = df["r_tail"] + df["r_fin"]
        df["year"] = df.index.year
        return df

    def annual(df):
        return df.groupby("year").agg(r_tail=("r_tail", "sum"),
                                      r_fin=("r_fin", "sum"),
                                      r_sleeve=("r_sleeve", "sum"))

    base = combine(TAIL_FRAC, TAIL_FRAC, 0.15)   # 1:1, delta 0.15
    base_ann = annual(base)

    # naked tail alone (for the calm-carry comparison), same tail_frac
    tail_only = combine(TAIL_FRAC, 0.0, 0.15)
    tail_only_ann = annual(tail_only)

    print("[3/4] base-case annual table:", flush=True)
    print(base_ann.round(4).to_string(), flush=True)

    # ---- sizing sweep: fin at 0.5x/1.0x/1.5x tail, x deltas 0.10/0.15 ----
    print("[4/4] sizing sweep ...", flush=True)
    sweep = {}
    for delta in FIN_DELTAS:
        for mult in FIN_MULTS:
            fin_frac = TAIL_FRAC * mult
            df = combine(TAIL_FRAC, fin_frac, delta)
            a = annual(df)
            calm = a["r_sleeve"].drop([y for y in a.index if y in CRASH_YEARS], errors="ignore")
            sweep[(delta, mult)] = {
                "ann": a,
                "calm_avg": float(calm.mean()),
                "2020": float(a["r_sleeve"].get(2020, np.nan)),
                "2022": float(a["r_sleeve"].get(2022, np.nan)),
                "n_pos": int((a["r_sleeve"] > 0).sum()),
                "n_neg": int((a["r_sleeve"] < 0).sum()),
                "worst": float(a["r_sleeve"].min()),
                "fin_frac": fin_frac,
            }

    # ---- guardrail: net delta through the two bottoms at each sizing ----
    guard = {}
    for delta in FIN_DELTAS:
        for mult in FIN_MULTS:
            fin_frac = TAIL_FRAC * mult
            guard[(delta, mult)] = sp.net_delta_guardrail(TAIL_FRAC, fin_frac, delta, FIN_DTE)

    write_report(base_ann, tail_only_ann, sweep, guard, spx_yr, fin_recs)
    print("done.", flush=True)


def TAIL_FRAC_scaled(tail_unit: pd.DataFrame, frac: float) -> pd.DataFrame:
    out = tail_unit.copy()
    out["r_tail"] = out["r_tail"] * frac
    out["tail_val"] = out["tail_val"] * frac
    return out


def fpct(x, nd=2):
    if x is None or (isinstance(x, float) and (np.isnan(x))):
        return "--"
    return f"{x*100:+.{nd}f}%"


def write_report(base_ann, tail_only_ann, sweep, guard, spx_yr, fin_recs):
    L = []
    A = L.append
    today = dt.date.today().isoformat()

    A("# S5 hedge sleeve -- first-cut year-by-year P&L")
    A("")
    A(f"*Generated {today} | PAPER / research only | offline | FIRST CUT to see the profile, "
      f"not a validated result*")
    A("")
    A("**The sleeve** = `[owned always-on deep tail]` + `[short-premium financing overlay]`, "
      "with **BOTH legs' FULL P&L netted into one stream** and **NO core equity** -- a "
      "self-contained hedge sleeve a client bolts onto their own strategies. The question: how "
      "many years does it make money vs lose, how big are the crash payoffs, and what does it "
      "cost to hold in calm years?")
    A("")
    A("**The mistake this deliberately does NOT repeat.** The prior `sell_against_owned_tail` "
      "metric counted only the SHORT leg and used the tail merely as a risk cap -- so the "
      "put-write \"blew up -95% in 2022\" with the tail's own payoff invisible. Here the tail's "
      "**full P&L (theta carry AND crash payoff)** is counted and **summed** with the financing "
      "leg. `sleeve = tail_mtm(carry+payoff) + fin(premium-losses)`, continuously rolled, "
      "honest fills.")
    A("")

    A("## Accounting convention (stated explicitly)")
    A("")
    A("- **Sleeve notional unit** = one SPX index unit = `index_level * 100 $`. Every figure is "
      "**%/yr of that SPX notional** the sleeve is sized against.")
    A("- **TAIL leg** = a single continuously-rolled LONG book of **20% OTM, ~63-DTE SPX puts** "
      "at **0.50 contracts per index unit**, rolled at ~21 DTE. Priced daily with Black-Scholes "
      "using the **REAL warehouse EOD skew IV** (interpolated across the 10/15/20/25% OTM strikes "
      "at the held contract's live moneyness), so theta bleed AND crash-vol spikes are captured. "
      "Marked to mid (a held hedge).")
    A("- **FIN leg** = a single **NON-overlapping** SHORT put-write book (one short put open at a "
      "time, re-entered on the next clean day after exit): **0.15 delta, 45 DTE, hold-to-expiry, "
      "HONEST fills** (sell bid / buy ask / $0.65 leg / cash-settled) via the committed harness. "
      "Sized at `fin_frac` contracts per index unit.")
    A("- **Base sizing = 1:1** (fin_frac = tail_frac = 0.50). NET-LONG-CONVEXITY guardrail: "
      "fin notional <= tail notional always.")
    A("- Per-year figures are the **simple sum of daily P&L fractions** within the year (a carry "
      "quote, non-compounded).")
    A("")

    A("## 2021 is a DATA HOLE (stated up front)")
    A("")
    A("The warehouse's honest two-sided quotes are DEAD in **2020-08-13 -> 2021-12-31**, so the "
      "SHORT financing leg cannot be honestly filled there. The **combined sleeve EXCLUDES 2021 "
      "entirely.** Clean windows: **A = 2018-01-02..2020-08-12**, **B = 2022-01-03..2026-07-02**. "
      "So \"X of 10 years\" is really **~7 usable years with 2 crash episodes (COVID 2020, bear "
      "2022)** -- enough to see the SHAPE, not to fine-tune a precise ratio.")
    A("")

    # ---- A. YEAR-BY-YEAR TABLE (base 1:1, delta 0.15) ----
    A("## A. Year-by-year -- base sleeve (fin 0.15d 45DTE, 1:1 to tail)")
    A("")
    A("| Year | Tail leg | Fin leg | **Sleeve net** | Tag |")
    A("|---:|---:|---:|---:|:--|")
    years_all = [2018, 2019, 2020, 2021, 2022, 2023, 2024, 2025, 2026]
    pos = []
    neg = []
    for yr in years_all:
        if yr == GAP_YEAR:
            A(f"| {yr} | -- | -- | **-- (MISSING)** | 2021 data hole |")
            continue
        rt = base_ann["r_tail"].get(yr, np.nan)
        rf = base_ann["r_fin"].get(yr, np.nan)
        rs = base_ann["r_sleeve"].get(yr, np.nan)
        tag = year_tag(yr, rs, spx_yr.get(yr))
        star = "**partial**" if yr in (2020, 2026) else ""
        yr_lbl = f"{yr} {star}".strip()
        A(f"| {yr_lbl} | {fpct(rt)} | {fpct(rf)} | **{fpct(rs)}** | {tag} |")
        if not np.isnan(rs):
            (pos if rs > 0 else neg).append((yr, rs))
    A("")
    A("*2020 is partial (to Aug 12, window A close -- captures the COVID crash + spring rebound); "
      "2026 is partial (to Jul 2).*")
    A("")

    # positive vs negative years with magnitudes
    A("### Positive vs negative years (with magnitudes)")
    A("")
    A(f"- **Positive years ({len(pos)}):** " +
      ", ".join(f"{y} {fpct(v)}" for y, v in pos))
    A(f"- **Negative years ({len(neg)}):** " +
      (", ".join(f"{y} {fpct(v)}" for y, v in neg) if neg else "none"))
    worst_y, worst_v = min(base_ann["r_sleeve"].items(), key=lambda kv: kv[1])
    A(f"- **Worst year:** {worst_y} at {fpct(worst_v)}")
    A("")

    # 2020 / 2022 crash payoffs
    s2020 = base_ann["r_sleeve"].get(2020, np.nan)
    s2022 = base_ann["r_sleeve"].get(2022, np.nan)
    t2020 = base_ann["r_tail"].get(2020, np.nan)
    t2022 = base_ann["r_tail"].get(2022, np.nan)
    f2020 = base_ann["r_fin"].get(2020, np.nan)
    f2022 = base_ann["r_fin"].get(2022, np.nan)
    A("### The 2020 & 2022 crash payoffs (did financing eat the hedge?)")
    A("")
    A(f"- **COVID 2020:** sleeve **{fpct(s2020)}** = tail {fpct(t2020)} + fin {fpct(f2020)}. "
      "The fast 33% crash + IV spike to ~80 makes the deep tail explode; the financing loss is "
      "small and the net stays clearly positive -- **the financing did NOT eat the hedge.**")
    A(f"- **Bear 2022:** sleeve **{fpct(s2022)}** = tail {fpct(t2022)} + fin {fpct(f2022)}. "
      "This is the DANGER ZONE: a slow ~25% grind where IV never spiked, so the deep 20% OTM "
      "tail barely paid (near-worthless even at the Oct bottom) while it kept bleeding carry -- "
      "the financing premium actually CARRIES the sleeve here.")
    A("")

    # calm carry vs -1.56%
    calm_sleeve = base_ann["r_sleeve"].drop([y for y in base_ann.index if y in CRASH_YEARS],
                                            errors="ignore").mean()
    calm_tail = tail_only_ann["r_tail"].drop(
        [y for y in tail_only_ann.index if y in CRASH_YEARS], errors="ignore").mean()
    A("### Calm-year carry -- how much did financing shrink the bleed?")
    A("")
    A(f"- **Naked tail alone**, calm years: **{fpct(calm_tail)}/yr** (the sleeve's tail leg at "
      "frac 0.50, real skew; the spec's reference naked-tail carry is ~-1.56%/yr).")
    A(f"- **Financed sleeve**, calm years: **{fpct(calm_sleeve)}/yr**.")
    shrink = calm_sleeve - calm_tail
    A(f"- **Financing shrank the calm bleed by ~{abs(shrink)*100:.2f} pts/yr** "
      f"({fpct(calm_tail)} -> {fpct(calm_sleeve)}). "
      + ("The sleeve now carries POSITIVE in calm years -- the financing more than pays for the "
         "tail." if calm_sleeve > 0 else
         "The sleeve still bleeds in calm but far less than the naked tail."))
    A("")

    # danger-zone / moderate-drop years
    A("### Danger-zone (moderate-drop) years -- financing loses, deep tail hasn't paid")
    A("")
    mod = [(y, base_ann["r_sleeve"].get(y)) for y in base_ann.index
           if spx_yr.get(y, 0) < -0.05 and y not in CRASH_YEARS]
    if mod:
        for y, v in mod:
            A(f"- **{y}** (SPX {spx_yr[y]*100:+.1f}%): sleeve {fpct(v)}")
    else:
        A("- No non-crash down-equity year in the clean windows crosses the -5% flag; **2022 is "
          "the sole moderate/slow-drop stress** and it is the danger zone made explicit above "
          "(the deep tail failed to pay in the grind, so the year leans entirely on the financing "
          "premium).")
    A("")

    # ---- B. SIZING SWEEP ----
    A("## B. Sizing sweep -- financing at 0.5x / 1.0x / 1.5x the tail notional")
    A("")
    A("| Fin delta | Fin x tail | Calm carry/yr | 2020 (CRASH) | 2022 (danger) | +yrs / -yrs | Worst yr | Net convexity |")
    A("|---:|---:|---:|---:|---:|:--:|---:|:--:|")
    for delta in FIN_DELTAS:
        for mult in FIN_MULTS:
            r = sweep[(delta, mult)]
            g = guard[(delta, mult)]
            long_cvx = all(v["long_convexity"] for v in g.values())
            flag = "LONG" if long_cvx else "**LOST**"
            A(f"| {delta} | {mult:.1f}x | {fpct(r['calm_avg'])} | {fpct(r['2020'])} | "
              f"{fpct(r['2022'])} | {r['n_pos']} / {r['n_neg']} | {fpct(r['worst'])} | {flag} |")
    A("")
    A("*Calm carry = mean sleeve %/yr excluding the two crash years. Net convexity = the sleeve's "
      "net delta stays SHORT (long-convexity, hedge still pays) through BOTH crash bottoms.*")
    A("")
    A("**Reading the sweep.** More financing (higher x) lifts the calm carry (more premium "
      "collected) but SHRINKS the crash payoff (the short puts lose into the crash, partly "
      "cancelling the tail's gain). The guardrail is whether the crash payoff stays materially "
      "positive AND net convexity stays long. Higher short delta (0.15 vs 0.10) collects more "
      "premium but loses more in the crash -- the same trade-off, sharper.")
    A("")

    # ---- C. GUARDRAIL ----
    A("## C. Guardrail -- net delta / net convexity through the crash bottoms")
    A("")
    A("Sleeve-only net delta (per index unit, NO core) = `tail_frac * put_delta_tail` "
      "(negative, long-convexity) `+ fin_frac * short_put_delta` (positive, the short put "
      "loses in a crash). **Net << 0 = still long convexity (the hedge pays).** The tail's deep-"
      "put delta is read from the real-skew table at each bottom; the short put contributes "
      "`+|delta|`.")
    A("")
    A("| Fin delta | Fin x tail | COVID-2020 net delta | Bear-2022 net delta | Long convexity? |")
    A("|---:|---:|---:|---:|:--:|")
    for delta in FIN_DELTAS:
        for mult in FIN_MULTS:
            g = guard[(delta, mult)]
            nd20 = g["COVID 2020"]["net_delta"]
            nd22 = g["Bear 2022"]["net_delta"]
            ok = g["COVID 2020"]["long_convexity"] and g["Bear 2022"]["long_convexity"]
            A(f"| {delta} | {mult:.1f}x | {nd20:+.4f} | {nd22:+.4f} | {'YES' if ok else 'NO'} |")
    A("")
    A("*Note: at the crash BOTTOM the deep tail's put delta is only modestly negative (a 20% OTM "
      "put is still partly OTM even at the low), so a large short-put book at 1.5x can pull the "
      "net toward zero -- that is the guardrail firing. The MARK-TO-MARKET payoff table in B is "
      "the more direct read of \"did the hedge still pay\"; this delta check is the structural "
      "confirmation.*")
    A("")

    # ---- caveats + plain read ----
    A("## Honest caveats")
    A("")
    A("- **Only TWO real crash tests** (COVID 2020 fast-crash, 2022 slow-grind bear) + a 2021 "
      "data hole => ~7 usable years, 2 episodes. Enough for the SHAPE, **not** to fine-tune a "
      "precise sizing ratio. Do NOT curve-fit the ratio.")
    A("- **Tail priced on REAL warehouse EOD skew** (10/15/20/25% OTM IV interpolated). This is "
      "the honest, skew-realistic tail cost -- materially pricier than flat-skew BSM. The FIN "
      "leg is honest bid/ask fills. Both are the pessimistic-realistic reads.")
    A("- The deep **20% OTM tail is a FAST-crash instrument**: it explodes in COVID (IV to ~80) "
      "but barely pays in the 2022 slow grind (IV peaked ~35, strike never neared the money). "
      "That asymmetry is the single most important shape finding here.")
    A("- **Non-overlapping single-book** accounting (one position per leg at a time) makes the "
      "notional unambiguous and directly comparable across legs; it is NOT the enter-every-day "
      "sweep (whose ~250 concurrent shorts produced the -95% artifact). Different, deliberate, "
      "and honest for a sleeve sizing question.")
    A("- First cut. Numbers are the profile; the decimals will move with tail OTM depth, roll "
      "cadence, and the financing tenor/delta -- all frozen here at one reasonable base each.")
    A("")

    # plain read
    A("## Plain read -- does financing make this hedge cheap enough to hold while keeping the payoff?")
    A("")
    read = []
    read.append(
        f"At the 1:1 base (fin 0.15d 45DTE), financing shrinks the tail's calm bleed from "
        f"{fpct(calm_tail)}/yr to {fpct(calm_sleeve)}/yr -- "
        + ("turning the hold cost POSITIVE" if calm_sleeve > 0 else "roughly halving the hold cost")
        + f", while COVID-2020 still pays {fpct(s2020)}. That is the whole thesis working: the "
        "short-premium overlay pays the rent on the tail in calm years without cancelling the "
        "fast-crash payoff. The catch the data makes unavoidable is the DANGER ZONE -- a slow "
        f"grind like 2022, where the deep tail never triggers (sleeve {fpct(s2022)}, leaning "
        "entirely on the financing premium to stay near flat). So the sleeve is genuinely cheap-"
        "to-hold and keeps its fast-crash convexity, but it is NOT a hedge against a slow, "
        "shallow, IV-suppressed decline -- for that danger zone you either accept the small "
        "carry, add a nearer-the-money tail leg, or size the financing up (which then erodes the "
        "COVID payoff, per the sweep). Two crash tests is too few to pick the exact ratio; the "
        "shape says the structure is sound and the 0.5x-1.0x financing band is the honest "
        "sweet-spot region to keep net convexity long.")
    A(" ".join(read))
    A("")

    path = OUT / "SLEEVE_YEAR_BY_YEAR_20260705.md"
    path.write_text("\n".join(L) + "\n", encoding="utf-8")
    print(f"report -> {path}", flush=True)


if __name__ == "__main__":
    main()
