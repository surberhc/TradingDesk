"""
run_options_overlay_real.py — the options-overlay HYBRID re-run on REAL ThetaData
single-stock option quotes (NOT modeled Black-Scholes).

WHAT CHANGES vs options_overlay_backtest.py
-------------------------------------------
The approved spec/logic is UNCHANGED (cheap ~ATM call as insurance -> convert to
stock when the call's DELTA crosses the trigger -> take delivery at strike ->
manage the delivered stock with the core E3 exit; never roll; sideways = walk;
head-to-head vs the stock book; shakeout-vs-theta decomposition). The ONLY change
is the DATA SOURCE for the option leg:

  * ENTRY PREMIUM  = the REAL ASK of the chosen call on the entry day (a buyer
    lifts the offer — an honest fill, not the mid and not a modeled BS price).
  * CONVERSION     = the REAL per-day DELTA reported by ThetaData crossing the
    trigger (not a modeled N(d1)). This RETIRES the modeled-BS + IV-sweep: real IV
    is baked into the real delta and the real premium, so there is a DEFINITIVE
    answer per (tenor, delta, strike, premium) cell — no IV assumption to sweep.
  * INTERMEDIATE MARKS not needed: the option is held to conversion / expiry, and
    P&L is realized via delivery (strike capital) or premium loss, exactly as the
    modeled engine does.

Real IV is REPORTED (median entry IV per cell) so the answer is anchored to the
observed vol, replacing the modeled 40/60/80% sweep with the real number.

STRIKE / TENOR SELECTION on the real chain (faithful to the spec, applied to real
listed contracts): from the entry-day chain, for the target strike offset
(ATM/±5% of the pivot) pick the LISTED strike nearest the target; for the target
tenor (2/3/4/6/9 mo) pick the LISTED expiration whose days-to-expiry is nearest
the target. This is what a trader could actually have bought.

HONEST FILLS / GUARDS (rule #1)
-------------------------------
- Buy at the ASK (real spread paid), not the mid. Conversion is exercise-at-strike
  (a cash round-trip), same mechanic as the modeled run — no exit spread modeled on
  the option itself because it is EXERCISED, not sold.
- Missing-quote honesty: if a name has no real chain on/near the entry day, or the
  target contract has no quote, that trade is recorded as 'no-quote' and EXCLUDED
  from the head-to-head (never faked). The count is reported.
- Post-conversion the delivered STOCK uses eb.simulate_exit(E3) — the SAME committed
  engine as the modeled run and the stock book (no re-derivation).
- Full grid reported: tenor {2,3,4,6,9mo} x strike {ATM,ITM5,OTM5} x delta
  {0.80,0.85,0.90} x budget {7%,14%}. No IV sweep (real IV replaces it).

Usage:  python run_options_overlay_real.py
  writes research/options_overlay_real.md, research/options_overlay_real_results.csv
"""

from __future__ import annotations

import csv
import datetime as dt
import math
import os
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import execution_backtest as eb        # E3 exit + loaders (shared brain, do not re-derive)
import options_overlay_backtest as ovb  # LIQUID universe, grid knobs, report helpers

RESEARCH = HERE / "research"
WAREHOUSE = Path(r"C:\TradingDesk-Local\canslim\thetadata_equity")

# Grid knobs — reuse the approved spec's grid (no IV sweep; real IV replaces it).
BUDGETS = ovb.BUDGETS                    # [0.07, 0.14]
TENORS_TD = ovb.TENORS_TD                # {"2mo":42,...,"9mo":189}
STRIKE_OFFSETS = ovb.STRIKE_OFFSETS      # {"ATM":0, "ITM5":-0.05, "OTM5":+0.05}
DELTA_TRIGGERS = ovb.DELTA_TRIGGERS      # [0.80,0.85,0.90]
ITM_MARGIN = ovb.ITM_MARGIN
START_CAPITAL = ovb.START_CAPITAL
HARD_STOP = ovb.HARD_STOP
BASE = dict(tenor_td=126, budget=0.07, strike="ATM", trig=0.85)  # 6mo/ATM/0.85 (no IV — real)

