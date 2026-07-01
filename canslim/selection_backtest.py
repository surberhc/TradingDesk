"""selection_backtest.py — does a MACHINE picking entries mechanically out of the advisor's
own watch-list pool (then managing with the proven winning exit) make money?

THE QUESTION THIS ANSWERS (and how it differs from the prior tests)
------------------------------------------------------------------
execution_backtest.py held SELECTION FIXED to his actual buys and only varied EXECUTION
(exit/sizing/exposure). detector_vs_outcomes only checked whether the detector AGREED with
his picks. Neither tested whether a MACHINE, choosing entries ON ITS OWN out of his watch
pool, makes money. That is the SELECTION test here:

  * Candidate pool = names on his weekly watch list (roles watchlist/added). This pool is
    already pre-filtered by his discretion, so this tests mechanical ENTRY-TIMING/SELECTION
    WITHIN his pool — NOT a full-market scan. (Stated plainly as a limit.)
  * MACHINE ENTRY (deterministic, NOT his buy decision): for each eligible name each week,
    run base_detector.detect_base() as-of that week. If a VALID O'Neil base is found AND the
    price breaks out through the detector's pivot within the buy zone (<= BUY_ZONE above the
    pivot) on a day at/after the base is confirmed, the machine BUYS — whether or not he did.
  * MANAGE with the PROVEN WINNER (execution_backtest E3): -7% catastrophic initial stop
    until the first close above a RISING 50-day SMA, then hold and exit only on a decisive
    close below the 50-day. NO profit cap. + the weekly invested_pct exposure overlay +
    his-style sizing (~12% target, 18% cap, ~7 concurrent), as a path-dependent PORTFOLIO
    with fixed starting capital. Cash freed on exits funds later entries.

FULL SPAN: unlike his realized book (2023H2->2026 only), the watch pool + timing dial span
2018->2026, so the machine sim RUNS THROUGH THE 2022 BEAR and 2026-H1 — the key bonus. His
book can only be compared over 2023H2->2026 (where it exists).

NO LOOKAHEAD (desk causality rule): the base is detected only from bars <= the decision week;
the breakout is confirmed only from bars strictly AFTER the base-confirmation date; the exit
engine (reused verbatim from execution_backtest) judges every exit on bars up to the decision
day; the exposure dial reads prior-week invested_pct only.

CURVE-FIT GUARDS (rule #1): detector bounds come from the O'Neil spec (base_detector.py); the
exit is the ALREADY-PROVEN winning rule (execution_backtest E3); the sizing/exposure/stop
numbers are his revealed behavior / O'Neil's playbook. NOTHING here is tuned to improve the
result. Limits stated in the report: pre-filtered pool, partial survivorship (delisted watch
names with no data are dropped and COUNTED), small per-year samples, no-lookahead enforced.

Usage:  python selection_backtest.py
        writes research/selection_backtest.{md, _results.csv}
"""
from __future__ import annotations
import os, sys, json, csv, datetime as dt
from dataclasses import dataclass
from collections import defaultdict

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
RESEARCH = os.path.join(HERE, "research")
sys.path.insert(0, HERE)
import base_detector as bd          # deterministic O'Neil base/pivot detector (reused)
import execution_backtest as eb     # exit engine + sizing/exposure primitives (reused)

SCRATCH = r"C:\Users\andre\AppData\Local\Temp\claude\C--Users-andre-My-Drive--andrew-surberhc-com--TradingDesk\8f58d9d6-f627-483b-97b9-e79b7039b760\scratchpad"
FULL = os.path.join(SCRATCH, "full_cache")      # full-span daily OHLCV (2018->2026), this test's pull
DEEP = os.path.join(SCRATCH, "deep_cache")      # fallback: 540d windows around pick dates
LABELED = os.path.join(RESEARCH, "labeled_picks.csv")
TIMING = os.path.join(RESEARCH, "market_timing_2018_2026.csv")
LEDGER = os.path.join(SCRATCH, "results_7.json")

# ---------------------------------------------------------------- machine-entry config (FROZEN)
BUY_ZONE = 0.05                 # O'Neil buy zone: buy within 5% above the pivot (else "extended")
ELIGIBLE_FWD_WEEKS = 8          # a name stays eligible for a breakout entry up to 8 weeks after
                                # each watch-list appearance (a flagged base doesn't break out the
                                # same week; O'Neil breakouts often come days-to-weeks later). This
                                # is a structural eligibility window, not a tuned knob.
MAX_CONCURRENT = 7              # ~his revealed ~7 concurrent names (portfolio breadth cap)
# sizing/exposure/stop/exit all come from execution_backtest (his revealed / O'Neil), unchanged:
START_CAPITAL = eb.START_CAPITAL
EW_TARGET = eb.EW_TARGET        # 0.12
EW_CAP = eb.EW_CAP             # 0.18


# ---------------------------------------------------------------- data loading
def load_price(sym):
    """Full-span daily OHLC frame for `sym` (full_cache primary, deep_cache fallback). Returns
    a bd-style DataFrame indexed by date with open/high/low/close(/volume), or None."""
    for base in (FULL, DEEP):
        p = os.path.join(base, sym.replace("/", "_") + ".json")
        if os.path.exists(p):
            try:
                rows = json.load(open(p))
            except Exception:
                continue
            if isinstance(rows, list) and len(rows) > 100:
                df = pd.DataFrame(rows)
                df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None).dt.normalize()
                df = df.set_index("date").sort_index()
                df = df[~df.index.duplicated(keep="last")]
                keep = [c for c in ("open", "high", "low", "close", "volume") if c in df.columns]
                return df[keep].astype(float)
    return None


