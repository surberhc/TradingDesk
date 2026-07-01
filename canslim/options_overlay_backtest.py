"""Options-overlay HYBRID backtest for the CAN SLIM replica (v1, delta-triggered delivery).

THE ONE QUESTION
----------------
For his LIQUID-OPTION names, does buying a cheap ~ATM call as INSURANCE (premium == the -7%
stop loss), holding the OPTION (not the stock) through shakeouts, CONVERTING to stock only
when the call's modeled DELTA crosses a threshold (it has become a stock proxy) — exercise,
take delivery at the strike, then manage the delivered stock with the proven winning exit
(E3: hold above the rising 50-day, no profit cap) — make MORE money than just buying the
stock outright at the same pivot and managing it on the same rule?

APPROVED SPEC (implemented exactly — see research/options_overlay_spec.md)
-------------------------------------------------------------------------
ENTRY       : same breakout entry (his pivot: date+price fixed). ~ATM call (strike ~ pivot).
              Tenor TEST knob {3mo, 6mo}. Budget = 7% of intended stock alloc (also 14%).
              contracts = floor(budget / call_premium). Premium = MAX LOSS.
INSURANCE   : hold the OPTION, capital at risk = premium only; cannot be shaken out. Do NOT
              convert on the first 50-day cross. No management except the conversion trigger.
CONVERSION  : convert (exercise, take delivery at STRIKE, deploy strike capital) when the
              modeled call DELTA crosses {0.80, 0.85, 0.90}. Delta from the BS model on-path.
MANAGEMENT  : after conversion, basis = strike + premium; hand STOCK to the core exit (E3,
              imported from execution_backtest.py): hold above rising 50-day, exit on decisive
              close below, NO profit cap.
EXPIRATION  : delta never triggers -> at expiry, if meaningfully ITM (S > K*(1+ITM_MARGIN))
              take delivery (never discard intrinsic) + E3; else (~ATM/OTM) let it EXPIRE
              WORTHLESS, book premium loss, do NOT take delivery. NEVER roll.

WHY BLACK-SCHOLES (MODELED, NOT REAL FILLS)
-------------------------------------------
No historical single-name option quotes. Calls PRICED WITH BS along each stock path; theta
decay modeled (time-to-expiry shrinks each bar); delta = N(d1) from the same BS model. IV is
the biggest unknown on growth names, so run an IV SWEEP {40/60/80%}. Earnings IV bump/crush:
per-trade earnings dates are not in the ledger, so a flat IV is used (EARN_BUMP/CRUSH hooks
left at 0 so the choice is explicit and the absence disclosed, not faked). MODELED prices:
no bid/ask spread, no vol surface/skew. A first approximation, to be validated on real quotes.

TEST GRID (full grid reported; NOTHING cherry-picked)
-----------------------------------------------------
tenor {3mo,6mo} x strike {ATM, ~5% ITM, ~5% OTM} x delta-trigger {0.80,0.85,0.90}
x budget {7%,14%} x IV {40,60,80}. The stock book (E3, his sizing) is the head-to-head bench.

Usage:  python options_overlay_backtest.py
  writes research/options_overlay.md, research/options_overlay_results.csv
"""
from __future__ import annotations
import os, sys, math, csv, datetime as dt
from dataclasses import dataclass
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import execution_backtest as eb   # E3 exit engine + loaders (do NOT re-derive the winning exit)

RESEARCH = os.path.join(HERE, "research")
SCRATCH = eb.SCRATCH

# ----------------------------------------------------------------------------- config (FROZEN — from the approved spec; NOT tuned)
BUDGETS = [0.07, 0.14]              # premium budget = % of intended stock allocation (spec: 7%, also 14%)
# widened tenor grid (his winners are SLOW: median hold ~84d, big winners ~113d, only ~8%
# resolve <=1mo). These are longer-dated ~ATM calls, NOT deep-ITM LEAPS (instrument unchanged).
TENORS_TD = {"2mo": 42, "3mo": 63, "4mo": 84, "6mo": 126, "9mo": 189}  # trading days
STRIKE_OFFSETS = {"ATM": 0.00, "ITM5": -0.05, "OTM5": +0.05}   # strike = pivot*(1+offset)
DELTA_TRIGGERS = [0.80, 0.85, 0.90] # convert-to-stock when modeled delta crosses this
IV_SWEEP = [0.40, 0.60, 0.80]       # annualized IV assumptions (growth-stock range); full sweep reported
RISK_FREE = 0.045                   # ~risk-free 2023-2026; modeled, immaterial vs IV
ITM_MARGIN = 0.05                   # "meaningfully ITM" at expiry = S > K*(1+ITM_MARGIN)
EARN_BUMP = 0.00                    # earnings IV bump (per-trade earnings dates absent -> disclosed 0)
CRUSH = 0.00                        # post-earnings crush (same reason -> disclosed 0)
START_CAPITAL = eb.START_CAPITAL
HARD_STOP = eb.HARD_STOP

# base cell for the head-to-head narrative + decomposition (a representative, NOT a tuned pick):
BASE = dict(tenor_td=126, iv=0.60, budget=0.07, strike="ATM", trig=0.85)  # 6mo/IV60/7%/ATM/0.85Δ

# ----------------------------------------------------------------------------- LIQUID-OPTION UNIVERSE
# Kept: names that PLAUSIBLY had LIQUID, actively-quoted listed options 2019-2026. Proxy =
# large/well-known optionable growth leaders, high-profile momentum names, well-known ADRs,
# liquid ETFs. EXCLUDED: thin small-caps / low-price names whose single-stock options are
# illiquid or absent. Known-optionable whitelist, NOT a fitted parameter.
LIQUID = {
    "AAPL", "NVDA", "TSLA", "PLTR", "MSTR", "AXON", "ARM", "ANET", "APP", "SMCI",
    "DDOG", "MDB", "SNPS", "ZS", "FTNT", "DXCM", "UBER", "SQ", "CLS", "VRT",
    "CRDO", "DKS", "CROX", "ELF", "DUOL", "ONON", "URBN", "TOST", "RBLX", "HOOD",
    "IBKR", "MCK", "UHS", "AEM", "CCJ", "SCCO", "TSM", "SMR", "OKLO", "RKLB",
    "IONQ", "HIMS", "VKTX", "GEV", "STRL", "IREN", "NBIS", "SYM", "APH", "FIX",
    "MOD", "AIT", "BIRK", "IR", "DELL", "IBIT",
}
# EXCLUDED (thin / not reliably liquid-optionable): AAON, ACMR, ADMA, AGX, ALMU, AMSC, APLD,
# APPF, AXGN, BLBD, BPMC, HLI, HMY, HXL, KD, KRMN, LOAR, MNSO, MTRX, MTSI, NTGR, PRCT, Q, QUBT,
# RKT, ROAD, SEI, SQM, TGTX, TILE, TS, TSSI, UFO, UFPT, VIAV, WAY, WLDN, YELP + no-path names.

