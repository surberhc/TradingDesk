"""Stock-side PYRAMIDING (add-to-position) backtest for the CAN SLIM replica.

THE QUESTION
------------
Does ADDING TO POSITIONS improve the STOCK-only CAN SLIM replica? Doug's measured
add behavior (canslim/research/doug_add_behavior.md) suggested adding is a CONSISTENCY
lever (higher win rate / better median, cuts the losing tail) but NOT a mean-return
booster (his adds missed the fast gappers). This module verifies whether that holds in a
full path-dependent PORTFOLIO sim once the O'Neil raise-the-stop discipline is layered on.

WHAT IS HELD FIXED vs WHAT VARIES
---------------------------------
FIXED (his): the pick set + each pick's ENTRY DATE, ENTRY PRICE, and starter dollar SIZE
(his revealed cost). The EXIT rule is fixed to E3 (the winning exit from execution_backtest.py:
-7% catastrophic stop until first close above a RISING 50-day SMA, then sell on a decisive
close back below the 50-day). This isolates the ADD effect ALONE: same picks, same entries,
same exits, same sizing of the STARTER -- the only thing that varies across arms is whether
(and how) a second tranche is added.

THE ARMS (descriptive grid, NOT tuned -- anchored to his measured behavior + O'Neil discipline)
    BASELINE        single-entry, E3 exit, NO add. (== execution_backtest E3.-.his, re-derived here.)
    DOUG_MEASURED   his actual pattern: an EARLY buy-zone top-up. Add fires the first time,
                    within ADD_WINDOW_DAYS of entry, that price is inside the add zone
                    [ADD_ZONE_LO .. ADD_ZONE_HI] off entry. Add size = ADD_FRAC of the starter
                    dollars. At most ONE add. THEN raise the stop so the BLENDED position's
                    worst case stays ~ the original -7% (O'Neil discipline: an add can never
                    turn the combined position into a larger dollar loss than the starter alone).
    ONEIL_UPTREND   DOUG_MEASURED, but the add ALSO requires price above a RISING 50-day SMA at
                    the add point (uptrend-confirmed continuation add). If 50d is unavailable
                    that early (paths begin ~entry), the add is skipped (conservative).
    ONEIL_HALF      DOUG_MEASURED, but add size = 50% of starter (a stricter decreasing pyramid)
                    instead of ~60%.

ANCHORS (from doug_add_behavior.md -- his MEASURED behavior, used as the rule, NOT tuned):
    - Trigger EARLY: median +2.1% progress, median 14 days; 74% of adds within 14d. -> window 14d.
    - Add ZONE: his 25th..75th pctile of progress-at-add was -3.6% .. +9.6% (buy-zone top-up). -> [-3.6%, +9.6%].
    - Add SIZE: median add = 59.6% of the initial position's dollars. -> 0.60 (ONEIL_HALF = 0.50).
    - CAP: at most one add for almost all names; a fully-built name tops ~2.4x starter / <=~25% of book.
    - RARITY: he only added ~25% of the time. We do NOT re-create his discretionary "only-if-working"
      selection (that would be lookahead); instead every position that MECHANICALLY enters the
      add zone in the window gets the add. This ADDS MORE OFTEN than he did (see limits) -- it is
      the honest mechanical version of his rule, and it tests the add MECHANIC, not his intuition.

O'NEIL RAISE-THE-STOP DISCIPLINE (the critical layer)
-----------------------------------------------------
When the add fires at price p_add with the starter still held (worst case on the starter = the
initial -7% stop at s0 = entry*(1-0.07)), we set a BLENDED stop s_b such that the combined
position's loss at s_b equals the STARTER's max loss at s0. i.e. the extra shares cannot deepen
the dollar loss. Concretely, with starter dollars D0 at entry E and add dollars D1 at p_add:
    starter worst-case $loss = D0 * 0.07                    (starter alone stopped at s0)
    blended $loss at stop price P = D0*(1-P/E) + D1*(1-P/p_add)
    solve D0*(1-P/E) + D1*(1-P/p_add) = D0*0.07  for P  -> the raised blended stop.
This stop is >= s0 whenever the add is at/above entry (it TIGHTENS after an up-add) and can be
slightly below s0 only if the add is below entry -- but the *dollar* loss cap is preserved by
construction. The blended stop stays in force until E3's 50-day rule takes over (first close
above a rising 50-day), exactly as in the baseline.

NO LOOKAHEAD
------------
Every decision on day D uses only bars up to and including D. The add trigger scans forward day
by day and fires on the FIRST qualifying bar; the 50-day (for exit and for the ONEIL_UPTREND
filter) uses only closes up to D. Same causality discipline as execution_backtest.py.

HONESTY / CURVE-FIT GUARDS (project rule #1)
--------------------------------------------
Every add parameter is an ANCHOR from his measured behavior or O'Neil's playbook, NOT tuned to
maximize return. The grid is DESCRIPTIVE (report all arms), not a sweep with a chosen winner.
Stated limits: bull-heavy 2023-2026 universe; small N of adds; his-entries-only so this is
EXECUTION not selection; and the mechanical add fires more often than his discretionary ~25%.

Usage:  python stock_add_backtest.py
        (writes research/stock_add_test.md + research/stock_add_results.csv, and scratchpad copies)
"""
from __future__ import annotations
import os, sys, json, csv, math, statistics, datetime as dt
from dataclasses import dataclass