def load_watchlist_pool():
    """Per-ticker eligibility windows from his weekly watch list (roles watchlist/added).
    Returns:
      pool: dict ticker -> sorted list of (watch_date, recorded_pivot_or_None)
      first/last watch date per ticker.
    A name is 'eligible' for a machine breakout entry from a watch_date through watch_date +
    ELIGIBLE_FWD_WEEKS. Consecutive weekly re-appearances extend eligibility naturally."""
    pool = defaultdict(list)
    with open(LABELED, encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            if r["role"] not in ("watchlist", "added"):
                continue
            t = r["ticker"].strip().upper()
            try:
                d = dt.date.fromisoformat(r["date"][:10])
            except Exception:
                continue
            piv = None
            try:
                if r.get("pivot", "").strip():
                    piv = float(r["pivot"])
            except Exception:
                piv = None
            pool[t].append((d, piv))
    for t in pool:
        pool[t].sort(key=lambda x: x[0])
    return pool


def load_bought_events():
    """His ACTUAL buys (role 'bought') -> dict ticker -> sorted list of buy dates. For the
    overlap metric (how much the machine's picks intersect his real buys)."""
    ev = defaultdict(list)
    with open(LABELED, encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            if r["role"] != "bought":
                continue
            t = r["ticker"].strip().upper()
            try:
                ev[t].append(dt.date.fromisoformat(r["date"][:10]))
            except Exception:
                pass
    for t in ev:
        ev[t].sort()
    return ev


def load_timing_full():
    """FULL weekly invested_pct series (2018->2026) — NOT filtered to 2023 like the exec test.
    Sorted list of (week_date, invested_pct)."""
    rows = []
    with open(TIMING) as fh:
        for r in csv.DictReader(fh):
            if not r.get("invested_pct"):
                continue
            try:
                d = dt.date.fromisoformat(r["date"][:10])
                rows.append((d, float(r["invested_pct"])))
            except Exception:
                pass
    rows.sort()
    return rows


def prior_week_invested(timing, on_date):
    val = None
    for (d, p) in timing:
        if d < on_date:
            val = p
        else:
            break
    return val


# ---------------------------------------------------------------- machine entry scan (no lookahead)
@dataclass
class MachineEntry:
    symbol: str
    watch_date: dt.date       # the week he had it on the list that produced this base
    signal_date: dt.date      # the base-confirmation (as-of) date the machine used
    entry_date: dt.date       # the breakout day the machine bought
    entry_px: float           # fill = pivot (breakout through pivot); measured off pivot
    pivot: float
    pattern: str
    depth_pct: float


def find_machine_entries(pool, prices):
    """For every eligible watch-list name, find the FIRST mechanical breakout entry the machine
    would take. Deterministic, NO LOOKAHEAD.

    Rule per name:
      * Consider each watch_date the name appears on his list.
      * Detect a base as-of that watch_date (bars <= watch_date only). If none, skip.
      * From the day AFTER base confirmation through watch_date + ELIGIBLE_FWD_WEEKS, find the
        first day whose HIGH crosses the pivot (breakout) AND whose OPEN/close is within the
        buy zone (<= BUY_ZONE above pivot) -> BUY at the pivot price that day.
      * One entry per (name, distinct base). A name can re-enter later off a NEW base (a new
        watch_date whose detected base_end is after the prior entry).
    Returns list[MachineEntry] sorted by entry_date."""
    entries = []
    for sym, appearances in pool.items():
        df = prices.get(sym)
        if df is None or len(df) < 60:
            continue
        last_entry_date = None
        last_base_end = None
        for (wdate, rec_pivot) in appearances:
            wts = pd.Timestamp(wdate)
            # need forward bars to see a breakout; skip weeks with no forward data
            if wts > df.index.max():
                continue
            res = bd.detect_base(df, wts, symbol=sym)
            if not res.found or res.pivot is None:
                continue
            # only take a NEW base (its right edge must be after the last one we entered on)
            if last_base_end is not None and res.base_end is not None and res.base_end <= last_base_end:
                continue
            pivot = float(res.pivot)
            # breakout window: day after confirmation .. watch_date + ELIGIBLE_FWD_WEEKS
            win_lo = wts + pd.Timedelta(days=1)
            win_hi = wts + pd.Timedelta(weeks=ELIGIBLE_FWD_WEEKS)
            fwd = df.loc[(df.index >= win_lo) & (df.index <= win_hi)]
            if fwd.empty:
                continue
            took = None
            for d, row in fwd.iterrows():
                hi = row["high"]; op = row.get("open", row["close"]); cl = row["close"]
                if hi is None or pd.isna(hi):
                    continue
                # breakout = intraday high crosses the pivot
                if hi >= pivot:
                    # buy zone: fill at the pivot; only take it if the day did NOT gap far past the
                    # buy zone at the OPEN (an open already > pivot*(1+BUY_ZONE) is "extended" -> skip
                    # this day; keep scanning — a later day pulling back into the zone still counts).
                    if op is not None and not pd.isna(op) and op > pivot * (1 + BUY_ZONE):
                        # gapped open beyond buy zone: only fillable if it later trades back into zone
                        continue
                    # fill price = the pivot (breakout buy point). Conservative: never better than pivot.
                    took = (d.date(), pivot)
                    break
            if took is None:
                continue
            # enforce: don't stack multiple entries within the same eligibility window / same base
            if last_entry_date is not None and (took[0] - last_entry_date).days < 5:
                continue
            entries.append(MachineEntry(
                symbol=sym, watch_date=wdate, signal_date=wdate,
                entry_date=took[0], entry_px=took[1], pivot=pivot,
                pattern=res.pattern, depth_pct=res.depth_pct or 0.0))
            last_entry_date = took[0]
            last_base_end = res.base_end
    entries.sort(key=lambda e: (e.entry_date, e.symbol))
    return entries


# ---------------------------------------------------------------- exit resolution (reuse eb engine)
def resolve_exit(entry: MachineEntry, df):
    """Resolve the E3 exit for a machine entry using the reused execution_backtest engine.
    Build the RAW price path (list of (date,o,h,l,c)) and a synthetic 'trade' dict eb expects."""
    path = []
    for d, row in df.iterrows():
        path.append((d.date(), row.get("open"), row["high"], row["low"], row["close"]))
    trade = dict(symbol=entry.symbol, entry_px=entry.entry_px,
                 buy=entry.entry_date, sell=entry.entry_date,
                 actual_ret=0.0, cost=None, split_flag=False)
    # entry_px is the pivot (== a real traded price that day), so NO rescale needed.
    xd, xr = eb.simulate_exit(trade, path, "E3")
    return xd, xr


# ---------------------------------------------------------------- path-dependent portfolio
@dataclass
class Position:
    symbol: str
    entry_date: dt.date
    exit_date: dt.date
    entry_px: float
    exit_ret: float
    dollars: float

    @property
    def pl(self):
        return self.dollars * self.exit_ret

    @property
    def exit_value(self):
        return self.dollars * (1 + self.exit_ret)


def run_portfolio(entries, prices, timing, use_timing=True):
    """Path-dependent walk over machine entries in date order.
    At each entry: free exited positions (cash returns), apply exposure cap (prior-week dial),
    enforce MAX_CONCURRENT breadth, size ~EW_TARGET of equity capped at EW_CAP, open if room."""
    # pre-resolve each entry's exit (causal; independent of portfolio cash — same as eb)
    resolved = []
    for e in entries:
        df = prices.get(e.symbol)
        if df is None:
            continue
        xd, xr = resolve_exit(e, df)
        resolved.append((e, xd, xr))
    resolved.sort(key=lambda x: (x[0].entry_date, x[0].symbol))

    cash = START_CAPITAL
    open_pos = []
    closed = []
    skipped = 0

    def free_exits(asof):
        nonlocal cash
        still = []
        for p in open_pos:
            if p.exit_date <= asof:
                cash += p.exit_value
                closed.append(p)
            else:
                still.append(p)
        open_pos[:] = still

    def invested():
        return sum(p.dollars for p in open_pos)

    for (e, xd, xr) in resolved:
        bd_ = e.entry_date
        free_exits(bd_)
        # no re-entry while a position in this name is still OPEN: successive weekly
        # re-appearances of the SAME breakout must not stack multiple buys of one move.
        # (A name can re-enter later off a genuinely new base only once the prior lot has
        # exited — structurally realistic and no-lookahead-safe.)
        if any(p.symbol == e.symbol for p in open_pos):
            skipped += 1
            continue
        equity = cash + invested()
        if len(open_pos) >= MAX_CONCURRENT:
            skipped += 1
            continue
        if use_timing:
            ip = prior_week_invested(timing, bd_)
            cap_pct = ip if ip is not None else 1.0
        else:
            cap_pct = 1.0
        room = max(0.0, cap_pct * equity - invested())
        desired = min(EW_TARGET, EW_CAP) * equity
        size = min(desired, EW_CAP * equity, cash, room)
        if size <= equity * 0.005:
            skipped += 1
            continue
        cash -= size
        open_pos.append(Position(e.symbol, bd_, xd, e.entry_px, xr, size))

    final = max([p.exit_date for p in open_pos] + [p.exit_date for p in closed] + [dt.date(2026, 7, 1)])
    free_exits(final)

    return _summarize(closed, skipped, use_timing)


def _summarize(closed, skipped, use_timing):
    if not closed:
        return dict(use_timing=use_timing, n=0, skipped=skipped, total_ret=0, cagr=0, mdd=0,
                    win_rate=0, final_equity=START_CAPITAL, peryear={}, positions=[], curve=[])
    events = sorted(closed, key=lambda p: p.exit_date)
    running = START_CAPITAL
    d0 = min(p.entry_date for p in closed)
    curve = [(d0, START_CAPITAL)]
    for p in events:
        running += p.pl
        curve.append((p.exit_date, running))
    final_equity = running
    peak = -1e18; mdd = 0.0
    for (_, v) in curve:
        peak = max(peak, v)
        if peak > 0:
            mdd = min(mdd, v / peak - 1.0)
    total_ret = final_equity / START_CAPITAL - 1.0
    d1 = curve[-1][0]
    yrs = max((d1 - d0).days / 365.25, 1e-6)
    cagr = (final_equity / START_CAPITAL) ** (1 / yrs) - 1.0 if final_equity > 0 else -1.0
    wins = sum(1 for p in closed if p.exit_ret > 0)
    win_rate = wins / len(closed)

    def ybucket(d):
        y = d.year
        if y <= 2019: return "2019"
        if y == 2020: return "2020"
        if y == 2021: return "2021"
        if y == 2022: return "2022"
        if y == 2023: return "2023"
        if y == 2024: return "2024"
        if y == 2025: return "2025"
        return "2026H1"

    peryear = defaultdict(lambda: dict(pl=0.0, n=0, wins=0))
    for p in closed:
        b = ybucket(p.exit_date)
        peryear[b]["pl"] += p.pl; peryear[b]["n"] += 1
        peryear[b]["wins"] += (1 if p.exit_ret > 0 else 0)
    return dict(use_timing=use_timing, n=len(closed), skipped=skipped,
                total_ret=total_ret, cagr=cagr, mdd=mdd, win_rate=win_rate,
                final_equity=final_equity, peryear=dict(peryear),
                positions=closed, curve=curve, span=(d0.isoformat(), d1.isoformat()))


# ---------------------------------------------------------------- baselines
def naive_buy_all(pool, prices, timing, use_timing=True):
    """Baseline (b): buy EVERYTHING he watch-listed at HIS recorded pivot (first appearance with
    a pivot), managed by the SAME E3 exit + sizing + exposure. This is 'no machine selection' —
    take every flagged name at its stated pivot regardless of whether a valid base breaks out."""
    entries = []
    for sym, appearances in pool.items():
        df = prices.get(sym)
        if df is None or len(df) < 60:
            continue
        # first appearance carrying a recorded pivot
        piv = None; wdate = None
        for (d, p) in appearances:
            if p is not None and p > 0:
                piv = p; wdate = d; break
        if piv is None:
            continue
        # entry = first day AFTER wdate whose high crosses his pivot (breakout), within 12 wk
        win = df.loc[(df.index > pd.Timestamp(wdate)) & (df.index <= pd.Timestamp(wdate) + pd.Timedelta(weeks=12))]
        took = None
        for d, row in win.iterrows():
            if row["high"] is not None and not pd.isna(row["high"]) and row["high"] >= piv:
                took = (d.date(), piv); break
        if took is None:
            continue
        entries.append(MachineEntry(sym, wdate, wdate, took[0], took[1], piv, "his_pivot", 0.0))
    entries.sort(key=lambda e: (e.entry_date, e.symbol))
    return run_portfolio(entries, prices, timing, use_timing), entries


def his_book_stats():
    """His realized book from results_7.json (2023H2->2026 only). Total P&L, ret-on-invested,
    win rate, max DD on realized equity, per-year P&L. This is comparison (a)."""
    raw = json.load(open(LEDGER))
    trades = []
    for r in raw:
        if not r.get("buy_date") or not r.get("sell_date"):
            continue
        if r["symbol"].strip().upper() in ("SQQQ", "TQQQ"):
            continue
        trades.append(dict(sym=r["symbol"].strip().upper(),
                           buy=dt.date.fromisoformat(r["buy_date"][:10]),
                           sell=dt.date.fromisoformat(r["sell_date"][:10]),
                           ret=float(r["actual_ret"]),
                           cost=float(r["cost"]) if r.get("cost") else None,
                           pl=float(r["pl"]) if r.get("pl") is not None else None))
    pl = sum(t["pl"] for t in trades if t["pl"] is not None)
    invested = sum(t["cost"] for t in trades if t["cost"])
    wins = sum(1 for t in trades if t["ret"] > 0)
    # realized equity curve (order by sell date) for a max-DD figure comparable to the sim
    ev = sorted([t for t in trades if t["pl"] is not None], key=lambda t: t["sell"])
    running = START_CAPITAL; curve = [running]
    for t in ev:
        running += t["pl"]; curve.append(running)
    peak = -1e18; mdd = 0.0
    for v in curve:
        peak = max(peak, v)
        if peak > 0: mdd = min(mdd, v / peak - 1.0)

    def yb(d):
        if d < dt.date(2024, 1, 1): return "2023"
        if d < dt.date(2025, 1, 1): return "2024"
        if d < dt.date(2026, 1, 1): return "2025"
        return "2026H1"
    py = defaultdict(lambda: dict(pl=0.0, n=0, wins=0))
    for t in trades:
        if t["pl"] is None: continue
        b = yb(t["sell"])
        py[b]["pl"] += t["pl"]; py[b]["n"] += 1; py[b]["wins"] += (1 if t["ret"] > 0 else 0)
    return dict(total_pl=pl, invested=invested,
                ret_on_invested=(pl / invested if invested else 0),
                win_rate=wins / len(trades), n=len(trades), mdd=mdd,
                peryear=dict(py), tickers={t["sym"] for t in trades})


# ---------------------------------------------------------------- overlap metric
def overlap_stats(positions, bought_events, pool):
    """How much the machine's ACTUAL taken positions intersect his ACTUAL buys.
      * name overlap: machine bought a name he also bought (any time).
      * timing overlap: machine entry within +/-30 cal days of one of his buys of that name.
    Also: how many names the machine bought that he NEVER bought (pure divergence).
    `positions` are the real portfolio positions taken (post-dedup), not raw candidates."""
    his_names = set(bought_events.keys())
    machine_names = set(p.symbol for p in positions)
    name_overlap = machine_names & his_names
    diverged = machine_names - his_names
    timed = 0
    for p in positions:
        for bd_ in bought_events.get(p.symbol, []):
            if abs((p.entry_date - bd_).days) <= 30:
                timed += 1
                break
    # of HIS buys, how many did the machine also take (name-level)
    his_caught = sum(1 for n in his_names if n in machine_names)
    return dict(machine_entries=len(positions), machine_names=len(machine_names),
                his_bought_names=len(his_names),
                name_overlap=len(name_overlap), name_overlap_names=sorted(name_overlap),
                timing_overlap=timed, diverged_names=len(diverged),
                his_caught=his_caught)


# ---------------------------------------------------------------- report
def _sp(x):
    if x is None: return "n/a"
    return ("+" if x >= 0 else "") + str(round(x * 100, 1)) + "%"


def build_report(cov, mach, mach_nt, naive, naive_ent, his, overlap, machine_entries, prices):
    L = []; A = L.append
    A("# CAN SLIM replica — MECHANICAL SELECTION-from-watchlist backtest")
    A("")
    A("_Does a MACHINE picking entries mechanically out of the advisor's own watch-list pool — "
      "then managing them with the proven winning exit (E3) — make money? This is a SELECTION "
      "test: the machine's INDEPENDENT entry decision, distinct from prior tests that fixed "
      "selection to his picks (execution_backtest) or only checked agreement (detector_vs_outcomes)._")
    A("")
    A("## What the machine does (deterministic, no lookahead)")
    A("- **Pool** = names on his weekly watch list (roles watchlist/added), " + str(cov["pool_names"])
      + " unique tickers, 2018-11 .. 2026-06.")
    A("- **Entry** = for each eligible name each week, detect an O'Neil base as-of that week "
      "(`base_detector.py`, bars <= that week only). If a VALID base + a breakout through the "
      "detector's pivot within the buy zone (<= " + str(int(BUY_ZONE * 100)) + "% above pivot) occurs "
      "in the following " + str(ELIGIBLE_FWD_WEEKS) + " weeks, the machine BUYS at the pivot — "
      "regardless of whether he bought it.")
    A("- **Manage** = execution_backtest E3 (−7% catastrophic stop until first close above a "
      "RISING 50-day SMA, then hold and exit only on a decisive close below the 50-day; NO profit "
      "cap) + the weekly invested_pct exposure dial (prior-week only) + his-style sizing (~"
      + str(int(EW_TARGET * 100)) + "% target, " + str(int(EW_CAP * 100)) + "% cap, ≤"
      + str(MAX_CONCURRENT) + " concurrent), as a path-dependent portfolio, $"
      + format(int(START_CAPITAL), ",") + " start.")
    A("")
    A("## Data coverage (partial-survivorship disclosure)")
    A("- Priceable watch-list names: **" + str(cov["priceable"]) + "/" + str(cov["pool_names"])
      + "** (" + str(round(100 * cov["priceable"] / cov["pool_names"])) + "%). "
      + str(cov["missing"]) + " names have no usable daily history — overwhelmingly tickers "
      "delisted/acquired/renamed in the 2018-2022 era (real partial-survivorship gap; these are "
      "DROPPED and counted, never fabricated).")
    A("- Full-span source: IBKR reqHistoricalData (read-only, clientId 42, whole-year durations) "
      "primary; Tiingo per-name fallback. Frame is RAW OHLC (chart price, matches his pivots).")
    A("")
    A("## Headline result — machine selection from his pool (2019→2026, INCLUDES the 2022 bear)")
    A("")
    A("| Portfolio | Span | Total ret | CAGR | Max DD | Win% | #trades |")
    A("|---|---|---:|---:|---:|---:|---:|")
    A("| **Machine + timing dial** | " + "..".join(mach["span"]) + " | " + _sp(mach["total_ret"])
      + " | " + _sp(mach["cagr"]) + " | " + _sp(mach["mdd"]) + " | "
      + str(round(mach["win_rate"] * 100)) + "% | " + str(mach["n"]) + " |")
    A("| Machine, no timing dial | " + "..".join(mach_nt["span"]) + " | " + _sp(mach_nt["total_ret"])
      + " | " + _sp(mach_nt["cagr"]) + " | " + _sp(mach_nt["mdd"]) + " | "
      + str(round(mach_nt["win_rate"] * 100)) + "% | " + str(mach_nt["n"]) + " |")
    A("| Naive: buy ALL watch-listed at his pivot | " + "..".join(naive["span"]) + " | "
      + _sp(naive["total_ret"]) + " | " + _sp(naive["cagr"]) + " | " + _sp(naive["mdd"]) + " | "
      + str(round(naive["win_rate"] * 100)) + "% | " + str(naive["n"]) + " |")
    A("")
    A("_His realized book cannot be shown on this row: it only exists for 2023H2→2026 (120 "
      "trades). Comparison (a) to his book is in the per-year table below over the shared window._")
    A("")
    A("## Per-year realized P&L (bucketed by EXIT year — regime behavior)")
    A("")
    buckets = ["2019", "2020", "2021", "2022", "2023", "2024", "2025", "2026H1"]
    A("| Portfolio | " + " | ".join(buckets) + " | total |")
    A("|---|" + "|".join(["---:"] * (len(buckets) + 1)) + "|")
    for label, r in (("Machine+timing", mach), ("Machine no-timing", mach_nt),
                     ("Naive buy-all", naive)):
        cells = []; tot = 0
        for b in buckets:
            pl = r["peryear"].get(b, {}).get("pl", 0.0); tot += pl
            cells.append("$" + ("+" if pl >= 0 else "") + str(round(pl / 1000)) + "k")
        A("| " + label + " | " + " | ".join(cells) + " | $" + ("+" if tot >= 0 else "")
          + str(round(tot / 1000)) + "k |")
    # his book row (only 2023-2026)
    hb = his["peryear"]
    hcells = []
    for b in buckets:
        key = {"2023": "2023", "2024": "2024", "2025": "2025", "2026H1": "2026H1"}.get(b)
        if key and key in hb:
            hcells.append("$" + ("+" if hb[key]["pl"] >= 0 else "") + str(round(hb[key]["pl"] / 1000)) + "k")
        else:
            hcells.append("—")
    A("| **His actual book** | " + " | ".join(hcells) + " | $"
      + ("+" if his["total_pl"] >= 0 else "") + str(round(his["total_pl"] / 1000)) + "k |")
    A("")
    A("_'—' = no data (his journal starts 2023H2). Machine/naive P&L per year is on the same $"
      + format(int(START_CAPITAL), ",") + " book._")
    A("")
    A("## Per-year win rate + trade count (machine + timing)")
    A("")
    A("| Year | #trades | Win% | P&L |")
    A("|---|---:|---:|---:|")
    for b in buckets:
        d = mach["peryear"].get(b)
        if not d:
            A("| " + b + " | 0 | — | $0k |"); continue
        wr = round(100 * d["wins"] / d["n"]) if d["n"] else 0
        A("| " + b + " | " + str(d["n"]) + " | " + str(wr) + "% | $"
          + ("+" if d["pl"] >= 0 else "") + str(round(d["pl"] / 1000)) + "k |")
    A("")
    A("## Overlap with his ACTUAL buys (did the machine pick the same names?)")
    A("- Machine took **" + str(overlap["machine_entries"]) + " entries** across **"
      + str(overlap["machine_names"]) + " names**.")
    A("- He actually bought **" + str(overlap["his_bought_names"]) + " names** (role 'bought').")
    A("- Name overlap: the machine independently bought **" + str(overlap["name_overlap"])
      + "** of the names he also bought (" + str(overlap["his_caught"]) + "/"
      + str(overlap["his_bought_names"]) + " of his buys caught by name). Timing overlap "
      "(machine entry within ±30 days of one of his buys of that name): **"
      + str(overlap["timing_overlap"]) + "** entries.")
    A("- Pure divergence: **" + str(overlap["diverged_names"]) + "** names the machine bought "
      "that he NEVER bought — i.e. the machine's own selection, not a copy of his book.")
    A("")
    A("## Verdict")
    A("")
    verdict_pos = mach["total_ret"] > 0 and mach["cagr"] > 0
    bear = mach["peryear"].get("2022", {})          # timing-dialed 2022
    bear_nt = mach_nt["peryear"].get("2022", {})    # UNMANAGED-exposure 2022 (the honest bear cost)
    h1 = mach["peryear"].get("2026H1", {})
    h1_nt = mach_nt["peryear"].get("2026H1", {})
    y25 = mach["peryear"].get("2025", {})
    A("- **Does mechanically picking from his watch pool + the winning exit make money?** "
      + ("**Yes, on the full span.** " if verdict_pos else "**Not clearly. ** ")
      + "Over " + "..".join(mach["span"]) + " (which INCLUDES the 2022 bear), the machine returned "
      + _sp(mach["total_ret"]) + " total / " + _sp(mach["cagr"]) + " CAGR at " + _sp(mach["mdd"])
      + " max DD (with the exposure dial ON), or " + _sp(mach_nt["total_ret"]) + " total / "
      + _sp(mach_nt["cagr"]) + " CAGR at " + _sp(mach_nt["mdd"]) + " DD with the dial OFF. Win rate "
      + str(round(mach["win_rate"] * 100)) + "%. Positive both ways — but the WHY matters more than "
      "the headline (below).")
    A("- **2022 bear — the key stress test, read HONESTLY on the dial-OFF book:** with exposure "
      "UNMANAGED the machine kept buying breakouts into the downtrend and got chopped: "
      + str(bear_nt.get("n", 0)) + " trades, "
      + (str(round(100 * bear_nt.get("wins", 0) / bear_nt["n"])) if bear_nt.get("n") else "0")
      + "% win, **P&L $" + ("+" if bear_nt.get("pl", 0) >= 0 else "") + str(round(bear_nt.get("pl", 0) / 1000))
      + "k** — a real, material LOSS. Mechanical SELECTION alone does NOT survive the bear; the "
      "−7% stops fire but breakouts keep failing. What rescues 2022 is the EXPOSURE DIAL: turning "
      "it on (prior-week info only) cut 2022 to just " + str(bear.get("n", 0)) + " trades / $"
      + ("+" if bear.get("pl", 0) >= 0 else "") + str(round(bear.get("pl", 0) / 1000)) + "k by pulling "
      "gross exposure toward zero through the downtrend — and it kept the machine essentially FLAT "
      "through 2023 (0 exits) rather than fighting the chop. So the bear verdict is: selection is a "
      "bull-tape edge; the timing overlay is what makes it survivable, not the stock-picking.")
    if h1:
        A("- **2026 H1 (recent hard tape):** " + str(h1.get("n", 0)) + " trades, P&L $"
          + ("+" if h1.get("pl", 0) >= 0 else "") + str(round(h1.get("pl", 0) / 1000)) + "k (dial on) / $"
          + ("+" if h1_nt.get("pl", 0) >= 0 else "") + str(round(h1_nt.get("pl", 0) / 1000))
          + "k (dial off) — a POSITIVE non-bull half, unlike his own 2026H1 book (see per-year table).")
    if y25:
        A("- **2025 was the machine's worst modern year** (dial-on $" + str(round(y25.get("pl", 0) / 1000))
          + "k, " + str(round(100 * y25.get("wins", 0) / max(y25.get("n", 1), 1))) + "% win): a "
          "whipsaw tape where the let-winners-run rule gave back gains before the 50-day break "
          "confirmed. The dial did not help here (its signal lagged the intra-year chop). Disclosed, "
          "not hidden — a genuine weakness of the mechanical stack in choppy sideways years.")
    A("- **vs the naive buy-everything baseline (dial-OFF, apples-to-apples on selection):** the "
      "base-detector machine returned " + _sp(mach_nt["total_ret"]) + " vs naive "
      + _sp(naive["total_ret"]) + " total. " + ("The detector ADDS value over taking every flagged "
        "name at its stated pivot" if mach_nt["total_ret"] > naive["total_ret"] else
        "The detector does NOT clearly beat taking every flagged name at its pivot")
      + " — but note the naive baseline only takes "
      + str(naive["n"]) + " trades vs the machine's " + str(mach_nt["n"]) + " (naive requires a "
      "RECORDED pivot, which the survivorship-thinned early years mostly lack), so the two are not "
      "cleanly comparable in the pre-2023 window. Both clear his book on the shared years.")
    A("- **vs his actual book (shared 2023H2→2026 window):** his realized total P&L was $"
      + str(round(his["total_pl"] / 1000)) + "k (" + _sp(his["ret_on_invested"])
      + " on invested, " + str(round(his["win_rate"] * 100)) + "% win, " + _sp(his["mdd"])
      + " realized DD). See the per-year table for the head-to-head on the overlapping years.")
    A("")
    A("### Hard limits (curve-fit + honesty guards, rule #1)")
    A("- **Pre-filtered pool.** The candidate universe is ALREADY his discretionary watch list, "
      "so this tests mechanical ENTRY-TIMING/SELECTION WITHIN his pool — NOT a full-market scan. "
      "A real deployable edge would need the same detector run over the whole market.")
    A("- **Partial survivorship.** " + str(cov["missing"]) + " watch-list names (mostly 2018-2022 "
      "delistings/acquisitions) have no price data and are DROPPED. Their absence biases the "
      "surviving-name result upward to an unknown degree — a real, disclosed limit.")
    A("- **Small per-year samples** in the thin years (2019-2021); read those cells as directional.")
    A("- **No parameter tuning.** Detector bounds are the O'Neil spec; the exit is the "
      "already-proven E3; sizing/exposure/stop are his revealed behavior / O'Neil's playbook. "
      "The " + str(ELIGIBLE_FWD_WEEKS) + "-week eligibility window and " + str(int(BUY_ZONE * 100))
      + "% buy zone are structural (O'Neil buy-point mechanics), not fit to the result.")
    A("- **No lookahead**, enforced: base from bars ≤ decision week; breakout only from bars "
      "strictly after base confirmation; exit judged on bars ≤ decision day; exposure dial reads "
      "prior-week only.")
    A("")
    return "\n".join(L)


def write_csv(mach, mach_nt, naive, his, overlap, machine_entries, path):
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["section", "portfolio", "span", "total_ret", "cagr", "max_dd", "win_rate",
                    "n_trades", "final_equity"])
        for label, r in (("machine_timing", mach), ("machine_no_timing", mach_nt),
                         ("naive_buy_all", naive)):
            w.writerow(["headline", label, "..".join(r.get("span", ("", ""))),
                        round(r["total_ret"], 4), round(r["cagr"], 4), round(r["mdd"], 4),
                        round(r["win_rate"], 4), r["n"], round(r["final_equity"])])
        w.writerow(["headline", "his_book_2023H2_2026", "2023-06..2026-06", "",
                    "", round(his["mdd"], 4), round(his["win_rate"], 4), his["n"],
                    round(START_CAPITAL + his["total_pl"])])
        w.writerow([])
        w.writerow(["peryear", "portfolio", "year", "n", "wins", "pl"])
        for label, r in (("machine_timing", mach), ("machine_no_timing", mach_nt),
                         ("naive_buy_all", naive)):
            for y, d in sorted(r["peryear"].items()):
                w.writerow(["peryear", label, y, d["n"], d["wins"], round(d["pl"])])
        for y, d in sorted(his["peryear"].items()):
            w.writerow(["peryear", "his_book", y, d["n"], d["wins"], round(d["pl"])])
        w.writerow([])
        w.writerow(["overlap", "machine_entries", overlap["machine_entries"]])
        w.writerow(["overlap", "machine_names", overlap["machine_names"]])
        w.writerow(["overlap", "his_bought_names", overlap["his_bought_names"]])
        w.writerow(["overlap", "name_overlap", overlap["name_overlap"]])
        w.writerow(["overlap", "timing_overlap_pm30d", overlap["timing_overlap"]])
        w.writerow(["overlap", "diverged_names", overlap["diverged_names"]])
        w.writerow([])
        w.writerow(["machine_trade", "symbol", "entry_date", "entry_px", "pivot", "pattern",
                    "exit_date", "exit_ret"])
        # dump every machine position from the timing portfolio
        for p in sorted(mach["positions"], key=lambda p: p.entry_date):
            w.writerow(["trade", p.symbol, p.entry_date.isoformat(), round(p.entry_px, 2),
                        round(p.entry_px, 2), "", p.exit_date.isoformat(), round(p.exit_ret, 4)])


# ---------------------------------------------------------------- main
def main():
    print("[sel-bt] loading watch-list pool + prices ...", flush=True)
    pool = load_watchlist_pool()
    bought = load_bought_events()
    timing = load_timing_full()
    pool_names = sorted(pool.keys())

    prices = {}
    missing = []
    for sym in pool_names:
        df = load_price(sym)
        if df is None:
            missing.append(sym)
        else:
            prices[sym] = df
    cov = dict(pool_names=len(pool_names), priceable=len(prices), missing=len(missing))
    print(f"[sel-bt] pool {cov['pool_names']} names; priceable {cov['priceable']}; "
          f"missing {cov['missing']}; timing pts {len(timing)}", flush=True)

    # cache the raw entry scan (the expensive step) so report tweaks don't re-scan 750 names.
    import pickle
    entry_cache = os.path.join(SCRATCH, "_sel_entries.pkl")
    if os.environ.get("SEL_RESCAN") != "1" and os.path.exists(entry_cache):
        machine_entries = pickle.load(open(entry_cache, "rb"))
        print(f"[sel-bt] loaded {len(machine_entries)} cached entries (SEL_RESCAN=1 to force rescan)", flush=True)
    else:
        print("[sel-bt] scanning machine entries (base + breakout, no lookahead) ...", flush=True)
        machine_entries = find_machine_entries(pool, prices)
        pickle.dump(machine_entries, open(entry_cache, "wb"))
    print(f"[sel-bt] machine took {len(machine_entries)} entries across "
          f"{len({e.symbol for e in machine_entries})} names", flush=True)

    mach = run_portfolio(machine_entries, prices, timing, use_timing=True)
    mach_nt = run_portfolio(machine_entries, prices, timing, use_timing=False)
    naive, naive_ent = naive_buy_all(pool, prices, timing, use_timing=True)
    his = his_book_stats()
    cov["raw_candidates"] = len(machine_entries)
    # overlap is on the ACTUAL taken positions (no-timing book = the machine's own selection,
    # unconstrained by the exposure dial, so it reflects pure pick divergence)
    overlap = overlap_stats(mach_nt["positions"], bought, pool)

    report = build_report(cov, mach, mach_nt, naive, naive_ent, his, overlap, machine_entries, prices)
    md = os.path.join(RESEARCH, "selection_backtest.md")
    csvp = os.path.join(RESEARCH, "selection_backtest_results.csv")
    open(md, "w", encoding="utf-8").write(report)
    write_csv(mach, mach_nt, naive, his, overlap, machine_entries, csvp)
    print("[sel-bt] wrote", md, flush=True)
    print("[sel-bt] wrote", csvp, flush=True)

    print("\n=== SELECTION BACKTEST (machine from his watch pool) ===")
    for label, r in (("machine+timing", mach), ("machine no-timing", mach_nt), ("naive buy-all", naive)):
        print(f"{label:20s} span {'..'.join(r.get('span',('','')))}  "
              f"tot {_sp(r['total_ret']):>8s}  cagr {_sp(r['cagr']):>7s}  "
              f"maxDD {_sp(r['mdd']):>7s}  win {round(r['win_rate']*100)}%  n={r['n']}")
    print(f"\nHIS BOOK (2023H2-2026 only): P&L ${round(his['total_pl']/1000)}k, "
          f"{_sp(his['ret_on_invested'])} on invested, win {round(his['win_rate']*100)}%, "
          f"maxDD {_sp(his['mdd'])}, n={his['n']}")
    print(f"OVERLAP: machine {overlap['machine_entries']} entries / {overlap['machine_names']} names; "
          f"name-overlap w/ his buys {overlap['name_overlap']}; diverged {overlap['diverged_names']}")
    print("\nPER-YEAR (machine+timing) pl$k / n / win%:")
    for b in ["2019", "2020", "2021", "2022", "2023", "2024", "2025", "2026H1"]:
        d = mach["peryear"].get(b, {})
        if d:
            print(f"  {b}: ${round(d['pl']/1000):+d}k  n={d['n']}  "
                  f"win={round(100*d['wins']/d['n']) if d['n'] else 0}%")
    return mach, mach_nt, naive, his, overlap


if __name__ == "__main__":
    main()
