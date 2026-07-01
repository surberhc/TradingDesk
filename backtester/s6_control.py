r"""
s6_control.py — the S6 0DTE credit-spread CONTROL harness (fixed-delta baseline).

PAPER / research only. OFFLINE. Windows. STRICTLY READ-ONLY on the warehouse.

THIS IS A YARDSTICK, NOT A STRATEGY. It runs the plain, documented S6 mechanics with
NO optimization and NO tuning, so that every later strike-selection idea has an honest
baseline to beat. It picks the short strike by a FIXED target delta (a chosen-by-spec
constant), takes HONEST fills (sell at bid / buy at ask), and reconstructs intraday P&L
minute-by-minute from the 1-minute NBBO via s5_intraday_data + s6_recon.

NOTHING here is fit to the data. The only "numbers" are the documented S6 constants,
declared once below. No sweeps, no best-delta search, no date selection. If the honest
baseline loses money, that is a valid and important finding and is reported as-is.

DOCUMENTED S6 PARAMETERS (verbatim spec constants — NOT tuned):
  * entry ~14:00 ET
  * 5-point-wide spread
  * short strike at a FIXED 0.15 target delta
  * exit: close at $0.05 debit (winner) OR stop when net loss = 2x entry credit OR hold
    to PM settlement (16:00)
  * skip the trade if entry credit < $0.30 (documented no-trade rule)
  * 1 contract, no position sizing

STRUCTURES (reported SEPARATELY): bull put spread, bear call spread, iron condor.

NO-LOOK-AHEAD: strike selection uses ONLY the 14:00 snapshot; the exit scan walks minutes
forward and stops at the FIRST minute a rule fires — it never peeks at later minutes.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field, asdict
from pathlib import Path

import numpy as np
import pandas as pd

import s5_intraday_data as s5
import s6_recon as recon

# --------------------------------------------------------------------------- #
# Documented S6 constants (NOT tuned — declared once, used verbatim).
# --------------------------------------------------------------------------- #
ENTRY_TIME = _dt.time(14, 0)        # ~14:00 ET entry
SETTLEMENT_TIME = _dt.time(16, 0)   # PM settlement / hold-to-close
SPREAD_WIDTH = 5.0                  # 5-point-wide spread
TARGET_SHORT_DELTA = 0.15          # FIXED short-strike delta (chosen-by-spec constant)
WINNER_DEBIT = 0.05                # close winner at $0.05 debit
STOP_MULTIPLE = 2.0                # stop at net loss = 2x entry credit
MIN_ENTRY_CREDIT = 0.30            # skip if entry credit < $0.30
CONTRACT_MULTIPLIER = 100.0        # SPX options are $100/point
N_CONTRACTS = 1                    # 1 contract, no sizing

# Research output area (NOT a committed data dir; created on demand).
OUTPUT_DIR = Path(__file__).resolve().parent / "output" / "s6_research"


# --------------------------------------------------------------------------- #
# Trade record
# --------------------------------------------------------------------------- #
@dataclass
class SpreadTrade:
    day: _dt.date
    structure: str                 # 'bull_put' | 'bear_call' | 'iron_condor'
    traded: bool = False
    skip_reason: str = ""
    entry_credit: float = float("nan")   # per spread, in option points
    short_strike: float = float("nan")
    long_strike: float = float("nan")
    short_strike_2: float = float("nan")  # iron condor's second short (call side)
    long_strike_2: float = float("nan")
    entry_short_delta: float = float("nan")
    exit_reason: str = ""          # 'winner' | 'stop' | 'settle'
    exit_minute: pd.Timestamp | None = None
    exit_debit: float = float("nan")     # cost to close (points)
    pnl_points: float = float("nan")     # entry_credit - exit_debit
    pnl_dollars: float = float("nan")
    # Day-type bucket — STUB for a future classifier. Left 'unclassified' on purpose;
    # the harness only structures the field so a classifier can slot in without touching
    # the P&L engine.
    day_type: str = "unclassified"


# --------------------------------------------------------------------------- #
# Fill helpers — HONEST FILLS, no mid.
# --------------------------------------------------------------------------- #
def _credit_to_open(short_bid: float, long_ask: float) -> float:
    """Open a credit spread: SELL the short leg at the BID, BUY the long leg at the ASK.

    Net credit received = short_bid - long_ask. Honest: we never assume mid.
    """
    return short_bid - long_ask


def _debit_to_close(short_ask: float, long_bid: float) -> float:
    """Close a credit spread: BUY BACK the short leg at the ASK, SELL the long leg at the BID.

    Net debit paid = short_ask - long_bid.
    """
    return short_ask - long_bid


# --------------------------------------------------------------------------- #
# Strike selection at entry (uses ONLY the entry snapshot => no look-ahead).
# --------------------------------------------------------------------------- #
def _snap_at(nbbo: pd.DataFrame, minute: pd.Timestamp) -> pd.DataFrame:
    return nbbo[nbbo["minute"] == minute][["strike", "right", "bid", "ask"]].copy()


def _pick_short_by_delta(
    delta_tbl: pd.DataFrame, right: str, target_abs_delta: float
) -> float | None:
    """Pick the strike whose |delta| is nearest the target, on the given side."""
    side = delta_tbl[delta_tbl["right"] == right].copy()
    side = side[side["delta"].notna()]
    if side.empty:
        return None
    side["d_err"] = (side["delta"].abs() - target_abs_delta).abs()
    return float(side.sort_values("d_err").iloc[0]["strike"])


def _leg_quote(snap: pd.DataFrame, strike: float, right: str) -> tuple[float, float] | None:
    """(bid, ask) for one leg at the snapshot, or None if absent/unquoted."""
    row = snap[(snap["strike"] == strike) & (snap["right"] == right)]
    if row.empty:
        return None
    b, a = float(row["bid"].iloc[0]), float(row["ask"].iloc[0])
    if not (np.isfinite(b) and np.isfinite(a)):
        return None
    return b, a


# --------------------------------------------------------------------------- #
# Per-structure entry builders
# --------------------------------------------------------------------------- #
def _build_put_spread(snap, delta_tbl, target_delta) -> dict | None:
    """Bull put spread: short put near target delta, long put SPREAD_WIDTH lower."""
    short_k = _pick_short_by_delta(delta_tbl, "PUT", target_delta)
    if short_k is None:
        return None
    long_k = short_k - SPREAD_WIDTH
    sq = _leg_quote(snap, short_k, "PUT")
    lq = _leg_quote(snap, long_k, "PUT")
    if sq is None or lq is None:
        return None
    short_bid, short_ask = sq
    long_bid, long_ask = lq
    credit = _credit_to_open(short_bid, long_ask)
    sd = delta_tbl[(delta_tbl["strike"] == short_k) & (delta_tbl["right"] == "PUT")]
    return {
        "short_strike": short_k, "long_strike": long_k,
        "entry_credit": credit,
        "entry_short_delta": float(sd["delta"].iloc[0]) if not sd.empty else float("nan"),
        "legs": [(short_k, "PUT", +1), (long_k, "PUT", -1)],  # +1 short(sold), -1 long(bought)
    }


def _build_call_spread(snap, delta_tbl, target_delta) -> dict | None:
    """Bear call spread: short call near target delta, long call SPREAD_WIDTH higher."""
    short_k = _pick_short_by_delta(delta_tbl, "CALL", target_delta)
    if short_k is None:
        return None
    long_k = short_k + SPREAD_WIDTH
    sq = _leg_quote(snap, short_k, "CALL")
    lq = _leg_quote(snap, long_k, "CALL")
    if sq is None or lq is None:
        return None
    short_bid, short_ask = sq
    long_bid, long_ask = lq
    credit = _credit_to_open(short_bid, long_ask)
    sd = delta_tbl[(delta_tbl["strike"] == short_k) & (delta_tbl["right"] == "CALL")]
    return {
        "short_strike": short_k, "long_strike": long_k,
        "entry_credit": credit,
        "entry_short_delta": float(sd["delta"].iloc[0]) if not sd.empty else float("nan"),
        "legs": [(short_k, "CALL", +1), (long_k, "CALL", -1)],
    }


def _build_iron_condor(snap, delta_tbl, target_delta) -> dict | None:
    """Iron condor = bull put spread + bear call spread (both at target delta)."""
    put = _build_put_spread(snap, delta_tbl, target_delta)
    call = _build_call_spread(snap, delta_tbl, target_delta)
    if put is None or call is None:
        return None
    return {
        "short_strike": put["short_strike"], "long_strike": put["long_strike"],
        "short_strike_2": call["short_strike"], "long_strike_2": call["long_strike"],
        "entry_credit": put["entry_credit"] + call["entry_credit"],
        "entry_short_delta": put["entry_short_delta"],
        "legs": put["legs"] + call["legs"],
    }


_BUILDERS = {
    "bull_put": _build_put_spread,
    "bear_call": _build_call_spread,
    "iron_condor": _build_iron_condor,
}


# --------------------------------------------------------------------------- #
# Intraday P&L / exit scan — walks minutes forward, stops at first rule hit.
# --------------------------------------------------------------------------- #
def _spread_debit_to_close(snap: pd.DataFrame, legs: list[tuple]) -> float | None:
    """Cost (debit, points) to close the whole position at this minute's NBBO.

    For each leg: a SOLD leg (+1) is bought back at the ASK; a BOUGHT leg (-1) is sold
    at the BID. Total close debit = sum over legs. Returns None if any leg is unquoted
    (cannot mark the position this minute -> caller skips this minute).
    """
    total = 0.0
    for strike, right, side in legs:
        q = _leg_quote(snap, strike, right)
        if q is None:
            return None
        bid, ask = q
        if side > 0:      # we are SHORT this leg -> buy back at ask (pay)
            total += ask
        else:             # we are LONG this leg -> sell at bid (receive)
            total -= bid
    return total


def _scan_exit(
    nbbo: pd.DataFrame,
    legs: list[tuple],
    entry_credit: float,
    entry_minute: pd.Timestamp,
    settle_minute: pd.Timestamp,
) -> tuple[str, pd.Timestamp, float]:
    """Walk minutes AFTER entry forward; return (reason, minute, exit_debit).

    Rules, checked in order each minute (causal — only this minute's quote):
      * winner: debit-to-close <= WINNER_DEBIT
      * stop:   net loss >= STOP_MULTIPLE * entry_credit, i.e.
                (debit - entry_credit) >= STOP_MULTIPLE*entry_credit
                <=> debit >= (1 + STOP_MULTIPLE) * entry_credit
    If neither fires by the settlement minute, close at settlement's debit ('settle').
    Never looks past the firing minute.
    """
    minutes = sorted(m for m in nbbo["minute"].unique()
                     if entry_minute < m <= settle_minute)
    stop_debit = (1.0 + STOP_MULTIPLE) * entry_credit
    last_marked_debit = float("nan")
    last_marked_minute = entry_minute
    for m in minutes:
        snap = _snap_at(nbbo, m)
        debit = _spread_debit_to_close(snap, legs)
        if debit is None:
            continue  # unquoted minute -> cannot act; do NOT invent a fill.
        last_marked_debit = debit
        last_marked_minute = m
        if debit <= WINNER_DEBIT:
            return "winner", m, debit
        if debit >= stop_debit:
            return "stop", m, debit
    # Held to settlement (or ran out of quoted minutes): close at the last mark.
    return "settle", last_marked_minute, last_marked_debit


# --------------------------------------------------------------------------- #
# One day, one structure
# --------------------------------------------------------------------------- #
def run_day_structure(
    d: _dt.date, structure: str, day_data: s5.DayData | None = None
) -> SpreadTrade:
    """Run the control mechanics for one structure on one day. Never raises on data
    quirks for a single day — returns a non-traded SpreadTrade with a skip_reason."""
    tr = SpreadTrade(day=d, structure=structure)
    try:
        dd = day_data if day_data is not None else s5.load_day(d)
        chain = s5.zero_dte_chain(d, day_data=dd)
        nbbo = chain.nbbo
        if nbbo.empty:
            tr.skip_reason = "no 0dte chain"
            return tr

        entry_minute = pd.Timestamp(_dt.datetime.combine(d, ENTRY_TIME))
        settle_minute = pd.Timestamp(_dt.datetime.combine(d, SETTLEMENT_TIME))
        if entry_minute not in set(nbbo["minute"].unique()):
            tr.skip_reason = "no 14:00 snapshot"
            return tr

        entry_snap = _snap_at(nbbo, entry_minute)
        # Recover spot at entry, then per-strike deltas (ONLY the entry minute).
        sr = recon.recover_forward_spot(entry_snap, entry_minute, d)
        if sr is None:
            tr.skip_reason = "spot recon failed at entry"
            return tr
        delta_tbl = recon.per_strike_delta(entry_snap, entry_minute, d, sr.spot)

        build = _BUILDERS[structure](entry_snap, delta_tbl, TARGET_SHORT_DELTA)
        if build is None:
            tr.skip_reason = "could not build structure at entry"
            return tr

        tr.short_strike = build["short_strike"]
        tr.long_strike = build["long_strike"]
        tr.short_strike_2 = build.get("short_strike_2", float("nan"))
        tr.long_strike_2 = build.get("long_strike_2", float("nan"))
        tr.entry_credit = build["entry_credit"]
        tr.entry_short_delta = build["entry_short_delta"]

        # Documented no-trade rule: skip if entry credit < $0.30.
        if not np.isfinite(build["entry_credit"]) or build["entry_credit"] < MIN_ENTRY_CREDIT:
            tr.skip_reason = f"entry credit {build['entry_credit']:.2f} < {MIN_ENTRY_CREDIT}"
            return tr

        reason, exit_minute, exit_debit = _scan_exit(
            nbbo, build["legs"], build["entry_credit"], entry_minute, settle_minute
        )
        if not np.isfinite(exit_debit):
            tr.skip_reason = "no quoted minute to mark/close"
            return tr

        tr.traded = True
        tr.exit_reason = reason
        tr.exit_minute = exit_minute
        tr.exit_debit = exit_debit
        tr.pnl_points = build["entry_credit"] - exit_debit
        tr.pnl_dollars = tr.pnl_points * CONTRACT_MULTIPLIER * N_CONTRACTS
        return tr
    except Exception as e:
        tr.skip_reason = f"error: {type(e).__name__}: {e}"
        return tr


# --------------------------------------------------------------------------- #
# Full-history run + stats
# --------------------------------------------------------------------------- #
_PARTIAL_CSV = OUTPUT_DIR / "s6_control_trades_partial.csv"


def run_history(
    days: list[_dt.date] | None = None,
    structures: tuple[str, ...] = ("bull_put", "bear_call", "iron_condor"),
    verbose: bool = True,
    save: bool = True,
    resume: bool = True,
) -> dict[str, pd.DataFrame]:
    """Run every structure over every available 0DTE day.

    CRASH-RESILIENT + RESUMABLE: each finished day's rows are appended to a partial CSV
    immediately, so a killed run loses at most the in-flight day. On restart with
    resume=True we skip days already in the partial CSV. This makes a long, I/O-bound
    full-history run safe to supervise — one bad day cannot abort it (per-day try/except
    inside run_day_structure) and an interruption cannot throw away hours of work.
    """
    if days is None:
        days = s5.available_days()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Resume: which days are already fully written?
    done_days: set[str] = set()
    if resume and _PARTIAL_CSV.is_file():
        try:
            prev = pd.read_csv(_PARTIAL_CSV, usecols=["day"])
            done_days = set(prev["day"].astype(str).unique())
        except Exception:
            done_days = set()
    if verbose and done_days:
        print(f"resume: {len(done_days)} days already in partial CSV; skipping them",
              flush=True)

    n = len(days)
    fieldnames = list(asdict(SpreadTrade(day=days[0], structure="x")).keys())
    write_header = not _PARTIAL_CSV.is_file()
    import csv
    with open(_PARTIAL_CSV, "a", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        if write_header:
            writer.writeheader()
        cum_trades = 0
        for i, d in enumerate(days, 1):
            if str(d) in done_days:
                continue
            try:
                dd = s5.load_day(d)  # load once, reuse across structures
            except Exception as e:
                if verbose:
                    print(f"[{i}/{n}] {d} LOAD-SKIP {type(e).__name__}: {e}", flush=True)
                continue
            for structure in structures:
                tr = run_day_structure(d, structure, day_data=dd)
                if tr.traded:
                    cum_trades += 1
                writer.writerow(asdict(tr))
            fh.flush()  # persist this day before moving on
            if verbose and (i % 25 == 0 or i == n):
                print(f"[{i}/{n}] {d} done  (new trades this run={cum_trades})",
                      flush=True)

    # Load the full (possibly resumed) partial CSV for stats/finalization.
    df = pd.read_csv(_PARTIAL_CSV)
    # CSV round-trips bools as strings — coerce 'traded' back to a real bool.
    df["traded"] = df["traded"].astype(str).str.lower().isin(["true", "1"])

    out: dict[str, pd.DataFrame] = {"trades": df}
    by_structure = {}
    for structure in structures:
        sub = df[df["structure"] == structure].copy()
        by_structure[structure] = compute_stats(sub, structure)
    out["stats"] = pd.DataFrame(by_structure).T

    if save:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        df.to_csv(OUTPUT_DIR / "s6_control_trades.csv", index=False)
        out["stats"].to_csv(OUTPUT_DIR / "s6_control_stats.csv")
        for structure in structures:
            _equity_curve(df[df["structure"] == structure]).to_csv(
                OUTPUT_DIR / f"s6_control_equity_{structure}.csv", index=False
            )
        if verbose:
            print(f"\nSaved research outputs to {OUTPUT_DIR}", flush=True)

    if verbose:
        print("\n=== S6 CONTROL BASELINE (honest fills) ===", flush=True)
        with pd.option_context("display.width", 200, "display.max_columns", 50):
            print(out["stats"].to_string(), flush=True)
    return out


def _equity_curve(sub: pd.DataFrame) -> pd.DataFrame:
    traded = sub[sub["traded"]].copy()
    traded = traded.sort_values("day")
    traded["cum_pnl_dollars"] = traded["pnl_dollars"].cumsum()
    return traded[["day", "structure", "pnl_dollars", "cum_pnl_dollars", "exit_reason"]]


def compute_stats(sub: pd.DataFrame, structure: str) -> dict:
    """All requested baseline outputs for one structure. day_type bucketing left as a
    STUB hook (per-bucket stats are produced if a classifier ever fills day_type)."""
    traded = sub[sub["traded"]].copy().sort_values("day")
    n_all = len(sub)
    n_trades = len(traded)
    if n_trades == 0:
        return {
            "structure": structure, "days_seen": n_all, "trades": 0,
            "skipped": int((~sub["traded"]).sum()),
        }
    wins = traded[traded["pnl_dollars"] > 0]
    losses = traded[traded["pnl_dollars"] <= 0]
    avg_win = float(wins["pnl_dollars"].mean()) if len(wins) else 0.0
    avg_loss = float(losses["pnl_dollars"].mean()) if len(losses) else 0.0
    win_rate = len(wins) / n_trades

    # Per-DAY net P&L (iron condor already one row/day; spreads one row/day too).
    daily = traded.groupby("day")["pnl_dollars"].sum().sort_index()
    cum = daily.cumsum()
    worst_day_val = float(daily.min())
    worst_day_date = daily.idxmin()

    # Max consecutive losing days.
    losing = (daily < 0).astype(int)
    max_streak = cur = 0
    for v in losing:
        cur = cur + 1 if v else 0
        max_streak = max(max_streak, cur)

    # Max-loss days: stop-outs (the engineered worst case = 2x credit loss).
    stop_days = traded[traded["exit_reason"] == "stop"]
    max_loss_dates = list(pd.to_datetime(stop_days["day"]).dt.date.astype(str))

    avg_loss_over_win = (abs(avg_loss) / avg_win) if avg_win > 0 else float("nan")

    return {
        "structure": structure,
        "days_seen": n_all,
        "trades": n_trades,
        "skipped": int((~sub["traded"]).sum()),
        "with_stops": int((traded["exit_reason"] == "stop").sum()),
        "winners": int((traded["exit_reason"] == "winner").sum()),
        "settled": int((traded["exit_reason"] == "settle").sum()),
        "win_rate": round(win_rate, 4),
        "avg_win_$": round(avg_win, 2),
        "avg_loss_$": round(avg_loss, 2),
        "avg_loss_over_avg_win": round(avg_loss_over_win, 3),
        "total_pnl_$": round(float(daily.sum()), 2),
        "net_profitable": bool(daily.sum() > 0),
        "worst_day_$": round(worst_day_val, 2),
        "worst_day_date": str(worst_day_date),
        "max_consec_losing_days": int(max_streak),
        "n_max_loss(stop)_days": len(max_loss_dates),
        "max_loss_dates": ";".join(max_loss_dates[:20]),
    }


if __name__ == "__main__":
    run_history()