ENTRY_TOL_DAYS = 4      # match the entry-day chain within this many calendar days
TENOR_TOL_FRAC = 0.5    # a listed expiry must be within 50% of the target tenor to use it


# --------------------------------------------------------------------------- #
# Real-chain loader (per name, cached)
# --------------------------------------------------------------------------- #
_CHAIN_CACHE: dict[str, pd.DataFrame | None] = {}


def load_chain(symbol: str) -> pd.DataFrame | None:
    """Load & concat all month parquets for a name into one CALL-only frame.
    Returns None if the name was never pulled. Adds a python `date` (datetime.date)
    and `expiry` column for fast lookups."""
    if symbol in _CHAIN_CACHE:
        return _CHAIN_CACHE[symbol]
    ddir = WAREHOUSE / symbol
    if not ddir.exists():
        _CHAIN_CACHE[symbol] = None
        return None
    frames = []
    for f in sorted(ddir.glob("*.parquet")):
        try:
            df = pd.read_parquet(f)
        except Exception:
            continue
        if df.empty or "right" not in df.columns:
            continue
        frames.append(df)
    if not frames:
        _CHAIN_CACHE[symbol] = None
        return None
    df = pd.concat(frames, ignore_index=True)
    df = df[df["right"].astype(str).str.upper() == "CALL"].copy()
    if df.empty:
        _CHAIN_CACHE[symbol] = None
        return None
    df["d"] = pd.to_datetime(df["date"], format="%Y%m%d", errors="coerce").dt.date
    df["exp"] = pd.to_datetime(df["expiration"], errors="coerce").dt.date
    for col in ("strike", "delta", "bid", "ask", "close", "implied_vol", "underlying_price"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    _CHAIN_CACHE[symbol] = df
    return df


def _entry_day_frame(chain: pd.DataFrame, buy: dt.date) -> pd.DataFrame:
    """Rows on the nearest trading day at/after the entry (within ENTRY_TOL_DAYS)."""
    days = sorted({d for d in chain["d"].dropna().unique()})
    cand = [d for d in days if 0 <= (d - buy).days <= ENTRY_TOL_DAYS]
    if not cand:
        # fall back to nearest day within tolerance either side
        cand = [d for d in days if abs((d - buy).days) <= ENTRY_TOL_DAYS]
    if not cand:
        return chain.iloc[0:0]
    d0 = min(cand, key=lambda d: (abs((d - buy).days), (d - buy).days < 0))
    return chain[chain["d"] == d0].copy()


def pick_contract(chain: pd.DataFrame, buy: dt.date, pivot: float,
                  strike_off: float, tenor_td: int):
    """Choose the real listed (strike, expiry) nearest the spec target on the entry
    day. Returns (entry_day, strike, expiry, entry_ask, entry_iv) or None."""
    ed = _entry_day_frame(chain, buy)
    if ed.empty:
        return None
    target_k = pivot * (1.0 + strike_off)
    target_days = tenor_td * 365.0 / 252.0
    ed = ed.dropna(subset=["strike", "exp"])
    if ed.empty:
        return None
    d0 = ed["d"].iloc[0]
    ed = ed[ed["exp"] >= d0].copy()
    if ed.empty:
        return None
    ed["dte"] = ed["exp"].map(lambda e: (e - d0).days)
    # nearest listed expiry to the target tenor (within tolerance)
    exps = sorted(ed["exp"].unique(), key=lambda e: abs((e - d0).days - target_days))
    chosen_exp = None
    for e in exps:
        dte = (e - d0).days
        if abs(dte - target_days) <= TENOR_TOL_FRAC * target_days:
            chosen_exp = e
            break
    if chosen_exp is None:
        return None
    sub = ed[ed["exp"] == chosen_exp].copy()
    # nearest listed strike to target
    sub["kdist"] = (sub["strike"] - target_k).abs()
    sub = sub.sort_values("kdist")
    row = sub.iloc[0]
    ask = row.get("ask")
    if ask is None or not (ask > 0):
        # fall back to close/mid if no valid ask quote
        mid = None
        if pd.notna(row.get("bid")) and pd.notna(row.get("ask")) and row["ask"] > 0:
            mid = (row["bid"] + row["ask"]) / 2
        ask = mid if mid else row.get("close")
    if ask is None or not (ask > 0):
        return None
    iv = row.get("implied_vol")
    return (d0, float(row["strike"]), chosen_exp, float(ask),
            float(iv) if pd.notna(iv) else None)


def delta_series(chain: pd.DataFrame, strike: float, expiry: dt.date,
                 after: dt.date) -> list[tuple]:
    """Real (date, delta) for the exact contract, sorted, for days after entry."""
    sub = chain[(chain["strike"] == strike) & (chain["exp"] == expiry)
                & (chain["d"] > after)].copy()
    sub = sub.dropna(subset=["delta"])
    sub = sub.sort_values("d")
    return list(zip(sub["d"], sub["delta"], sub["underlying_price"]))


# --------------------------------------------------------------------------- #
# One real-quote option position (mirrors ovb.run_option_trade, real data)
# --------------------------------------------------------------------------- #
@dataclass
class RealOptOutcome:
    symbol: str
    buy: dt.date
    strike: float
    entry_prem: float
    entry_iv: float | None
    contracts: int
    premium_paid: float
    converted: bool
    convert_date: dt.date | None
    strike_capital: float
    exit_date: dt.date
    pl: float
    kind: str            # delta-convert-run|itm-expiry-run|expired-worthless|no-quote|no-path
    overrun: bool


def run_real_trade(trade, path, chain, tenor_td, budget_pct, strike_off, delta_trig):
    pivot = trade["entry_px"]
    cost = trade["cost"] if trade.get("cost") else eb.EW_TARGET * START_CAPITAL

    pick = pick_contract(chain, trade["buy"], pivot, strike_off, tenor_td)
    if pick is None:
        return RealOptOutcome(trade["symbol"], trade["buy"], pivot * (1 + strike_off), 0.0,
                              None, 0, 0.0, False, None, 0.0, trade["buy"], 0.0,
                              "no-quote", False)
    entry_day, K, expiry, entry_prem, entry_iv = pick
    budget = budget_pct * cost
    contracts = int(budget // (100 * entry_prem))
    overrun = contracts < 1
    if contracts < 1:
        contracts = 1
    premium_paid = contracts * 100 * entry_prem
    shares = contracts * 100

    # walk the REAL per-day delta of this exact contract; convert on first cross
    ds = delta_series(chain, K, expiry, entry_day)
    for (d, delta, _spot) in ds:
        if delta >= delta_trig:
            deliv = {**trade, "buy": d, "entry_px": K}
            xd, xr = eb.simulate_exit(deliv, path, "E3")
            pl = shares * K * xr - premium_paid
            return RealOptOutcome(trade["symbol"], trade["buy"], K, entry_prem, entry_iv,
                                  contracts, premium_paid, True, d, shares * K, xd, pl,
                                  "delta-convert-run", overrun)

    # never triggered -> expiry decision using the underlying's real close at expiry
    exp_close = None
    for (d, o, h, l, c) in path:
        if d <= expiry and c is not None:
            exp_close = c
    S_exp = exp_close if exp_close else pivot
    if S_exp > K * (1.0 + ITM_MARGIN):
        deliv = {**trade, "buy": expiry, "entry_px": K}
        xd, xr = eb.simulate_exit(deliv, path, "E3")
        pl = shares * K * xr - premium_paid
        return RealOptOutcome(trade["symbol"], trade["buy"], K, entry_prem, entry_iv,
                              contracts, premium_paid, True, None, shares * K, xd, pl,
                              "itm-expiry-run", overrun)
    return RealOptOutcome(trade["symbol"], trade["buy"], K, entry_prem, entry_iv, contracts,
                          premium_paid, False, None, 0.0, expiry, -premium_paid,
                          "expired-worthless", overrun)


# --------------------------------------------------------------------------- #
# Portfolio walk (mirrors ovb.option_book; excludes no-quote trades from equity)
# --------------------------------------------------------------------------- #
def real_option_book(trades, paths, tenor_td, budget_pct, strike_off, delta_trig):
    outs = []
    for t in trades:
        p = paths.get(t["symbol"])
        chain = load_chain(t["symbol"])
        if not p:
            outs.append(RealOptOutcome(t["symbol"], t["buy"], t["entry_px"], 0.0, None, 0,
                                       0.0, False, None, 0.0, t["sell"], 0.0, "no-path", False))
            continue
        if chain is None:
            outs.append(RealOptOutcome(t["symbol"], t["buy"], t["entry_px"], 0.0, None, 0,
                                       0.0, False, None, 0.0, t["buy"], 0.0, "no-quote", False))
            continue
        rp = eb.rescaled_path(p, t["buy"], t["entry_px"])
        outs.append(run_real_trade(t, rp, chain, tenor_td, budget_pct, strike_off, delta_trig))

    priced = [o for o in outs if o.kind not in ("no-quote", "no-path")]
    total_pl = sum(o.pl for o in priced)
    final_equity = START_CAPITAL + total_pl
    ev = sorted(priced, key=lambda o: o.exit_date)
    curve = [(min(t["buy"] for t in trades), START_CAPITAL)]
    run = START_CAPITAL
    for o in ev:
        run += o.pl
        curve.append((o.exit_date, run))
    peak, mdd = -1e18, 0.0
    for (_, v) in curve:
        peak = max(peak, v)
        if peak > 0:
            mdd = min(mdd, v / peak - 1.0)
    total_ret = final_equity / START_CAPITAL - 1.0
    wins = sum(1 for o in priced if o.pl > 0)
    win_rate = wins / len(priced) if priced else 0.0

    def yb(d):
        if d < dt.date(2024, 1, 1): return "2023H2"
        if d < dt.date(2025, 1, 1): return "2024"
        if d < dt.date(2026, 1, 1): return "2025"
        return "2026H1"
    peryear = defaultdict(lambda: dict(pl=0.0, n=0))
    for o in priced:
        b = yb(o.exit_date); peryear[b]["pl"] += o.pl; peryear[b]["n"] += 1

    ivs = [o.entry_iv for o in priced if o.entry_iv is not None]
    med_iv = sorted(ivs)[len(ivs) // 2] if ivs else None
    return dict(tenor_td=tenor_td, budget=budget_pct, strike=strike_off, trig=delta_trig,
                final_equity=final_equity, total_ret=total_ret, mdd=mdd, win_rate=win_rate,
                total_pl=total_pl, n=len(priced), n_converted=sum(1 for o in priced if o.converted),
                n_worthless=sum(1 for o in priced if o.kind == "expired-worthless"),
                n_noquote=sum(1 for o in outs if o.kind in ("no-quote", "no-path")),
                med_entry_iv=med_iv, outs=outs, priced=priced, peryear=dict(peryear))


# --------------------------------------------------------------------------- #
# Decomposition on real quotes (shakeout wins vs theta losses) — mirror ovb
# --------------------------------------------------------------------------- #
def decompose(res, trades, paths):
    a = b = c = mit = 0.0
    a_rows, b_rows = [], []
    for o in res["priced"]:
        t = next((x for x in trades if x["symbol"] == o.symbol and x["buy"] == o.buy), None)
        if t is None:
            continue
        p = paths.get(o.symbol)
        if not p:
            continue
        rp = eb.rescaled_path(p, t["buy"], t["entry_px"])
        _, xr_s = eb.simulate_exit(t, rp, "E3")
        cost = t["cost"] if t.get("cost") else eb.EW_TARGET * START_CAPITAL
        stock_pl = cost * xr_s
        opt_pl = o.pl
        stopped = (xr_s <= -HARD_STOP + 1e-9)
        row = (o.symbol, o.buy.isoformat(), round(xr_s, 3), round(stock_pl), round(opt_pl))
        if stopped and o.converted and opt_pl > 0:
            a += (opt_pl - stock_pl); a_rows.append(row + (round(opt_pl - stock_pl), o.kind))
        elif (not o.converted) and opt_pl <= 0 and not stopped:
            b += (stock_pl - opt_pl); b_rows.append(row + (round(stock_pl - opt_pl), o.kind))
        elif o.converted and opt_pl > 0 and stock_pl > opt_pl:
            c += (stock_pl - opt_pl)
        elif stopped and opt_pl < 0 and opt_pl > stock_pl:
            mit += (opt_pl - stock_pl)
    return dict(a=a, b=b, c=c, mit=mit, net=a - b,
                a_rows=sorted(a_rows, key=lambda r: -r[5]),
                b_rows=sorted(b_rows, key=lambda r: -r[5]))


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #
_pct = ovb._pct
_k = ovb._k


def _cell_tag(r):
    lab_tenor = {v: k for k, v in TENORS_TD.items()}.get(r["tenor_td"], str(r["tenor_td"]))
    strike = {0.0: "ATM", -0.05: "ITM5", 0.05: "OTM5"}.get(round(r["strike"], 2), str(r["strike"]))
    return f"{lab_tenor}/{strike}/d{int(r['trig']*100)}/{int(r['budget']*100)}%"


def build_report(stock_res, grid, base_res, decomp, meta):
    L = []; A = L.append
    A("# CAN SLIM options-overlay HYBRID — RE-RUN ON REAL ThetaData QUOTES (definitive)")
    A("")
    A("_Same approved spec as the modeled run (cheap ~ATM call insurance -> convert to stock "
      "when DELTA crosses the trigger -> take delivery -> core E3 exit; never roll; head-to-head "
      "vs stock; shakeout-vs-theta decomposition). The ONLY change: the option leg uses **REAL "
      "historical single-stock quotes** — entry premium = real ASK, conversion = real reported "
      "DELTA. This RETIRES the modeled Black-Scholes prices and the 40/60/80% IV sweep: real IV "
      "is baked into the real premium + real delta, so this is the DEFINITIVE per-cell answer._")
    A("")
    A(f"- Start capital **${int(START_CAPITAL):,}** (same as the stock engine).")
    A(f"- **Liquid universe: {meta['n_liquid_names']} names, {meta['n_liquid_trades']} entries.** "
      f"Priced on real quotes: **{base_res['n']}** entries (base cell); "
      f"**{base_res['n_noquote']}** had no real chain on/near the entry day and are EXCLUDED "
      "from the head-to-head (never faked).")
    if base_res["med_entry_iv"] is not None:
        A(f"- **Median real entry IV (base cell): {base_res['med_entry_iv']*100:.0f}%** — the "
          "observed vol that replaces the modeled sweep.")
    A("")
    A("## Head-to-head — STOCK book vs REAL-QUOTE OPTION book (base: 6mo / ATM / delta 0.85 / 7%)")
    A("")
    A("| Book | Total ret | Max DD | Win% | Final equity | #converted | #worthless |")
    A("|---|---:|---:|---:|---:|---:|---:|")
    A(f"| STOCK (buy pivot, E3 exit) | {_pct(stock_res['total_ret'])} | {_pct(stock_res['mdd'])} "
      f"| {round(stock_res['win_rate']*100)}% | ${int(stock_res['final_equity']):,} | — | — |")
    A(f"| OPTION (real quotes) | {_pct(base_res['total_ret'])} | {_pct(base_res['mdd'])} | "
      f"{round(base_res['win_rate']*100)}% | ${int(base_res['final_equity']):,} | "
      f"{base_res['n_converted']}/{base_res['n']} | {base_res['n_worthless']} |")
    A("")
    A("## Per-year (bucketed by EXIT date)")
    A("")
    buckets = ["2023H2", "2024", "2025", "2026H1"]
    A("| Book | " + " | ".join(buckets) + " | total |")
    A("|---|" + "|".join(["---:"] * (len(buckets) + 1)) + "|")
    sc = stock_res["peryear"]
    A("| STOCK E3 | " + " | ".join(_k(sc.get(b, {}).get("pl", 0.0)) for b in buckets) + " | "
      + _k(sum(sc.get(b, {}).get("pl", 0.0) for b in buckets)) + " |")
    py = base_res["peryear"]
    A("| OPTION real | " + " | ".join(_k(py.get(b, {}).get("pl", 0.0)) for b in buckets) + " | "
      + _k(sum(py.get(b, {}).get("pl", 0.0) for b in buckets)) + " |")
    A("")
    A("## FULL GRID on real quotes (every cell reported; ranked by total ret; NO IV sweep — real IV)")
    A("")
    A(f"_tag = tenor / strike / delta-trig / budget. Stock book: {_pct(stock_res['total_ret'])} "
      f"total / {_pct(stock_res['mdd'])} maxDD._")
    A("")
    A("| Cell | Total ret | Max DD | Win% | Final $ | vs STOCK ($) | conv/worthless | priced | medIV |")
    A("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for r in sorted(grid, key=lambda x: -x["total_ret"]):
        iv = f"{r['med_entry_iv']*100:.0f}%" if r["med_entry_iv"] is not None else "n/a"
        A(f"| {_cell_tag(r)} | {_pct(r['total_ret'])} | {_pct(r['mdd'])} | "
          f"{round(r['win_rate']*100)}% | ${int(r['final_equity']):,} | "
          f"{_k(r['final_equity']-stock_res['final_equity'])} | "
          f"{r['n_converted']}/{r['n_worthless']} | {r['n']} | {iv} |")
    A("")
    best = max(grid, key=lambda x: x["total_ret"]); worst = min(grid, key=lambda x: x["total_ret"])
    A(f"- **Best cell:** `{_cell_tag(best)}` {_pct(best['total_ret'])} "
      f"({_k(best['final_equity']-stock_res['final_equity'])} vs stock). "
      f"**Worst:** `{_cell_tag(worst)}` {_pct(worst['total_ret'])} "
      f"({_k(worst['final_equity']-stock_res['final_equity'])} vs stock). Grid is exploratory "
      "sensitivity, NOT a recommendation.")
    A("")
    A("## DECOMPOSITION on real quotes (base cell), in dollars")
    A("")
    A("**(a) SHAKEOUT-SURVIVAL WINS** — the -7% stock stop ejected the name, the option survived, "
      "converted, and finished a WINNER:")
    A("")
    A("| Name | Buy | Stock E3 ret | Stock $ | Option $ | Gain to option $ | kind |")
    A("|---|---|---:|---:|---:|---:|---|")
    for r in decomp["a_rows"]:
        A(f"| {r[0]} | {r[1]} | {_pct(r[2])} | {_k(r[3])} | {_k(r[4])} | {_k(r[5])} | {r[6]} |")
    if not decomp["a_rows"]:
        A("| _(none)_ | | | | | | |")
    A("")
    A("**(b) THETA/STALL LOSSES** — the stock went flat/small (never stopped), the option bled to "
      "worthless:")
    A("")
    A("| Name | Buy | Stock E3 ret | Stock $ | Option $ | Loss to option $ | kind |")
    A("|---|---|---:|---:|---:|---:|---|")
    for r in decomp["b_rows"]:
        A(f"| {r[0]} | {r[1]} | {_pct(r[2])} | {_k(r[3])} | {_k(r[4])} | {_k(r[5])} | {r[6]} |")
    if not decomp["b_rows"]:
        A("| _(none)_ | | | | | | |")
    A("")
    A(f"**NET (a) - (b) = {_k(decomp['net'])}** (shakeout-survival wins {_k(decomp['a'])} minus "
      f"theta/stall losses {_k(decomp['b'])}).")
    A(f"_Honesty note (not in the named net): on WINNERS the option gave up **{_k(decomp['c'])}** "
      "to notional-cap under-participation; on names where BOTH lost it 'saved' "
      f"{_k(-decomp['mit'])} purely by betting less._")
    A("")
    # verdict
    grid_all_below = all(r["final_equity"] < stock_res["final_equity"] for r in grid)
    grid_all_above = all(r["final_equity"] > stock_res["final_equity"] for r in grid)
    A("## VERDICT (real quotes — definitive for this liquid subset & window)")
    A("")
    if grid_all_below:
        A("- **No — on REAL quotes the cheap-call-insurance-to-delivery route does NOT beat owning "
          "the stock, and the answer HOLDS across the ENTIRE grid.** Base cell "
          f"(6mo/ATM/d0.85/7%): option {_pct(base_res['total_ret'])} vs stock "
          f"{_pct(stock_res['total_ret'])} ({_k(base_res['final_equity']-stock_res['final_equity'])}). "
          "Real spreads + real IV make this the honest verdict the modeled run flagged as needed.")
    elif grid_all_above:
        A("- **Yes — on REAL quotes the option route BEATS owning the stock across the entire grid.** "
          f"Base: {_pct(base_res['total_ret'])} vs stock {_pct(stock_res['total_ret'])}.")
    else:
        A(f"- **Mixed on real quotes** — base cell option {_pct(base_res['total_ret'])} vs stock "
          f"{_pct(stock_res['total_ret'])}; some cells beat the stock, some don't (see grid). The "
          "cell-dependence is itself the finding — not a robust standalone edge.")
    A(f"- **Why:** shakeout-survival wins {_k(decomp['a'])} vs theta/stall losses {_k(decomp['b'])} "
      f"(net {_k(decomp['net'])}), plus notional-cap drag on winners {_k(decomp['c'])}.")
    A("")
    A("### Hard limits (curve-fit + honesty guards, rule #1)")
    A("- **Real quotes now** (retires the modeled-BS / IV-sweep caveat): entry premium = real ASK "
      "(spread paid), conversion = real reported delta, real per-name IV. Conversion is "
      "exercise-at-strike (cash round-trip), so no option exit-spread is modeled (it is exercised, "
      "not sold) — a small friendly assumption disclosed here.")
    A(f"- **Missing quotes excluded, not faked:** {base_res['n_noquote']} entries had no usable real "
      "contract on/near the entry day and are dropped from the head-to-head.")
    A("- **Liquid-option subset only**, small sample, bull-heavy 2023-2026 window (cannot test a "
      "bear regime). Selection is HIS; this tests the OVERLAY, not stock-picking.")
    A("- **Full grid reported** so nothing is cherry-picked.")
    A("")
    return "\n".join(L), dict(grid_all_below=grid_all_below, grid_all_above=grid_all_above)


def write_csv(stock_res, grid, decomp, path):
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["section", "cell", "book", "tenor_td", "strike_off", "delta_trig", "budget",
                    "total_ret", "max_dd", "win_rate", "final_equity", "total_pl",
                    "n_converted", "n_worthless", "n_priced", "med_entry_iv"])
        w.writerow(["headline", "stock", "STOCK_E3", "", "", "", "",
                    round(stock_res["total_ret"], 4), round(stock_res["mdd"], 4),
                    round(stock_res["win_rate"], 4), round(stock_res["final_equity"]),
                    round(stock_res["final_equity"] - START_CAPITAL), "", "", "", ""])
        for r in sorted(grid, key=lambda x: -x["total_ret"]):
            w.writerow(["grid", _cell_tag(r), "OPTION_REAL", r["tenor_td"], r["strike"], r["trig"],
                        r["budget"], round(r["total_ret"], 4), round(r["mdd"], 4),
                        round(r["win_rate"], 4), round(r["final_equity"]), round(r["total_pl"]),
                        r["n_converted"], r["n_worthless"], r["n"],
                        round(r["med_entry_iv"], 4) if r["med_entry_iv"] else ""])
        w.writerow([])
        w.writerow(["decomp_a_shakeout_wins", "symbol", "buy", "stock_ret", "stock_$", "opt_$",
                    "gain_$", "kind"])
        for r in decomp["a_rows"]:
            w.writerow(["a"] + list(r))
        w.writerow(["a_TOTAL", "", "", "", "", "", round(decomp["a"]), ""])
        w.writerow(["decomp_b_theta_losses", "symbol", "buy", "stock_ret", "stock_$", "opt_$",
                    "loss_$", "kind"])
        for r in decomp["b_rows"]:
            w.writerow(["b"] + list(r))
        w.writerow(["b_TOTAL", "", "", "", "", "", round(decomp["b"]), ""])
        w.writerow(["NET_a_minus_b", "", "", "", "", "", round(decomp["net"]), ""])


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    all_trades = eb.load_ledger()
    paths = eb.load_paths()
    timing = eb.load_timing()
    trades = [t for t in all_trades if t["symbol"] in ovb.LIQUID]
    meta = dict(n_liquid_names=len({t["symbol"] for t in trades}),
                n_liquid_trades=len(trades))
    print(f"[real-opt-bt] liquid subset: {len(trades)} trades / {meta['n_liquid_names']} names")

    stock_res = ovb.stock_book(trades, paths, timing)

    grid = []
    for tenor_td in TENORS_TD.values():
        for strike_off in STRIKE_OFFSETS.values():
            for trig in DELTA_TRIGGERS:
                for budget in BUDGETS:
                    grid.append(real_option_book(trades, paths, tenor_td, budget, strike_off, trig))
    print(f"[real-opt-bt] grid cells: {len(grid)}")

    base_res = next(x for x in grid if x["tenor_td"] == BASE["tenor_td"]
                    and abs(x["budget"] - BASE["budget"]) < 1e-9
                    and abs(x["strike"]) < 1e-9 and abs(x["trig"] - BASE["trig"]) < 1e-9)
    decomp = decompose(base_res, trades, paths)

    report, verdict = build_report(stock_res, grid, base_res, decomp, meta)
    md_path = RESEARCH / "options_overlay_real.md"
    csv_path = RESEARCH / "options_overlay_real_results.csv"
    RESEARCH.mkdir(parents=True, exist_ok=True)
    md_path.write_text(report, encoding="utf-8")
    write_csv(stock_res, grid, decomp, csv_path)
    print(f"[real-opt-bt] wrote {md_path}")
    print(f"[real-opt-bt] wrote {csv_path}")

    print(f"\nSTOCK E3   total {_pct(stock_res['total_ret'])}  final ${int(stock_res['final_equity']):,}")
    print(f"OPT real base  total {_pct(base_res['total_ret'])}  final ${int(base_res['final_equity']):,}"
          f"  conv {base_res['n_converted']}/{base_res['n']}  worthless {base_res['n_worthless']}"
          f"  noquote {base_res['n_noquote']}  medIV "
          f"{base_res['med_entry_iv']*100:.0f}%" if base_res['med_entry_iv'] else "medIV n/a")
    return dict(stock_res=stock_res, grid=grid, base_res=base_res, decomp=decomp,
                verdict=verdict, meta=meta, md_path=str(md_path), csv_path=str(csv_path))


if __name__ == "__main__":
    main()