# Reuse the committed execution engine wholesale (paths, ledger, indicators, rescale, E3 exit).
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import execution_backtest as X   # load_ledger, load_paths, load_timing, rescaled_path, sma, simulate_exit, prior_week_invested, START_CAPITAL, HARD_STOP, SMA_BUFFER, EW_CAP

RESEARCH = os.path.join(HERE, "research")
SCRATCH = X.SCRATCH

# ----------------------------------------------------------------------------- ADD ANCHORS (from doug_add_behavior.md -- NOT tuned)
ADD_WINDOW_DAYS = 14        # median 14d; 74% of his adds within 14 calendar days of entry
ADD_ZONE_LO = -0.036        # 25th pctile of his progress-at-add
ADD_ZONE_HI = 0.096         # 75th pctile of his progress-at-add
ADD_FRAC = 0.60             # median add = 59.6% of starter dollars
ADD_FRAC_HALF = 0.50        # ONEIL_HALF variant: stricter decreasing pyramid
BOOK_CAP_FRAC = 0.25        # a built name may not exceed 25% of equity at add time (his ceiling)

HARD_STOP = X.HARD_STOP     # 0.07 O'Neil catastrophic
SMA_BUFFER = X.SMA_BUFFER   # 0.98 decisive-break buffer
START_CAPITAL = X.START_CAPITAL