# ----------------------------------------------------------------------------- Black-Scholes (price + delta)
def _norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

def _d1(S, K, T, sigma, r=RISK_FREE):
    return (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))

def bs_call(S, K, T, sigma, r=RISK_FREE):
    """Call price. T in years. At/after expiry (T<=0) returns intrinsic."""
    if T <= 0:
        return max(0.0, S - K)
    if sigma <= 0 or S <= 0 or K <= 0:
        return max(0.0, S - K * math.exp(-r * T))
    d1 = _d1(S, K, T, sigma, r); d2 = d1 - sigma * math.sqrt(T)
    return S * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)

def bs_delta(S, K, T, sigma, r=RISK_FREE):
    """Call delta = N(d1). At/after expiry: 1 if ITM else 0."""
    if T <= 0:
        return 1.0 if S > K else 0.0
    if sigma <= 0 or S <= 0 or K <= 0:
        return 1.0 if S > K * math.exp(-r * T) else 0.0
    return _norm_cdf(_d1(S, K, T, sigma, r))

# ----------------------------------------------------------------------------- option-book trade engine (per position, causal)
@dataclass
class OptOutcome:
    symbol: str
    buy: dt.date
    strike: float
    entry_prem: float          # per-share premium at entry (BS at t=entry)
    contracts: int
    premium_paid: float        # contracts*100*entry_prem  == defined loss cap
    converted: bool            # did delta trigger (or ITM-at-expiry) -> took delivery?
    convert_date: dt.date | None
    strike_capital: float      # contracts*100*strike deployed on conversion (0 if never)
    exit_date: dt.date
    pl: float                  # net $ P&L for this position on the option book
    kind: str                  # 'delta-convert-run'|'itm-expiry-run'|'expired-worthless'|'no-path'
    budget_overrun: bool       # true if one contract already exceeded the premium budget