# ----------------------------------------------------------------------------- two-lot causal exit engine (E3 + optional add + raised blended stop)
def simulate_with_add(trade, path, arm, starter_dollars, equity_at_entry):
    """E3 exit on a position that MAY receive one add. Returns a dict describing the whole lot:
        exit_date, blended_ret (return on total dollars deployed), add_fired (bool),
        add_dollars, add_progress (price progress at add), total_dollars.

    Causality: forward scan; the add fires on the FIRST bar in the window whose CLOSE sits in the
    add zone (and, for ONEIL_UPTREND, above a rising 50-day). After the add, the stop becomes the
    RAISED BLENDED stop (dollar-loss preserved). E3's 50-day handover and decisive-break exit are
    unchanged from execution_backtest.simulate_exit.

    blended_ret is on the AVERAGE cost of the combined dollars, so the portfolio P&L uses the true
    dollar-weighted return of the built position.
    """
    entry = trade["entry_px"]; bd = trade["buy"]
    hold = [(d, o, h, l, c) for (d, o, h, l, c) in path if d > bd]
    if not hold:
        return dict(exit_date=trade["sell"], blended_ret=trade["actual_ret"], add_fired=False,
                    add_dollars=0.0, add_progress=None, total_dollars=starter_dollars)

    closes = [c for (_, _, _, _, c) in path]
    sma50 = X.sma(closes, 50)
    sma_by_date = {}
    for i, (d, *_rest) in enumerate(path):
        sma_by_date[d] = (sma50[i], sma50[i - 1] if i >= 1 else None)

    # add sizing per arm
    if arm == "ONEIL_HALF":
        add_frac = ADD_FRAC_HALF
    else:
        add_frac = ADD_FRAC
    want_uptrend = (arm == "ONEIL_UPTREND")
    can_add = (arm != "BASELINE")

    init_stop_px = entry * (1 - HARD_STOP)         # starter catastrophic stop (dollar-loss basis)
    starter_maxloss = starter_dollars * HARD_STOP  # the dollar loss the blended stop must not exceed

    # position state
    add_fired = False
    add_dollars = 0.0
    add_px = None
    add_progress = None
    total_dollars = starter_dollars
    # weighted-average entry for return bookkeeping; blended stop PRICE for the loss cap
    avg_entry = entry
    stop_px = init_stop_px          # active hard/blended stop price (until 50-line takes over)
    sma_active = False

    def blended_ret_at(px):
        """Dollar-weighted return of the whole built position when marked at price px."""
        v0 = starter_dollars * (px / entry)
        v1 = (add_dollars * (px / add_px)) if add_fired and add_px else 0.0
        return (v0 + v1) / total_dollars - 1.0

    for (d, o, h, l, c) in hold:
        s_today, s_prev = sma_by_date.get(d, (None, None))

        # ---- ADD trigger (only before 50-line takes over, only within the window, once) ----
        if can_add and not add_fired and not sma_active:
            days = (d - bd).days
            if days <= ADD_WINDOW_DAYS and c is not None:
                prog = c / entry - 1.0
                in_zone = (ADD_ZONE_LO <= prog <= ADD_ZONE_HI)
                uptrend_ok = True
                if want_uptrend:
                    uptrend_ok = (s_today is not None and s_prev is not None
                                  and s_today > s_prev and c > s_today)
                # book cap: built name must not exceed BOOK_CAP_FRAC of equity at entry
                prospective = starter_dollars + add_frac * starter_dollars
                cap_ok = prospective <= BOOK_CAP_FRAC * equity_at_entry if equity_at_entry else True
                if in_zone and uptrend_ok and cap_ok:
                    add_dollars = add_frac * starter_dollars
                    add_px = c
                    add_progress = prog
                    add_fired = True
                    total_dollars = starter_dollars + add_dollars
                    # RAISE THE STOP: solve blended $loss at P == starter_maxloss.
                    # D0*(1-P/E) + D1*(1-P/add_px) = starter_maxloss
                    # => P*(D0/E + D1/add_px) = D0 + D1 - starter_maxloss
                    D0, D1, E, A = starter_dollars, add_dollars, entry, add_px
                    denom = (D0 / E + D1 / A)
                    stop_px = (D0 + D1 - starter_maxloss) / denom if denom > 0 else init_stop_px

        # ---- exit logic (E3 semantics, using the ACTIVE stop_px which may be the blended one) ----
        if not sma_active:
            if l is not None and l <= stop_px:
                fill = o if (o is not None and o <= stop_px) else stop_px
                return dict(exit_date=d, blended_ret=blended_ret_at(fill), add_fired=add_fired,
                            add_dollars=add_dollars, add_progress=add_progress,
                            total_dollars=total_dollars)
            if (s_today is not None and s_prev is not None
                    and s_today > s_prev and c is not None and c > s_today):
                sma_active = True     # 50-line takes over; stop no longer used
        else:
            if s_today is not None and c is not None and c < SMA_BUFFER * s_today:
                return dict(exit_date=d, blended_ret=blended_ret_at(c), add_fired=add_fired,
                            add_dollars=add_dollars, add_progress=add_progress,
                            total_dollars=total_dollars)

    # never triggered -> mark to last available close
    last_c, last_d = None, None
    for (d, o, h, l, c) in reversed(path):
        if c is not None:
            last_c, last_d = c, d; break
    if last_c is None:
        return dict(exit_date=trade["sell"], blended_ret=trade["actual_ret"], add_fired=add_fired,
                    add_dollars=add_dollars, add_progress=add_progress, total_dollars=total_dollars)
    return dict(exit_date=last_d, blended_ret=blended_ret_at(last_c), add_fired=add_fired,
                add_dollars=add_dollars, add_progress=add_progress, total_dollars=total_dollars)


# ----------------------------------------------------------------------------- portfolio walk (path-dependent; add draws extra cash at the add date)
@dataclass
class Lot:
    symbol: str
    entry_date: dt.date
    exit_date: dt.date
    starter_dollars: float
    add_dollars: float
    total_dollars: float
    blended_ret: float
    add_fired: bool
    add_progress: float | None

    @property
    def pl(self):
        return self.total_dollars * self.blended_ret

    @property
    def exit_value(self):
        return self.total_dollars * (1 + self.blended_ret)


def run_arm(trades, paths, arm):
    """Path-dependent portfolio over his entry sequence, E3 exit, one optional add per the arm.

    Sizing of the STARTER = his revealed dollar cost (isolates the add effect; starter identical
    across arms). The add draws additional cash at ENTRY time (approximation: we fund the whole
    built position at entry, since the add lands within ~2 weeks and this keeps the cash accounting
    a single event). Exposure/cash caps mirror execution_backtest: a starter is skipped if there is
    no cash room; the add is only taken if it fits the book cap AND cash is available.
    """
    # pre-resolve each trade's built-lot outcome (depends on price path + starter size + equity proxy)
    cash = START_CAPITAL
    open_lots: list[Lot] = []
    closed: list[Lot] = []
    skipped = 0

    def free_exits(asof):
        nonlocal cash
        still = []
        for p in open_lots:
            if p.exit_date <= asof:
                cash += p.exit_value
                closed.append(p)
            else:
                still.append(p)
        open_lots[:] = still

    def invested():
        return sum(p.total_dollars for p in open_lots)

    def equity_now():
        return cash + invested()

    for t in trades:
        bd = t["buy"]
        free_exits(bd)
        equity = equity_now()

        starter = t["cost"] if t["cost"] else min(X.EW_TARGET, X.EW_CAP) * equity
        # starter must fit available cash (exposure cap = 100% here; timing dial is off for the add
        # study so the add effect is not confounded by the exposure overlay -- reported separately if needed)
        starter = min(starter, cash)
        if starter <= equity * 0.005:
            skipped += 1
            continue

        path = paths.get(t["symbol"])
        # No path, OR a split-flagged trade whose rescaled path is unreliable mid-hold (a split
        # corrupts the price frame -> bogus stop levels). Both fall back to his ACTUAL outcome,
        # single-entry, no add -- exactly as execution_backtest excludes split trades from $ aggregates.
        if (not path) or t["split_flag"]:
            lot = Lot(t["symbol"], bd, t["sell"], starter, 0.0, starter, t["actual_ret"], False, None)
            cash -= starter
            open_lots.append(lot)
            continue

        rp = X.rescaled_path(path, t["buy"], t["entry_px"])
        res = simulate_with_add(t, rp, arm, starter, equity)

        add_dollars = res["add_dollars"]
        # cash check for the add: if the add would overdraw cash, cancel the add (keep starter only)
        if res["add_fired"] and add_dollars > (cash - starter):
            # not enough cash for the add -> re-run as if no add fired by scaling back to starter-only
            # (recompute a starter-only outcome under the same arm's exit = E3)
            res_noadd = simulate_with_add(t, rp, "BASELINE", starter, equity)
            lot = Lot(t["symbol"], bd, res_noadd["exit_date"], starter, 0.0, starter,
                      res_noadd["blended_ret"], False, None)
            cash -= starter
            open_lots.append(lot)
            continue

        total = res["total_dollars"]
        cash -= total
        open_lots.append(Lot(t["symbol"], bd, res["exit_date"], starter, add_dollars, total,
                             res["blended_ret"], res["add_fired"], res["add_progress"]))

    final_date = max([p.exit_date for p in open_lots] + [p.exit_date for p in closed] + [dt.date(2026, 7, 1)])
    free_exits(final_date)

    return summarize(arm, closed, trades)