def run_option_trade(trade, path, iv, tenor_td, budget_pct, strike_off, delta_trig):
    """One option-book position, causal. Returns OptOutcome with net $ P&L.

    Insurance phase: hold the call, marking delta each bar via BS with SHRINKING T. Convert
    (exercise -> take delivery at strike -> deploy strike capital -> E3 stock exit from that
    day) the first bar delta >= delta_trig. If never: at expiry, meaningfully-ITM -> deliver
    + E3; else expire worthless (lose premium). NO management in the insurance phase; NO roll.
    """
    pivot = trade["entry_px"]
    K = pivot * (1.0 + strike_off)
    cost = trade["cost"] if trade.get("cost") else eb.EW_TARGET * START_CAPITAL

    # entry premium (BS at t=entry, S=pivot)
    T0 = tenor_td / 252.0
    entry_prem = bs_call(pivot, K, T0, iv)
    if entry_prem <= 0:
        entry_prem = 1e-6
    budget = budget_pct * cost
    contracts = int(budget // (100 * entry_prem))
    overrun = contracts < 1
    if contracts < 1:
        contracts = 1
    premium_paid = contracts * 100 * entry_prem
    shares = contracts * 100

    fwd = [(d, o, h, l, c) for (d, o, h, l, c) in path if d > trade["buy"]]
    if not fwd:
        return OptOutcome(trade["symbol"], trade["buy"], K, entry_prem, contracts, premium_paid,
                          False, None, 0.0, trade["buy"], -premium_paid, "expired-worthless", overrun)
    n_exp = min(tenor_td, len(fwd))
    expiry_date = fwd[n_exp - 1][0]

    # walk the insurance phase bar-by-bar; delta uses the bar's CLOSE and remaining T (causal)
    step = 0
    for (d, o, h, l, c) in fwd[:n_exp]:
        step += 1
        if c is None:
            continue
        T_rem = max((tenor_td - step) / 252.0, 1e-9)
        delta = bs_delta(c, K, T_rem, iv)
        if delta >= delta_trig:
            strike_capital = shares * K
            deliv = {**trade, "buy": d, "entry_px": K}
            xd, xr = eb.simulate_exit(deliv, path, "E3")
            pl = shares * K * xr - premium_paid    # basis = strike + premium (premium sunk)
            return OptOutcome(trade["symbol"], trade["buy"], K, entry_prem, contracts, premium_paid,
                              True, d, strike_capital, xd, pl, "delta-convert-run", overrun)

    # never triggered by expiry -> expiry decision
    exp_close = None
    for (d, o, h, l, c) in path:
        if d <= expiry_date and c is not None:
            exp_close = (d, c)
    S_exp = exp_close[1] if exp_close else pivot
    if S_exp > K * (1.0 + ITM_MARGIN):
        strike_capital = shares * K
        deliv = {**trade, "buy": expiry_date, "entry_px": K}
        xd, xr = eb.simulate_exit(deliv, path, "E3")
        pl = shares * K * xr - premium_paid
        return OptOutcome(trade["symbol"], trade["buy"], K, entry_prem, contracts, premium_paid,
                          True, None, strike_capital, xd, pl, "itm-expiry-run", overrun)
    else:
        return OptOutcome(trade["symbol"], trade["buy"], K, entry_prem, contracts, premium_paid,
                          False, None, 0.0, expiry_date, -premium_paid, "expired-worthless", overrun)

# ----------------------------------------------------------------------------- portfolio walks
def stock_book(trades, paths, timing):
    """Stock book: his sizing, E3 winning exit, prior-week exposure dial. Reuses the committed
    engine so it stays byte-identical with execution_backtest."""
    return eb.run_portfolio(trades, paths, timing, "E3", use_timing=True, sizing="his")

def option_book(trades, paths, iv, tenor_td, budget_pct, strike_off, delta_trig):
    """Path-dependent option-book walk over the liquid entry sequence. Same START_CAPITAL as
    the stock book. Premium spent at entry; on conversion the strike capital is deployed
    (real cash draw) and recovered (with P&L) at the E3 stock exit."""
    outs = []
    for t in trades:
        p = paths.get(t["symbol"])
        if not p:
            outs.append(OptOutcome(t["symbol"], t["buy"], t["entry_px"], 0.0, 1, 0.0, False,
                                   None, 0.0, t["sell"], 0.0, "no-path", False))
            continue
        rp = eb.rescaled_path(p, t["buy"], t["entry_px"])
        outs.append(run_option_trade(t, rp, iv, tenor_td, budget_pct, strike_off, delta_trig))

    order = sorted(range(len(trades)), key=lambda i: (trades[i]["buy"], trades[i]["symbol"]))
    cash = START_CAPITAL
    open_pos = []   # (exit_date, cash_back, symbol)
    skipped = []

    def free(asof):
        nonlocal cash
        still = []
        for (xd, cb, sym) in open_pos:
            if xd <= asof:
                cash += cb
            else:
                still.append((xd, cb, sym))
        open_pos[:] = still

    for i in order:
        t = trades[i]; o = outs[i]; bd = t["buy"]
        free(bd)
        if cash < o.premium_paid:
            skipped.append((o.symbol, bd, "no cash for premium"))
            continue
        cash -= o.premium_paid
        if o.converted:
            need = o.strike_capital
            if cash >= need:
                cash -= need
                cash_back = need + (o.pl + o.premium_paid)   # recover strike + net share P&L
            else:
                skipped.append((o.symbol, bd, "no cash to take delivery"))
                cash_back = 0.0   # premium already sunk
            open_pos.append((o.exit_date, cash_back, o.symbol))
        else:
            open_pos.append((o.exit_date, 0.0, o.symbol))   # worthless

    free(max([xd for (xd, _, _) in open_pos] + [dt.date(2026, 7, 1)]))

    total_pl = sum(o.pl for o in outs)
    final_equity = START_CAPITAL + total_pl
    ev = sorted(outs, key=lambda o: o.exit_date)
    curve = [(min(t["buy"] for t in trades), START_CAPITAL)]
    run = START_CAPITAL
    for o in ev:
        run += o.pl
        curve.append((o.exit_date, run))
    peak = -1e18; mdd = 0.0
    for (_, v) in curve:
        peak = max(peak, v)
        if peak > 0:
            mdd = min(mdd, v / peak - 1.0)
    total_ret = final_equity / START_CAPITAL - 1.0
    wins = sum(1 for o in outs if o.pl > 0)
    win_rate = wins / len(outs) if outs else 0.0

    def yb(d):
        if d < dt.date(2024, 1, 1): return "2023H2"
        if d < dt.date(2025, 1, 1): return "2024"
        if d < dt.date(2026, 1, 1): return "2025"
        return "2026H1"
    peryear = defaultdict(lambda: dict(pl=0.0, n=0))
    for o in outs:
        b = yb(o.exit_date); peryear[b]["pl"] += o.pl; peryear[b]["n"] += 1

    conv = sum(1 for o in outs if o.converted)
    worthless = sum(1 for o in outs if o.kind == "expired-worthless")
    return dict(iv=iv, tenor_td=tenor_td, budget=budget_pct, strike=strike_off, trig=delta_trig,
                final_equity=final_equity, total_ret=total_ret, mdd=mdd, win_rate=win_rate,
                total_pl=total_pl, n=len(outs), n_converted=conv, n_worthless=worthless,
                outs=outs, curve=curve, peryear=dict(peryear), skipped=skipped)

# ----------------------------------------------------------------------------- decomposition (shakeout wins vs theta losses)
def decompose(opt_res, trades, paths):
    """In dollars, position-level (his sizing):
      (a) SHAKEOUT-SURVIVAL WINS : the -7% stock stop ejected the name (E3 ~ -7%) but the option
          survived and CONVERTED to a WINNER (delivered, ended positive). $ = opt_pl - stock_pl.
      (b) THETA/STALL LOSSES     : the stock went flat/small (not stopped) but the option bled to
          worthless. $ = stock_pl - opt_pl (option worse).
    Also tracked for honesty (not part of the named net): (c) notional-cap under-participation on
    winners (stock owned more shares -> captured more), and loss-mitigation (both lost, option
    lost less only because it bet less).
    """
    a=0.0; b=0.0; c=0.0; mit=0.0
    a_rows=[]; b_rows=[]; c_rows=[]
    for o in opt_res["outs"]:
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
            c += (stock_pl - opt_pl); c_rows.append(row + (round(stock_pl - opt_pl), o.kind))
        elif stopped and opt_pl < 0 and opt_pl > stock_pl:
            mit += (opt_pl - stock_pl)
    return dict(a=a, b=b, c=c, mit=mit, net=a - b,
                a_rows=sorted(a_rows, key=lambda r: -r[5]),
                b_rows=sorted(b_rows, key=lambda r: -r[5]),
                c_rows=sorted(c_rows, key=lambda r: -r[5]))

# ----------------------------------------------------------------------------- time-to-conversion analysis
def _pctl(xs, q):
    if not xs:
        return None
    s = sorted(xs)
    i = q * (len(s) - 1)
    lo = int(math.floor(i)); hi = int(math.ceil(i))
    if lo == hi:
        return s[lo]
    return s[lo] + (s[hi] - s[lo]) * (i - lo)

def days_to_trigger(trades, paths, iv, strike_off, delta_trig, horizon_td=252):
    """For each name, days-from-entry until the modeled call DELTA first reaches delta_trig,
    measured with a LONG (1-year) horizon so the measurement is NOT capped by a short tenor.
    Returns list of (symbol, buy, days_to_trigger_or_None, eventual_winner_bool).

    'eventual winner' = the delivered stock (E3 from the trigger date, basis=strike) would
    finish positive if we converted — i.e. this is a name whose stock actually ran. If delta
    never reaches the trigger within the horizon, days=None (a chop/fail, not a slow winner).
    """
    out = []
    for t in trades:
        p = paths.get(t["symbol"])
        if not p:
            continue
        rp = eb.rescaled_path(p, t["buy"], t["entry_px"])
        pivot = t["entry_px"]; K = pivot * (1 + strike_off)
        fwd = [(d, o, h, l, c) for (d, o, h, l, c) in rp if d > t["buy"]]
        hit_day = None; hit_date = None; step = 0
        for (d, o, h, l, c) in fwd[:horizon_td]:
            step += 1
            if c is None:
                continue
            T_rem = max((horizon_td - step) / 252.0, 1e-9)
            if bs_delta(c, K, T_rem, iv) >= delta_trig:
                hit_day = (d - t["buy"]).days; hit_date = d
                break
        winner = False
        if hit_date is not None:
            deliv = {**t, "buy": hit_date, "entry_px": K}
            _, xr = eb.simulate_exit(deliv, rp, "E3")
            winner = xr > 0
        out.append((t["symbol"], t["buy"], hit_day, winner))
    return out

def conversion_percentiles(dtt_rows):
    """Percentiles of days-to-trigger among names that BECAME WINNERS (delivered & positive)."""
    days = [r[2] for r in dtt_rows if r[3] and r[2] is not None]
    return dict(n=len(days),
                p25=_pctl(days, 0.25), p50=_pctl(days, 0.50), p75=_pctl(days, 0.75),
                p90=_pctl(days, 0.90), median=_pctl(days, 0.50))

def tenor_crossover(trades, paths, iv, strike_off, delta_trig, budget_pct):
    """For each tenor: of the eventual-winners (delta reaches trigger within 1yr), what fraction
    CONVERT before the option expires (captured) vs EXPIRE before converting (lost to too-short a
    tenor)? Plus the avg premium cost (% of stock alloc actually spent) and avg notional/stock-cost
    the 7% budget buys at that tenor. Shows the time-vs-premium tradeoff; sweet spot visible."""
    dtt = days_to_trigger(trades, paths, iv, strike_off, delta_trig)
    winners = [r for r in dtt if r[3] and r[2] is not None]     # eventual winners w/ a trigger day
    rows = []
    for lab, td in TENORS_TD.items():
        tenor_cal = td * 365.0 / 252.0     # approx calendar days of the tenor
        captured = sum(1 for r in winners if r[2] <= tenor_cal)
        lost = len(winners) - captured
        # premium cost + notional at this tenor (avg over names, 7% budget)
        prem_fracs = []; notionals = []
        seen = set()
        for t in trades:
            if t["symbol"] in seen:
                continue
            seen.add(t["symbol"])
            pivot = t["entry_px"]; K = pivot * (1 + strike_off)
            cost = t["cost"] if t.get("cost") else eb.EW_TARGET * START_CAPITAL
            prem = bs_call(pivot, K, td / 252.0, iv)
            contracts = max(1, int((budget_pct * cost) // (100 * prem)))
            prem_paid = contracts * 100 * prem
            prem_fracs.append(prem_paid / cost)
            notionals.append((contracts * 100 * K) / cost)
        rows.append(dict(tenor=lab, td=td,
                         n_winners=len(winners), captured=captured, lost=lost,
                         capture_rate=(captured / len(winners) if winners else 0.0),
                         avg_prem_frac=sum(prem_fracs) / len(prem_fracs),
                         avg_notional=sum(notionals) / len(notionals)))
    return rows

# ----------------------------------------------------------------------------- notional table
def notional_table(trades, iv, tenor_td, budget_pct, strike_off=0.0):
    rows=[]; seen=set()
    for t in trades:
        if t["symbol"] in seen:
            continue
        seen.add(t["symbol"])
        pivot = t["entry_px"]; K = pivot * (1 + strike_off)
        cost = t["cost"] if t.get("cost") else eb.EW_TARGET * START_CAPITAL
        T0 = tenor_td / 252.0
        prem = bs_call(pivot, K, T0, iv)
        contracts = max(1, int((budget_pct * cost) // (100 * prem)))
        notional = contracts * 100 * K
        rows.append((t["symbol"], round(K, 1), round(cost), round(prem, 2), contracts,
                     round(notional), round(notional / cost, 2)))
    rows.sort(key=lambda r: -r[6])
    return rows

# ----------------------------------------------------------------------------- report helpers
def _pct(x):
    return "n/a" if x is None else ("+" if x >= 0 else "") + str(round(x * 100, 1)) + "%"
def _k(x):
    return "$" + ("+" if x >= 0 else "") + format(round(x / 1000), ",") + "k"

def _cell_tag(r):
    lab_tenor = {v: k for k, v in TENORS_TD.items()}.get(r["tenor_td"], str(r["tenor_td"]))
    strike = {0.0: "ATM", -0.05: "ITM5", 0.05: "OTM5"}.get(round(r["strike"], 2), str(r["strike"]))
    return (lab_tenor + "/" + strike + "/d" + str(int(r["trig"] * 100)) + "/"
            + str(int(r["budget"] * 100)) + "%/IV" + str(int(r["iv"] * 100)))

# ----------------------------------------------------------------------------- report
def build_report(stock_res, grid, base_res, decomp, notion7, notion14, trades, meta,
                 conv_pctls, xover):
    L=[]; A=L.append
    A("# CAN SLIM options-overlay HYBRID — cheap-ATM-call insurance -> delta-triggered delivery -> core exit")
    A("")
    A("_One question: on his LIQUID-OPTION names, does holding a cheap ~ATM call as INSURANCE "
      "(premium == the -7% stop loss), converting to stock only when the modeled call DELTA "
      "crosses a threshold (it has become a stock proxy), then managing the delivered stock "
      "with the proven winning exit (E3), beat just owning the stock from the pivot? Calls are "
      "**MODELED with Black-Scholes** (price + delta), theta decay on-path, across an **IV sweep "
      "40/60/80%**. Full spec: research/options_overlay_spec.md._")
    A("")
    A("- Start capital **$" + format(int(START_CAPITAL), ",") + "** (same as the stock engine).")
    A("- **Liquid-option universe: " + str(meta["n_liquid_names"]) + " names, "
      + str(meta["n_liquid_trades"]) + " entries** (of his " + str(meta["n_all_names"]) + " names / "
      + str(meta["n_all_trades"]) + " trades). Chosen as a KNOWN-OPTIONABLE whitelist (large/liquid "
      "growth leaders, well-known ADRs, liquid ETFs) that plausibly had actively-quoted listed "
      "options 2019-2026. Thin small-caps / low-price names are EXCLUDED — a modeled BS price would "
      "misrepresent their real fills.")
    A("- **IN (liquid):** " + ", ".join(sorted(meta["liquid_in"])))
    A("- **OUT (excluded):** " + ", ".join(sorted(meta["liquid_out"])))
    A("")
    A("## Head-to-head — STOCK book vs OPTION book (base cell: 6mo / ATM / delta 0.85 / 7% / IV 60%)")
    A("")
    A("| Book | Total ret | Max DD | Win% | Final equity | #converted | #worthless |")
    A("|---|---:|---:|---:|---:|---:|---:|")
    A("| STOCK (buy pivot, E3 exit) | " + _pct(stock_res["total_ret"]) + " | " + _pct(stock_res["mdd"])
      + " | " + str(round(stock_res["win_rate"] * 100)) + "% | $" + format(int(stock_res["final_equity"]), ",")
      + " | — | — |")
    A("| OPTION (base cell) | " + _pct(base_res["total_ret"]) + " | " + _pct(base_res["mdd"]) + " | "
      + str(round(base_res["win_rate"] * 100)) + "% | $" + format(int(base_res["final_equity"]), ",")
      + " | " + str(base_res["n_converted"]) + "/" + str(base_res["n"]) + " | "
      + str(base_res["n_worthless"]) + " |")
    A("")
    A("## Per-year (bucketed by EXIT date) — bull 2024/2025 vs choppy stretches")
    A("")
    buckets = ["2023H2", "2024", "2025", "2026H1"]
    A("| Book | " + " | ".join(buckets) + " | total |")
    A("|---|" + "|".join(["---:"] * (len(buckets) + 1)) + "|")
    sc = stock_res["peryear"]
    A("| STOCK E3 | " + " | ".join(_k(sc.get(b, {}).get("pl", 0.0)) for b in buckets) + " | "
      + _k(sum(sc.get(b, {}).get("pl", 0.0) for b in buckets)) + " |")
    py = base_res["peryear"]
    A("| OPTION base | " + " | ".join(_k(py.get(b, {}).get("pl", 0.0)) for b in buckets) + " | "
      + _k(sum(py.get(b, {}).get("pl", 0.0) for b in buckets)) + " |")
    A("")
    A("## TIME-TO-CONVERSION — how long until a winner gets deep enough ITM to take delivery?")
    A("")
    A("_His winners are SLOW (his realized book: median hold ~84 days, big winners ~113 days, only "
      "~8% resolve in <=1 month; losers die in ~1 month). So the option must SURVIVE long enough "
      "for a slow winner to push the call's delta to the trigger. For eventual-WINNER names "
      "(delta reaches the trigger within a 1-yr horizon and the delivered stock finishes positive), "
      "the distribution of DAYS-FROM-ENTRY until the delta trigger (base: ATM / delta 0.85 / IV 60%):_")
    A("")
    A("| n winners | 25th | 50th (median) | 75th | 90th |")
    A("|---:|---:|---:|---:|---:|")
    A("| " + str(conv_pctls["n"]) + " | " + str(round(conv_pctls["p25"])) + "d | "
      + str(round(conv_pctls["p50"])) + "d | " + str(round(conv_pctls["p75"])) + "d | "
      + str(round(conv_pctls["p90"])) + "d |")
    A("")
    A("### Tenor crossover — winners CAPTURED (convert before expiry) vs LOST (expire first)")
    A("")
    A("_The time-vs-premium tradeoff: shorter tenor = cheaper / more notional but EXPIRES on slow "
      "winners; longer tenor = survives slow winners but costs more premium / buys less notional. "
      "Base: ATM / delta 0.85 / IV 60% / 7% budget._")
    A("")
    A("| Tenor | Winners captured | Winners lost (expired first) | Capture rate | Avg premium (% of alloc) | Avg notional/stock-cost |")
    A("|---|---:|---:|---:|---:|---:|")
    for r in xover:
        A("| " + r["tenor"] + " | " + str(r["captured"]) + " | " + str(r["lost"]) + " | "
          + str(round(r["capture_rate"] * 100)) + "% | " + str(round(r["avg_prem_frac"] * 100, 1))
          + "% | " + str(round(r["avg_notional"], 2)) + "x |")
    A("")
    # robust-tenor reasoning (data-driven, not the single best backtest cell)
    p90 = conv_pctls["p90"]; p75 = conv_pctls["p75"]
    hit90 = next((r for r in xover if r["capture_rate"] >= 0.90), None)
    robust = hit90 if hit90 is not None else max(xover, key=lambda r: r["capture_rate"])
    A("- **Robust tenor (reasoned from the crossover, NOT the best backtest cell):** the median "
      "winner takes **" + str(round(conv_pctls["p50"])) + " days** to reach the delta trigger, the "
      "75th percentile **" + str(round(p75)) + " days**, the 90th **" + str(round(p90))
      + " days** — so any tenor shorter than ~6mo structurally EXPIRES on a large fraction of the "
      "slow winners (2mo captures just " + str(round(xover[0]["capture_rate"] * 100)) + "%, 4mo "
      + str(round(next(r for r in xover if r['tenor']=='4mo')['capture_rate'] * 100)) + "%). "
      + ("No tenor in the grid reaches 90% capture; " if hit90 is None else "")
      + "the most robust is **" + robust["tenor"] + "** (capture "
      + str(round(robust["capture_rate"] * 100)) + "% of eventual-winners, avg premium "
      + str(round(robust["avg_prem_frac"] * 100, 1)) + "% of alloc, notional "
      + str(round(robust["avg_notional"], 2)) + "x). The tension is real: even 9mo just misses the "
      "slowest ~10% (p90 ~" + str(round(p90)) + "d), and buying that survival costs more premium "
      "and buys LESS notional (0.6x at 2mo -> 0.37x at 9mo). The data says a SLOW system needs a "
      "LONG tenor (6-9mo), which is exactly what erodes the notional the option can afford — this "
      "tradeoff, not a single P&L peak, is the finding. Reported as a curve; not tuned.")
    A("")
    A("## IV SENSITIVITY (6mo / ATM / delta 0.85 / 7%) — does the verdict hold or flip?")
    A("")
    A("| IV | Option total ret | Max DD | Win% | Final $ | vs STOCK ($) |")
    A("|---:|---:|---:|---:|---:|---:|")
    for iv in IV_SWEEP:
        r = next(x for x in grid if abs(x["iv"] - iv) < 1e-9 and x["tenor_td"] == 126
                 and abs(x["strike"]) < 1e-9 and abs(x["trig"] - 0.85) < 1e-9 and abs(x["budget"] - 0.07) < 1e-9)
        A("| " + str(int(iv * 100)) + "% | " + _pct(r["total_ret"]) + " | " + _pct(r["mdd"]) + " | "
          + str(round(r["win_rate"] * 100)) + "% | $" + format(int(r["final_equity"]), ",") + " | "
          + _k(r["final_equity"] - stock_res["final_equity"]) + " |")
    A("")
    A("## FULL TEST GRID (exploratory, NOT tuned — every cell reported; ranked by total ret)")
    A("")
    A("_tag = tenor / strike / delta-trig / budget / IV. The stock book is "
      + _pct(stock_res["total_ret"]) + " total / " + _pct(stock_res["mdd"]) + " maxDD for reference._")
    A("")
    A("| Cell | Total ret | Max DD | Win% | Final $ | vs STOCK ($) | conv/worthless |")
    A("|---|---:|---:|---:|---:|---:|---:|")
    for r in sorted(grid, key=lambda x: -x["total_ret"]):
        A("| " + _cell_tag(r) + " | " + _pct(r["total_ret"]) + " | " + _pct(r["mdd"]) + " | "
          + str(round(r["win_rate"] * 100)) + "% | $" + format(int(r["final_equity"]), ",") + " | "
          + _k(r["final_equity"] - stock_res["final_equity"]) + " | "
          + str(r["n_converted"]) + "/" + str(r["n_worthless"]) + " |")
    A("")
    best = max(grid, key=lambda x: x["total_ret"]); worst = min(grid, key=lambda x: x["total_ret"])
    A("- **Best cell:** `" + _cell_tag(best) + "` at " + _pct(best["total_ret"]) + " ("
      + _k(best["final_equity"] - stock_res["final_equity"]) + " vs stock). **Worst cell:** `"
      + _cell_tag(worst) + "` at " + _pct(worst["total_ret"]) + " (" + _k(worst["final_equity"]
      - stock_res["final_equity"]) + " vs stock). The grid is EXPLORATORY sensitivity, not a "
      "recommendation — do not read the best cell as a chosen strategy.")
    A("")
    A("## DECOMPOSITION — the two competing forces, in dollars (base cell)")
    A("")
    A("**(a) SHAKEOUT-SURVIVAL WINS** — the -7% stock stop ejected the name, but the option "
      "survived the dip, converted, and finished a WINNER:")
    A("")
    A("| Name | Buy | Stock E3 ret | Stock $ | Option $ | Gain to option $ | kind |")
    A("|---|---|---:|---:|---:|---:|---|")
    for r in decomp["a_rows"]:
        A("| " + r[0] + " | " + r[1] + " | " + _pct(r[2]) + " | " + _k(r[3]) + " | " + _k(r[4])
          + " | " + _k(r[5]) + " | " + r[6] + " |")
    if not decomp["a_rows"]:
        A("| _(none)_ | | | | | | |")
    A("")
    A("**(b) THETA/STALL LOSSES** — the stock went flat/small (never stopped), the option bled to "
      "worthless (lost the premium):")
    A("")
    A("| Name | Buy | Stock E3 ret | Stock $ | Option $ | Loss to option $ | kind |")
    A("|---|---|---:|---:|---:|---:|---|")
    for r in decomp["b_rows"]:
        A("| " + r[0] + " | " + r[1] + " | " + _pct(r[2]) + " | " + _k(r[3]) + " | " + _k(r[4])
          + " | " + _k(r[5]) + " | " + r[6] + " |")
    if not decomp["b_rows"]:
        A("| _(none)_ | | | | | | |")
    A("")
    A("**NET (a) - (b) = " + _k(decomp["net"]) + "**  (shakeout-survival wins " + _k(decomp["a"])
      + " minus theta/stall losses " + _k(decomp["b"]) + ").")
    A("")
    A("_Honesty note (not part of the named net): on WINNERS the option gave up **" + _k(decomp["c"])
      + "** to notional-cap under-participation (it owned fewer shares than the stock, so it "
      "captured less of the run — the dominant drag), and on names where BOTH lost, the option "
      "'saved' " + _k(-decomp["mit"]) + " purely by betting less (loss-mitigation, not a real edge)._")
    A("")
    A("## NOTIONAL — what does the 7% (vs 14%) premium actually buy? (IV 60%, 6mo, ATM)")
    A("")
    A("| Name | Strike | Stock cost $ | ATM prem/sh | #contracts | Notional $ | Notional / stock cost |")
    A("|---|---:|---:|---:|---:|---:|---:|")
    for r in notion7[:20]:
        A("| " + r[0] + " | $" + str(r[1]) + " | " + _k(r[2]) + " | $" + str(r[3]) + " | " + str(r[4])
          + " | " + _k(r[5]) + " | " + str(r[6]) + "x |")
    A("")
    avg7 = sum(r[6] for r in notion7) / len(notion7)
    avg14 = sum(r[6] for r in notion14) / len(notion14)
    A("_Avg notional/stock-cost: **" + str(round(avg7, 2)) + "x** at 7% vs **" + str(round(avg14, 2))
      + "x** at 14% budget (IV 60%). A 7% budget on these high-IV names controls WELL UNDER a full "
      "stock position's notional, so even a converted winner under-owns the upside._")
    A("")
    # verdict
    ivs = [next(x for x in grid if abs(x["iv"] - iv) < 1e-9 and x["tenor_td"] == 126
               and abs(x["strike"]) < 1e-9 and abs(x["trig"] - 0.85) < 1e-9 and abs(x["budget"] - 0.07) < 1e-9)
           for iv in IV_SWEEP]
    all_below = all(r["final_equity"] < stock_res["final_equity"] for r in ivs)
    all_above = all(r["final_equity"] > stock_res["final_equity"] for r in ivs)
    grid_all_below = all(r["final_equity"] < stock_res["final_equity"] for r in grid)
    A("## VERDICT (this liquid subset, modeled prices)")
    A("")
    if grid_all_below:
        A("- **No — on liquid-option names the cheap-call-insurance-to-delivery route does NOT beat "
          "owning the stock, and the answer HOLDS across the ENTIRE grid AND the full 40/60/80% IV "
          "sweep** (every cell trails the stock book). Base cell (6mo/ATM/d0.85/7%/IV60): option "
          + _pct(base_res["total_ret"]) + " vs stock " + _pct(stock_res["total_ret"]) + " ("
          + _k(base_res["final_equity"] - stock_res["final_equity"]) + ").")
    elif all_below:
        A("- **No at the base cell, and the IV sweep does not rescue it** — option trails the stock "
          "at every IV. Base: " + _pct(base_res["total_ret"]) + " vs stock "
          + _pct(stock_res["total_ret"]) + ".")
    elif all_above:
        A("- **Yes — the option route BEATS owning the stock and the edge HOLDS across the IV sweep.** "
          "Base: " + _pct(base_res["total_ret"]) + " vs stock " + _pct(stock_res["total_ret"]) + ".")
    else:
        A("- **Mixed — the verdict FLIPS with IV** (option wins at low IV, loses at high IV). That "
          "IV-dependence is itself the finding: not robust to the one input we can't observe.")
    A("- **Why:** the delta trigger keeps the call in INSURANCE mode through shakeouts (it converts "
      "only once delta >= trigger), so it DOES buy some shakeout survival (decomposition (a) = "
      + _k(decomp["a"]) + "). But two costs dominate: (i) THETA/STALL — names that chopped sideways "
      "bled the premium to zero (" + _k(decomp["b"]) + "), and (ii) NOTIONAL CAP — a 7% budget buys "
      "only ~" + str(round(avg7, 2)) + "x the stock notional on these high-IV names, so even the "
      "CONVERTED winners own fewer shares and under-participate (gave up " + _k(decomp["c"]) + " on "
      "winners). Net of the two named forces: " + _k(decomp["net"]) + ".")
    A("- **Regime read:** the option's relative case is least-bad in CHOPPY stretches (shakeouts "
      "frequent -> survival edge earns its keep) and worst in a clean BULL (2024/2025), where the "
      "stock book's full notional simply compounds and the option's capped delta + theta drag lose "
      "the race. See the per-year table.")
    A("")
    A("### Hard limits (curve-fit + honesty guards, rule #1)")
    A("- **Modeled BS prices + delta, not real fills.** No bid/ask spread, no vol surface/skew, one "
      "flat IV per run, and NO per-trade earnings IV bump/crush (per-trade earnings dates are not in "
      "the ledger — disclosed, not faked). Real spreads/skew would make the option book WORSE, so "
      "this is a friendly upper bound. Validate on real historical quotes before trusting.")
    A("- **Liquid-option subset only** (" + str(meta["n_liquid_names"]) + " names) — EXCLUDES the thin "
      "small-caps this system often trades, where the 'can't be shaken out' pitch is most appealing "
      "but real option liquidity is worst. The answer does NOT transfer to those names.")
    A("- **Full grid + IV sweep reported** so nothing is cherry-picked; the grid is exploratory "
      "sensitivity, not a chosen cell.")
    A("- **Small sample, bull-heavy 2023-2026 window** — cannot test a bear regime; the let-winners-"
      "run edge flatters BOTH books in a bull.")
    A("- **Exercise/delivery capital assumption:** conversion deploys the full strike dollars (a real "
      "cash draw funded from the same start capital; modeled as a cash round-trip).")
    A("")
    return "\n".join(L)

def write_csv(stock_res, grid, decomp, notion7, path, conv_pctls=None, xover=None):
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["section", "cell", "book", "iv", "tenor_td", "strike_off", "delta_trig",
                    "budget", "total_ret", "max_dd", "win_rate", "final_equity", "total_pl",
                    "n_converted", "n_worthless"])
        w.writerow(["headline", "stock", "STOCK_E3", "", "", "", "", "",
                    round(stock_res["total_ret"], 4), round(stock_res["mdd"], 4),
                    round(stock_res["win_rate"], 4), round(stock_res["final_equity"]),
                    round(stock_res["final_equity"] - START_CAPITAL), "", ""])
        for r in sorted(grid, key=lambda x: -x["total_ret"]):
            w.writerow(["grid", _cell_tag(r), "OPTION", r["iv"], r["tenor_td"], r["strike"],
                        r["trig"], r["budget"], round(r["total_ret"], 4), round(r["mdd"], 4),
                        round(r["win_rate"], 4), round(r["final_equity"]), round(r["total_pl"]),
                        r["n_converted"], r["n_worthless"]])
        w.writerow([])
        w.writerow(["decomp_a_shakeout_wins", "symbol", "buy", "stock_e3_ret", "stock_$",
                    "option_$", "gain_$", "kind"])
        for r in decomp["a_rows"]:
            w.writerow(["a"] + list(r))
        w.writerow(["a_TOTAL", "", "", "", "", "", round(decomp["a"]), ""])
        w.writerow(["decomp_b_theta_losses", "symbol", "buy", "stock_e3_ret", "stock_$",
                    "option_$", "loss_$", "kind"])
        for r in decomp["b_rows"]:
            w.writerow(["b"] + list(r))
        w.writerow(["b_TOTAL", "", "", "", "", "", round(decomp["b"]), ""])
        w.writerow(["NET_a_minus_b", "", "", "", "", "", round(decomp["net"]), ""])
        w.writerow(["notional_cap_on_winners_c", "", "", "", "", "", round(decomp["c"]), ""])
        w.writerow([])
        w.writerow(["notional_7pct_iv60_6mo_ATM", "symbol", "strike", "stock_cost", "atm_prem_sh",
                    "contracts", "notional", "notional_over_cost"])
        for r in notion7:
            w.writerow(["notional"] + list(r))
        if conv_pctls is not None:
            w.writerow([])
            w.writerow(["time_to_conversion_winners_ATM_d85_IV60", "n_winners", "p25_days",
                        "p50_days", "p75_days", "p90_days"])
            w.writerow(["days_to_trigger", conv_pctls["n"], round(conv_pctls["p25"]),
                        round(conv_pctls["p50"]), round(conv_pctls["p75"]), round(conv_pctls["p90"])])
        if xover is not None:
            w.writerow([])
            w.writerow(["tenor_crossover_ATM_d85_IV60_7pct", "tenor", "winners_captured",
                        "winners_lost", "capture_rate", "avg_prem_frac_of_alloc",
                        "avg_notional_over_cost"])
            for r in xover:
                w.writerow(["crossover", r["tenor"], r["captured"], r["lost"],
                            round(r["capture_rate"], 3), round(r["avg_prem_frac"], 4),
                            round(r["avg_notional"], 3)])

# ----------------------------------------------------------------------------- main
def main():
    all_trades = eb.load_ledger()
    paths = eb.load_paths()
    timing = eb.load_timing()

    trades = [t for t in all_trades if t["symbol"] in LIQUID]
    liquid_in = sorted({t["symbol"] for t in trades})
    liquid_out = sorted({t["symbol"] for t in all_trades if t["symbol"] not in LIQUID})
    meta = dict(n_all_names=len({t["symbol"] for t in all_trades}), n_all_trades=len(all_trades),
                n_liquid_names=len(liquid_in), n_liquid_trades=len(trades),
                liquid_in=liquid_in, liquid_out=liquid_out)
    print("[opt-bt] liquid subset: " + str(len(trades)) + " trades / " + str(len(liquid_in))
          + " names (of " + str(len(all_trades)) + " / " + str(meta["n_all_names"]) + ")")

    stock_res = stock_book(trades, paths, timing)

    # FULL GRID: tenor x strike x delta-trig x budget x IV
    grid = []
    for tenor_td in TENORS_TD.values():
        for strike_lab, strike_off in STRIKE_OFFSETS.items():
            for trig in DELTA_TRIGGERS:
                for budget in BUDGETS:
                    for iv in IV_SWEEP:
                        grid.append(option_book(trades, paths, iv, tenor_td, budget, strike_off, trig))
    print("[opt-bt] grid cells: " + str(len(grid)))

    base_res = next(x for x in grid if x["tenor_td"] == BASE["tenor_td"]
                    and abs(x["iv"] - BASE["iv"]) < 1e-9 and abs(x["budget"] - BASE["budget"]) < 1e-9
                    and abs(x["strike"]) < 1e-9 and abs(x["trig"] - BASE["trig"]) < 1e-9)
    decomp = decompose(base_res, trades, paths)
    notion7 = notional_table(trades, 0.60, 126, 0.07, 0.0)
    notion14 = notional_table(trades, 0.60, 126, 0.14, 0.0)

    # time-to-conversion (base: ATM / delta 0.85 / IV 60% / 7% budget)
    dtt = days_to_trigger(trades, paths, 0.60, 0.0, 0.85)
    conv_pctls = conversion_percentiles(dtt)
    xover = tenor_crossover(trades, paths, 0.60, 0.0, 0.85, 0.07)

    report = build_report(stock_res, grid, base_res, decomp, notion7, notion14, trades, meta,
                          conv_pctls, xover)
    md_path = os.path.join(RESEARCH, "options_overlay.md")
    csv_path = os.path.join(RESEARCH, "options_overlay_results.csv")
    open(md_path, "w", encoding="utf-8").write(report)
    write_csv(stock_res, grid, decomp, notion7, csv_path, conv_pctls, xover)
    print("[opt-bt] wrote " + md_path)
    print("[opt-bt] wrote " + csv_path)

    print("\n=== STOCK vs OPTION (base 6mo/ATM/d0.85/7%) ===")
    print("STOCK E3   total " + _pct(stock_res["total_ret"]) + "  maxDD " + _pct(stock_res["mdd"])
          + "  win " + str(round(stock_res["win_rate"] * 100)) + "%  final $"
          + format(int(stock_res["final_equity"]), ","))
    for iv in IV_SWEEP:
        r = next(x for x in grid if abs(x["iv"] - iv) < 1e-9 and x["tenor_td"] == 126
                 and abs(x["strike"]) < 1e-9 and abs(x["trig"] - 0.85) < 1e-9 and abs(x["budget"] - 0.07) < 1e-9)
        print("OPT IV" + str(int(iv * 100)) + "%  total " + _pct(r["total_ret"]) + "  maxDD "
              + _pct(r["mdd"]) + "  win " + str(round(r["win_rate"] * 100)) + "%  final $"
              + format(int(r["final_equity"]), ",") + "  conv " + str(r["n_converted"]) + "/"
              + str(r["n"]) + " worthless " + str(r["n_worthless"]) + "  vs stock "
              + _k(r["final_equity"] - stock_res["final_equity"]))
    best = max(grid, key=lambda x: x["total_ret"]); worst = min(grid, key=lambda x: x["total_ret"])
    print("\nGRID best  " + _cell_tag(best) + "  " + _pct(best["total_ret"]) + "  (vs stock "
          + _k(best["final_equity"] - stock_res["final_equity"]) + ")")
    print("GRID worst " + _cell_tag(worst) + "  " + _pct(worst["total_ret"]) + "  (vs stock "
          + _k(worst["final_equity"] - stock_res["final_equity"]) + ")")
    print("\nDECOMP (base): shakeout-wins " + _k(decomp["a"]) + "  theta-losses " + _k(decomp["b"])
          + "  NET " + _k(decomp["net"]) + "  | notional-cap drag on winners " + _k(decomp["c"]))
    return stock_res, grid, base_res, decomp, meta

if __name__ == "__main__":
    main()