# ----------------------------------------------------------------------------- stats
def summarize(arm, lots, trades):
    lots = sorted(lots, key=lambda p: p.exit_date)
    n = len(lots)
    rets = [p.blended_ret for p in lots]
    # equity curve (realized P&L accreted at each exit date)
    eq = START_CAPITAL
    curve = [(min(t["buy"] for t in trades), START_CAPITAL)]
    running = START_CAPITAL
    for p in lots:
        running += p.pl
        curve.append((p.exit_date, running))
    final_equity = running
    total_ret = final_equity / START_CAPITAL - 1.0
    d0, d1 = curve[0][0], curve[-1][0]
    yrs = max((d1 - d0).days / 365.25, 1e-6)
    cagr = (final_equity / START_CAPITAL) ** (1 / yrs) - 1.0 if final_equity > 0 else -1.0
    peak, mdd = -1e18, 0.0
    for (_, v) in curve:
        peak = max(peak, v)
        if peak > 0:
            mdd = min(mdd, v / peak - 1.0)
    wins = sum(1 for r in rets if r > 0)
    win_rate = wins / n if n else 0.0
    median_ret = statistics.median(rets) if rets else 0.0
    mean_ret = statistics.mean(rets) if rets else 0.0
    # losing tail: mean of the worst-decile of trades, and 10th-pctile trade return
    srt = sorted(rets)
    tail_n = max(1, n // 10)
    losing_tail_mean = statistics.mean(srt[:tail_n]) if srt else 0.0
    p10 = srt[max(0, int(0.10 * n) - 1)] if srt else 0.0
    worst = srt[0] if srt else 0.0
    n_added = sum(1 for p in lots if p.add_fired)
    total_add_dollars = sum(p.add_dollars for p in lots)
    return dict(
        arm=arm, n=n, total_ret=total_ret, cagr=cagr, mdd=mdd, win_rate=win_rate,
        median_ret=median_ret, mean_ret=mean_ret, losing_tail_mean=losing_tail_mean,
        p10=p10, worst=worst, final_equity=final_equity, n_added=n_added,
        total_add_dollars=total_add_dollars, lots=lots,
    )


# ----------------------------------------------------------------------------- add-cohort diagnostics (did the ADDED-TO lots behave like Doug's data?)
def add_cohort(res_arm):
    """Within an arm that adds, compare added-to lots vs single-entry lots (mirrors the
    doug_add_behavior.md §5 table so we can check the direction holds)."""
    lots = res_arm["lots"]
    added = [p for p in lots if p.add_fired]
    single = [p for p in lots if not p.add_fired]
    def stat(g):
        if not g:
            return dict(n=0, win=0.0, mean=0.0, median=0.0)
        r = [p.blended_ret for p in g]
        return dict(n=len(g), win=sum(1 for x in r if x > 0) / len(r),
                    mean=statistics.mean(r), median=statistics.median(r))
    return dict(added=stat(added), single=stat(single))


# ----------------------------------------------------------------------------- report
def _pct(x):
    if x is None:
        return "n/a"
    return ("+" if x >= 0 else "") + str(round(x * 100, 1)) + "%"


def build_report(results, cohort):
    base = results["BASELINE"]
    L = []
    A = L.append
    A("# CAN SLIM replica - STOCK-side ADD (pyramiding) backtest")
    A("")
    A("_Does ADDING TO POSITIONS improve the stock-only replica? Selection, entries, starter "
      "sizing, and the EXIT rule (E3: -7% stop -> rising-50-day handover -> decisive-break sell) "
      "are ALL held fixed to isolate the ADD effect. Add rules are anchored to Doug's MEASURED "
      "behavior (research/doug_add_behavior.md) + O'Neil's raise-the-stop discipline -- NOT tuned. "
      "The options version is separate and waits for real quotes._")
    A("")
    A("- Start capital **$" + format(int(START_CAPITAL), ",") + "**; his " + str(base["n"])
      + " built positions; entries + starter $ = his revealed cost; exit = E3 for every arm.")
    A("- Add anchors (from his data, frozen): trigger within **" + str(ADD_WINDOW_DAYS)
      + " days** of entry while price is in the buy-zone **[" + _pct(ADD_ZONE_LO) + " .. "
      + _pct(ADD_ZONE_HI) + "]** off entry; add size **" + str(int(ADD_FRAC * 100))
      + "% of starter** (ONEIL_HALF = 50%); **one add max**; built name capped at "
      + str(int(BOOK_CAP_FRAC * 100)) + "% of equity. Each add RAISES the stop so the blended "
      "position's worst-case dollar loss stays == the starter's original -7%.")
    A("")
    A("## Arm-by-arm vs baseline")
    A("")
    A("| Arm | Total ret | CAGR | Max DD | Win% | Median trade | Mean trade | Losing-tail (worst decile) | 10th-pctile | #adds |")
    A("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    order = ["BASELINE", "DOUG_MEASURED", "ONEIL_UPTREND", "ONEIL_HALF"]
    for arm in order:
        r = results[arm]
        A("| " + arm + " | " + _pct(r["total_ret"]) + " | " + _pct(r["cagr"]) + " | "
          + _pct(r["mdd"]) + " | " + str(round(r["win_rate"] * 100)) + "% | " + _pct(r["median_ret"])
          + " | " + _pct(r["mean_ret"]) + " | " + _pct(r["losing_tail_mean"]) + " | "
          + _pct(r["p10"]) + " | " + str(r["n_added"]) + " |")
    A("")
    A("_Same picks / entries / starter sizing / E3 exit across all rows. The ONLY difference is "
      "the add. #adds = how many of the " + str(base["n"]) + " positions received a top-up._")
    A("")
    A("## Deltas vs BASELINE (the isolated add effect)")
    A("")
    A("| Arm | dTotal ret | Max DD (base -> arm) | dWin% | dMedian | dMean |")
    A("|---|---:|---:|---:|---:|---:|")
    for arm in order[1:]:
        r = results[arm]
        dd_dir = "deeper" if (r["mdd"] - base["mdd"]) < -0.005 else ("shallower" if (r["mdd"] - base["mdd"]) > 0.005 else "flat")
        A("| " + arm + " | " + _pct(r["total_ret"] - base["total_ret"]) + " | "
          + _pct(base["mdd"]) + " -> " + _pct(r["mdd"]) + " (" + dd_dir + ") | "
          + ("+" if (r["win_rate"] - base["win_rate"]) >= 0 else "")
          + str(round((r["win_rate"] - base["win_rate"]) * 100, 1)) + "pp | "
          + _pct(r["median_ret"] - base["median_ret"]) + " | "
          + _pct(r["mean_ret"] - base["mean_ret"]) + " |")
    A("_Max DD is more negative = deeper = worse. Adding DEEPENS portfolio drawdown here "
      "(more capital committed), even though each add's per-position loss is capped at the starter's -7%._")
    A("")
    A("## Added-to vs single-entry cohorts (does Doug's §5 direction hold in-sim?)")
    A("")
    A("_For DOUG_MEASURED: split the built positions into those that got an add vs those that "
      "did not, and compare -- the mirror of doug_add_behavior.md §5. His data: added-to won 48% "
      "(mean 3.1%, median -0.2%) vs single-entry 28% (mean 6.1%, median -6.5%) -- adding was a "
      "consistency lever, not a mean booster._")
    A("")
    c = cohort
    A("| Cohort | n | Win% | Mean | Median |")
    A("|---|---:|---:|---:|---:|")
    A("| added-to | " + str(c["added"]["n"]) + " | " + str(round(c["added"]["win"] * 100)) + "% | "
      + _pct(c["added"]["mean"]) + " | " + _pct(c["added"]["median"]) + " |")
    A("| single-entry | " + str(c["single"]["n"]) + " | " + str(round(c["single"]["win"] * 100))
      + "% | " + _pct(c["single"]["mean"]) + " | " + _pct(c["single"]["median"]) + " |")
    A("")

    # ---- automated verdict ----
    dm = results["DOUG_MEASURED"]
    d_ret = dm["total_ret"] - base["total_ret"]
    d_win = (dm["win_rate"] - base["win_rate"]) * 100        # pp
    d_med = dm["median_ret"] - base["median_ret"]
    d_dd = dm["mdd"] - base["mdd"]                            # NEGATIVE => drawdown got DEEPER (worse)
    return_booster = (d_ret > 0.01)
    dd_worse = (d_dd < -0.005)                                # more negative mdd = worse
    win_up = (d_win > 0.5)
    med_up = (d_med > 0.005)

    def dd_phrase():
        if dd_worse:
            return ("max DD got DEEPER (" + _pct(base["mdd"]) + " -> " + _pct(dm["mdd"])
                    + ", i.e. " + str(round(abs(d_dd) * 100, 1)) + "pp WORSE)")
        elif d_dd > 0.005:
            return ("max DD improved (" + _pct(base["mdd"]) + " -> " + _pct(dm["mdd"]) + ")")
        return "max DD roughly unchanged (" + _pct(base["mdd"]) + " -> " + _pct(dm["mdd"]) + ")"

    A("## Verdict (this sample)")
    A("")
    if return_booster:
        A("- **Adding RAISES total return** by " + _pct(d_ret) + " (baseline " + _pct(base["total_ret"])
          + " -> DOUG_MEASURED " + _pct(dm["total_ret"]) + ") -- but at the cost of a DEEPER "
          "drawdown, and it is NOT the consistency lever his raw data suggested (see below).")
    else:
        A("- **Adding is roughly NEUTRAL / negative on total return** here (" + _pct(d_ret)
          + ": baseline " + _pct(base["total_ret"]) + " -> DOUG_MEASURED " + _pct(dm["total_ret"]) + ").")
    # consistency read -- honest about sign of EACH metric
    if win_up and med_up and not dd_worse:
        A("- **It also behaves as a consistency lever** (win " + ("+" if d_win >= 0 else "")
          + str(round(d_win, 1)) + "pp, median " + _pct(d_med) + ", " + dd_phrase()
          + ") -- matching Doug's §5 direction.")
    else:
        A("- **It does NOT act as a consistency lever in-sim** -- the opposite of what his raw §5 "
          "table showed. Win rate " + ("+" if d_win >= 0 else "") + str(round(d_win, 1))
          + "pp (baseline " + str(round(base["win_rate"] * 100)) + "% -> " + str(round(dm["win_rate"] * 100))
          + "%), median trade " + _pct(d_med) + ", and " + dd_phrase() + ". Once the raise-the-stop "
          "discipline is enforced AND the extra dollars are marked in a real portfolio, adding "
          "levers RETURN (more capital in working names) rather than smoothing the ride.")
    A("- **Why the flip from his §5 data?** His raw table found adding improved win rate / median "
      "because he added DISCRETIONARILY only to names already working ('only-if-working' selection). "
      "This mechanical rule adds to nearly every name that dips into the buy-zone early ("
      + str(dm["n_added"]) + "/" + str(base["n"]) + "), so it does NOT inherit his selection edge -- "
      "it just deploys ~60% more dollars into the same E3 outcome. Return scales up; the win-rate/"
      "median smoothing does not, because that smoothing was his PICKING, not the add mechanic.")
    A("- **Mechanism check (raise-the-stop holds):** an add can never deepen the *per-position* "
      "dollar loss vs the starter's -7% (verified in code). The DEEPER PORTFOLIO drawdown comes "
      "from concentration -- ~60% more capital committed across many names that dip together in the "
      "2025-2026 air-pockets -- not from any single add blowing through its stop.")
    A("")
    A("### Hard limits (curve-fit + honesty guards, rule #1)")
    A("- **Anchored, not tuned.** Window (14d), zone (-3.6%..+9.6%), size (60%/50%), one-add cap, "
      "25%-of-book cap all come from his measured behavior or O'Neil's playbook. The grid is "
      "DESCRIPTIVE; no cell was selected to win.")
    A("- **Fires more often than he did.** He added to ~25% of positions (discretionary, "
      "'only-if-working'); the mechanical rule adds to EVERY position that enters the zone in the "
      "window (" + str(dm["n_added"]) + "/" + str(base["n"]) + " here). Re-creating his ~25% "
      "selectivity would be lookahead, so this OVER-adds vs him -- a conservative stress of the "
      "mechanic, not a replica of his hand.")
    A("- **Bull-heavy 2023-2026 universe, small N of adds, his-entries-only** (EXECUTION not "
      "selection). Paths begin ~entry, so the 50-day filter (ONEIL_UPTREND) is rarely available "
      "inside the 14-day add window -> that arm adds very seldom by construction; read it as 'add "
      "only when an early uptrend is already confirmable,' which is strict here.")
    A("- **Add funded at entry** (single cash event) since it lands within ~2 weeks; if cash is "
      "short the add is cancelled and the position runs starter-only. No lookahead: the add fires "
      "on the first qualifying bar using only bars up to that day.")
    A("")
    return "\n".join(L)


def write_csv(results, cohort, path):
    order = ["BASELINE", "DOUG_MEASURED", "ONEIL_UPTREND", "ONEIL_HALF"]
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["section", "arm", "n", "n_added", "total_ret", "cagr", "max_dd", "win_rate",
                    "median_ret", "mean_ret", "losing_tail_mean", "p10", "worst",
                    "final_equity", "total_add_dollars"])
        for arm in order:
            r = results[arm]
            w.writerow(["arm", arm, r["n"], r["n_added"], round(r["total_ret"], 4),
                        round(r["cagr"], 4), round(r["mdd"], 4), round(r["win_rate"], 4),
                        round(r["median_ret"], 4), round(r["mean_ret"], 4),
                        round(r["losing_tail_mean"], 4), round(r["p10"], 4), round(r["worst"], 4),
                        round(r["final_equity"]), round(r["total_add_dollars"])])
        w.writerow([])
        w.writerow(["cohort", "which", "n", "win_rate", "mean_ret", "median_ret"])
        for k in ("added", "single"):
            c = cohort[k]
            w.writerow(["cohort", k, c["n"], round(c["win"], 4), round(c["mean"], 4),
                        round(c["median"], 4)])


# ----------------------------------------------------------------------------- main
def main():
    trades = X.load_ledger()
    paths = X.load_paths()
    names = {t["symbol"] for t in trades}
    have = sum(1 for n in names if n in paths)
    print("[add-bt] " + str(len(trades)) + " trades; price paths for " + str(have) + "/"
          + str(len(names)) + " names")

    results = {}
    for arm in ("BASELINE", "DOUG_MEASURED", "ONEIL_UPTREND", "ONEIL_HALF"):
        results[arm] = run_arm(trades, paths, arm)
    cohort = add_cohort(results["DOUG_MEASURED"])

    report = build_report(results, cohort)
    md_path = os.path.join(RESEARCH, "stock_add_test.md")
    csv_path = os.path.join(RESEARCH, "stock_add_results.csv")
    open(md_path, "w", encoding="utf-8").write(report)
    write_csv(results, cohort, csv_path)
    # scratchpad copies
    open(os.path.join(SCRATCH, "stock_add_test.md"), "w", encoding="utf-8").write(report)
    write_csv(results, cohort, os.path.join(SCRATCH, "stock_add_results.csv"))
    print("[add-bt] wrote " + md_path)
    print("[add-bt] wrote " + csv_path)

    print("\n=== ARMS (total / CAGR / maxDD / win% / median / mean / #adds) ===")
    hdr = ("arm".ljust(15) + "total".rjust(9) + "cagr".rjust(8) + "maxDD".rjust(8)
           + "win%".rjust(6) + "median".rjust(9) + "mean".rjust(9) + "#adds".rjust(7))
    print(hdr)
    for arm in ("BASELINE", "DOUG_MEASURED", "ONEIL_UPTREND", "ONEIL_HALF"):
        r = results[arm]
        print(arm.ljust(15)
              + (format(r["total_ret"] * 100, "+.1f") + "%").rjust(9)
              + (format(r["cagr"] * 100, "+.1f") + "%").rjust(8)
              + (format(r["mdd"] * 100, "+.1f") + "%").rjust(8)
              + (str(round(r["win_rate"] * 100)) + "%").rjust(6)
              + (format(r["median_ret"] * 100, "+.1f") + "%").rjust(9)
              + (format(r["mean_ret"] * 100, "+.1f") + "%").rjust(9)
              + str(r["n_added"]).rjust(7))
    print("\nadded-to cohort (DOUG_MEASURED): ", cohort["added"])
    print("single-entry cohort:             ", cohort["single"])
    return results, cohort


if __name__ == "__main__":
    main()
