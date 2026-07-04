r"""
condor_management_experiment.py — the untested DIAL in the 0DTE iron-condor line:
the MANAGEMENT / EXIT rule. A TERRAIN MAP, not a hunt for one winning cell.

PAPER / research only. OFFLINE. Windows. STRICTLY READ-ONLY on the warehouse.

THE HYPOTHESIS (mechanism-first, Andrew's)
------------------------------------------
Every prior refutation of the 0DTE iron condor (S6 exit-matrix, S2/S3 gap gate, S2/S3
morning-rvol) used the SAME exit chassis: enter 14:00, hold to 16:00 settlement with a
$0.05 winner and a 2x-credit stop. That chassis SITS THROUGH the final-hour gamma spike
every single day. The management/exit rule was a frozen constant and is the real UNTESTED
dial. This module varies ONLY that dial, on the identical 0.15-delta entry chassis, so the
result is comparable to the prior control number (-$32,870 hold-to-settle, 2022-2026).

Proposed mechanism: early profit-taking harvests the fast, front-loaded morning/early theta
and exits BEFORE the end-of-day gamma tail; short duration already dodges the overnight-gap
tail. IF that mechanism is real, an early-exit rule flips the hold-to-settle loss into a
robust net-positive PLATEAU after honest 4-leg costs. IF not, it dies -- and a refutation is
a valid outcome.

THE ARMS (vary ONLY the exit rule; entry chassis = the control's, verbatim)
---------------------------------------------------------------------------
  A  HOLD-TO-SETTLE   : the losing baseline (winner $0.05 / 2x stop / settle). REPRODUCES the
                        prior control byte-for-byte (a sanity check, asserted in tests).
  B  PROFIT-TARGET    : close the whole condor at first-touch of open P&L >= {25,50,75}% of the
                        entry credit. (Also keeps the 2x stop as the standard disaster brake --
                        an unmanaged short-vol book with no stop is not a realistic comparator;
                        the B arms differ from A ONLY by adding an EARLY take-profit.)
  C  TIME-EXIT        : flat by a fixed clock time {14:00... no -- entry is 14:00; use 15:00, 15:30}
                        regardless, to escape the final-hour gamma. (Keeps the 2x stop too.)
  D  COMBO            : profit-target(50%) OR 2x stop OR time-exit(15:30), whichever fires first
                        -- the realistic managed trade.

HONEST COSTS (make-or-break on thin 0DTE premium)
-------------------------------------------------
Entry AND every management exit are HONEST 4-leg bid/ask fills via the control's own
ctrl._spread_debit_to_close / ctrl._credit_to_open. The extra exit round-trip on an early
close is a REAL cost and is booked (the P&L is entry_credit - exit_debit, and the exit_debit
is the true cost to buy back the shorts at the ASK and sell the wings at the BID). There is
no mid, no modeled slippage discount. We ALSO report each arm gross (mid-fill) vs net (honest)
to show how much of the credit the 4-leg spread eats -- the crux for 0DTE.

ANTI-CURVE-FIT SPINE (rule #1 -- the point, not an afterthought)
----------------------------------------------------------------
  * PLATEAU not peak: the profit-target result is reported as a CURVE across 25/50/75%. A robust
    positive plateau is signal; a single positive cell surrounded by losers is a mirage -- and
    we say so explicitly.
  * PLACEBO (decisive): for any early-exit arm that beats hold-to-settle, a RANDOM-EXIT control
    matched to that arm's AVERAGE holding time (exit at a random minute drawn to match the mean
    duration). If the arm does NOT beat its matched random-exit placebo, the early-harvest LOGIC
    added nothing -- it was just "less time in market." Same bar that killed the re-entry ladder,
    the gap gate, and the morning-rvol arms.
  * OOS split at 2024-06-30 (train 2022-2024 / test 2024-2026) reported for every arm.
  * PER-REGIME / per-year P&L broken out; the 2022 bear vs calm stretches flagged. The 0DTE
    window is 2022+ (no 2020/2018), so the tail sample is small -- stated honestly.
  * The exit constants (25/50/75%, 15:00/15:30, 2x stop) are PRE-REGISTERED plain choices, not
    swept to a winner. The profit-target CURVE is the plateau test, not a best-cell search.

NO LOOK-AHEAD (load-bearing)
----------------------------
The entry uses ONLY the 14:00 snapshot. `_scan_managed_exits` walks minutes forward and
resolves EACH arm at the FIRST minute its own rule binds -- it never peeks past a firing
minute, and a later-minute price can never change an earlier exit decision. Pinned by
tests/test_condor_management_experiment.py (a no-lookahead guard + a cost-is-charged guard)
and the standing causality guard.

CRASH-RESILIENT + RESUMABLE: per-day incremental CSV append + resume-skip, heartbeat prints
flushed each block. A killed run loses at most the in-flight day. ASCII-only console output.
"""

from __future__ import annotations

import argparse
import csv
import datetime as _dt
import gc
from dataclasses import dataclass, asdict
from pathlib import Path

import numpy as np
import pandas as pd

import s5_intraday_data as s5
import s6_recon as recon
import s6_control as ctrl
import s6_matrix as mx

# --------------------------------------------------------------------------- #
# Entry chassis -- inherited VERBATIM from the control (NOT re-tuned).
# --------------------------------------------------------------------------- #
ENTRY_TIME = ctrl.ENTRY_TIME              # 14:00 ET
SETTLEMENT_TIME = ctrl.SETTLEMENT_TIME    # 16:00 ET
SPREAD_WIDTH = ctrl.SPREAD_WIDTH          # 5.0
TARGET_SHORT_DELTA = ctrl.TARGET_SHORT_DELTA  # 0.15
MIN_ENTRY_CREDIT = ctrl.MIN_ENTRY_CREDIT  # 0.30
WINNER_DEBIT = ctrl.WINNER_DEBIT          # 0.05
STOP_MULTIPLE = ctrl.STOP_MULTIPLE        # 2.0 (the standard disaster brake, kept on every arm)
CONTRACT_MULTIPLIER = ctrl.CONTRACT_MULTIPLIER
N_CONTRACTS = ctrl.N_CONTRACTS

# --------------------------------------------------------------------------- #
# The MANAGEMENT dial -- pre-registered plain choices, NOT swept to a winner.
# --------------------------------------------------------------------------- #
PROFIT_TARGET_FRACS = (0.25, 0.50, 0.75)          # B arms: take profit at these fractions of credit
TIME_EXIT_TIMES = (_dt.time(15, 0), _dt.time(15, 30))  # C arms: flat by this clock time
COMBO_PROFIT_FRAC = 0.50                            # D arm: 50% take-profit ...
COMBO_TIME_EXIT = _dt.time(15, 30)                  # ... OR flat by 15:30 ... (2x stop always on)

# --------------------------------------------------------------------------- #
# FILL BAND -- the pre-registered execution SENSITIVITY axis on the NET COMBO package
# (docs\PREREG_condor_regime_profit_modulation_2026-07-03.md, Addendum 2026-07-03).
# A fill fraction f blends the whole condor package between net-MID (f=0, optimistic) and
# worst-side-every-leg (f=1, the control's honest pessimistic bound):
#   entry credit(f) = (1-f)*net_mid_credit + f*worst_credit   (cross more spread -> receive less)
#   close  debit(f) = (1-f)*net_mid_debit  + f*worst_debit    (cross more spread -> pay more)
# f PROPAGATES through the profit-target triggers: a friendlier fill collects more credit and
# marks the running close cheaper, so it changes WHEN the 25/50/75% target is touched. This is a
# real engine parameter, not a post-hoc multiplier. f=1.0 reproduces the control byte-for-byte.
FILL_FRACS = (0.0, 0.25, 0.50, 1.0)       # mid / 25% / 50%(HEADLINE) / full-cross(control)
HEADLINE_FILL = 0.50
_FILL_TAG = {0.0: "mid", 0.25: "f25", 0.50: "f50", 1.0: "full"}

TRAIN_END = mx.TRAIN_END                  # 2024-06-30, same OOS split as everything upstream

OUTPUT_DIR = Path(__file__).resolve().parent / "output" / "condor_management"
_PARTIAL_CSV = OUTPUT_DIR / "condor_management_partial.csv"

# Off-Drive cache for the per-day honest debit PATHS (used by the exact random-exit placebo).
# Paths are minute-level and large; they must NOT churn Drive sync (per CLAUDE.md), so they
# live on local disk. One parquet per day: columns (offset_min, debit). Idempotent + resumable.
LOCAL_CACHE_DIR = Path(r"C:\TradingDesk-Local\state\condor_management")
PATHS_DIR = LOCAL_CACHE_DIR / "paths"

# Arm identifiers (the columns written per day). "A_hold" is the reproduced baseline.
ARM_NAMES = (
    "A_hold",
    "B_pt25", "B_pt50", "B_pt75",
    "C_t1500", "C_t1530",
    "D_combo",
)


# --------------------------------------------------------------------------- #
# Per-day record: one row per traded/considered day. Each arm gets its own
# pnl / exit-reason / exit-minute-index / mid-fill (gross) columns.
# --------------------------------------------------------------------------- #
@dataclass
class DayRecord:
    day: _dt.date
    traded: bool = False
    skip_reason: str = ""
    entry_credit: float = float("nan")          # honest 4-leg credit received (bid/ask)
    entry_credit_mid: float = float("nan")       # mid-fill credit (for the cost-drag report)
    short_put_k: float = float("nan")
    short_call_k: float = float("nan")
    entry_spot: float = float("nan")
    # Per-arm outcomes are attached dynamically (pnl_{arm}, exit_{arm}, holdmin_{arm},
    # pnl_mid_{arm}). Declared as a dict so the dataclass stays readable; flattened on write.
    arms: dict = None
    # Fill-band outcomes: {fill_tag -> {"entry_credit": float, arm_name -> {pnl, exit_reason,
    # hold_min}}}. Flattened to pnl_{arm}_{tag} etc. on write.
    fills: dict = None
    # day-type + split
    gamma_regime: str = "unknown"
    vix_regime: str = "unknown"
    half: str = ""

    def flat(self) -> dict:
        """Flatten to a CSV row (arm sub-fields become pnl_{arm} etc.).

        Legacy columns pnl_{arm} / pnl_mid_{arm} / exit_{arm} / holdmin_{arm} keep the
        full-cross (honest) result so the 837 resumed rows stay schema-compatible. The
        pre-registered FILL BAND adds pnl_{arm}_{tag} / exit_{arm}_{tag} / holdmin_{arm}_{tag}
        for each fill fraction (mid/f25/f50/full) plus entry_credit_{tag}.
        """
        base = {k: v for k, v in asdict(self).items() if k != "arms"}
        a = self.arms or {}
        for name in ARM_NAMES:
            sub = a.get(name, {})
            base[f"pnl_{name}"] = sub.get("pnl", float("nan"))
            base[f"pnl_mid_{name}"] = sub.get("pnl_mid", float("nan"))
            base[f"exit_{name}"] = sub.get("exit_reason", "")
            base[f"holdmin_{name}"] = sub.get("hold_min", float("nan"))
        fb = self.fills or {}
        for frac in FILL_FRACS:
            tag = _FILL_TAG[frac]
            base[f"entry_credit_{tag}"] = fb.get(tag, {}).get("entry_credit", float("nan"))
            for name in ARM_NAMES:
                sub = fb.get(tag, {}).get(name, {})
                base[f"pnl_{name}_{tag}"] = sub.get("pnl", float("nan"))
                base[f"exit_{name}_{tag}"] = sub.get("exit_reason", "")
                base[f"holdmin_{name}_{tag}"] = sub.get("hold_min", float("nan"))
        return base


def _flat_fieldnames() -> list[str]:
    base = [k for k in asdict(DayRecord(day=_dt.date(2022, 1, 3))).keys()
            if k not in ("arms", "fills")]
    for name in ARM_NAMES:
        base += [f"pnl_{name}", f"pnl_mid_{name}", f"exit_{name}", f"holdmin_{name}"]
    for frac in FILL_FRACS:
        tag = _FILL_TAG[frac]
        base += [f"entry_credit_{tag}"]
        for name in ARM_NAMES:
            base += [f"pnl_{name}_{tag}", f"exit_{name}_{tag}", f"holdmin_{name}_{tag}"]
    return base


def write_header_ok(fieldnames: list[str]) -> bool:
    """True iff the existing partial CSV's header EXACTLY matches the current schema, so a
    resume-append aligns columns correctly. A mismatch (e.g. the fill band was added since the
    file was written) must force a clean run rather than silently misalign rows."""
    try:
        with open(_PARTIAL_CSV, newline="") as fh:
            existing = next(csv.reader(fh))
        return existing == fieldnames
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# Honest + mid marks to close the whole condor at one minute's NBBO.
# _spread_debit_to_close (honest: buy shorts at ASK, sell wings at BID) is the control's own.
# The mid version is ONLY for the gross-vs-net cost-drag report -- never used for a P&L
# decision or an arm's booked result.
# --------------------------------------------------------------------------- #
def _debit_mid(snap: pd.DataFrame, legs: list[tuple]) -> float | None:
    """Mid-fill cost to close (buy/sell at the mid). Diagnostic only."""
    total = 0.0
    for strike, right, side in legs:
        q = ctrl._leg_quote(snap, strike, right)
        if q is None:
            return None
        bid, ask = q
        mid = 0.5 * (bid + ask)
        total += mid if side > 0 else -mid   # short: pay to buy back; long: receive to sell
    return total


def _credit_mid(snap: pd.DataFrame, legs: list[tuple]) -> float | None:
    """Mid-fill credit to OPEN (sell shorts / buy wings at the mid). Diagnostic only."""
    total = 0.0
    for strike, right, side in legs:
        q = ctrl._leg_quote(snap, strike, right)
        if q is None:
            return None
        bid, ask = q
        mid = 0.5 * (bid + ask)
        total += mid if side > 0 else -mid   # short: receive on sell; long: pay on buy
    return total


# --------------------------------------------------------------------------- #
# FILL-BAND blended marks (the pre-registered net-combo execution axis).
# At fraction f, every price is blended between its mid (f=0) and its worst-side (f=1):
#   * OPEN a short leg: worst = sell at BID (receive less); mid = sell at MID.
#   * OPEN a long  leg: worst = buy  at ASK (pay more);      mid = buy  at MID.
#   * CLOSE a short leg: worst = buy back at ASK (pay more); mid = buy at MID.
#   * CLOSE a long  leg: worst = sell at BID (receive less); mid = sell at MID.
# f=0 reproduces _credit_mid / _debit_mid; f=1 reproduces ctrl._credit / _spread_debit_to_close.
# Returns None if ANY leg is unquoted (never invent a fill), matching the honest engine.
# --------------------------------------------------------------------------- #
def _blended_credit_to_open(snap: pd.DataFrame, legs: list[tuple], f: float) -> float | None:
    """Net credit to OPEN the whole condor at fill fraction f of the net spread."""
    total = 0.0
    for strike, right, side in legs:
        q = ctrl._leg_quote(snap, strike, right)
        if q is None:
            return None
        bid, ask = q
        mid = 0.5 * (bid + ask)
        if side > 0:                       # short leg: sell -> worst is the BID
            total += (1.0 - f) * mid + f * bid
        else:                              # long leg: buy -> worst is the ASK
            total -= (1.0 - f) * mid + f * ask
    return total


def _blended_debit_to_close(snap: pd.DataFrame, legs: list[tuple], f: float) -> float | None:
    """Net debit to CLOSE the whole condor at fill fraction f of the net spread."""
    total = 0.0
    for strike, right, side in legs:
        q = ctrl._leg_quote(snap, strike, right)
        if q is None:
            return None
        bid, ask = q
        mid = 0.5 * (bid + ask)
        if side > 0:                       # short leg: buy back -> worst is the ASK
            total += (1.0 - f) * mid + f * ask
        else:                              # long leg: sell -> worst is the BID
            total -= (1.0 - f) * mid + f * bid
    return total


# --------------------------------------------------------------------------- #
# THE MANAGEMENT ENGINE: one causal minute-walk resolving ALL arms at once.
# --------------------------------------------------------------------------- #
def _scan_managed_exits(
    nbbo: pd.DataFrame,
    legs: list[tuple],
    entry_credit: float,
    entry_minute: pd.Timestamp,
    settle_minute: pd.Timestamp,
    profit_fracs: tuple[float, ...] = PROFIT_TARGET_FRACS,
    time_exits: tuple[_dt.time, ...] = TIME_EXIT_TIMES,
    combo_profit_frac: float = COMBO_PROFIT_FRAC,
    combo_time_exit: _dt.time = COMBO_TIME_EXIT,
) -> tuple[dict[str, dict], np.ndarray]:
    """Walk minutes AFTER entry forward ONCE; resolve every arm at the FIRST minute its own
    rule binds. Returns ({arm_name: {exit_reason, exit_minute, exit_debit, exit_debit_mid,
    hold_min}}, path) where path is an (n_quoted_minutes, 2) array of (offset_min, honest_debit)
    -- the realized honest debit-to-close at every quoted minute after entry, for the placebo.

    Causal: each minute we compute this minute's honest debit-to-close from THIS minute's NBBO
    only, then check each unresolved arm's rule against it. Once an arm fires we freeze its
    outcome and never revisit -- so a later minute can NEVER change an earlier exit. Any arm
    still open at settlement closes at the last marked debit ('settle'). This shares the
    control's exact fill engine (ctrl._spread_debit_to_close) so arm A reproduces the control.

    Rules (all in honest debit terms; open P&L = entry_credit - debit):
      * winner : debit <= WINNER_DEBIT              (a near-max-profit close; on every arm)
      * stop   : debit >= (1+STOP_MULTIPLE)*credit  (the 2x disaster brake; on every arm)
      * profit_target(f): open P&L >= f*credit  <=>  debit <= (1-f)*credit
      * time_exit(t): first minute with clock time >= t
    Arm A (hold): winner/stop/settle only -- the reproduced baseline.
    Arm B_pt{f}: winner/stop, PLUS take-profit at fraction f (whichever binds first).
    Arm C_t{t}: winner/stop, PLUS a hard time-exit at t.
    Arm D_combo: winner/stop, PLUS take-profit(combo_profit_frac) OR time-exit(combo_time_exit).
    """
    minutes = sorted(m for m in nbbo["minute"].unique()
                     if entry_minute < m <= settle_minute)
    stop_debit = (1.0 + STOP_MULTIPLE) * entry_credit

    # Build the arm spec: each arm is a set of trigger predicates on (debit, minute).
    # Every arm carries winner + stop; the extra rule per arm is what distinguishes it.
    def pt_debit(f: float) -> float:
        return (1.0 - f) * entry_credit   # debit at/below which open P&L >= f*credit

    results: dict[str, dict] = {name: None for name in ARM_NAMES}
    # Track the running "last honest/mid mark" so an arm that never triggers can settle.
    last_debit = float("nan")
    last_mid = float("nan")
    last_minute = entry_minute
    path_rows: list[tuple[float, float]] = []   # (offset_min, honest_debit) at each quoted minute

    def resolve(name: str, reason: str, minute, debit, debit_mid):
        if results[name] is None:
            hold_min = (minute - entry_minute).total_seconds() / 60.0
            results[name] = {"exit_reason": reason, "exit_minute": minute,
                             "exit_debit": float(debit),
                             "exit_debit_mid": (float(debit_mid)
                                                if debit_mid is not None else float("nan")),
                             "hold_min": float(hold_min)}

    for m in minutes:
        snap = ctrl._snap_at(nbbo, m)
        debit = ctrl._spread_debit_to_close(snap, legs)
        if debit is None:
            continue   # unquoted minute -> cannot act on ANY arm; never invent a fill.
        debit_mid = _debit_mid(snap, legs)
        last_debit, last_mid, last_minute = debit, debit_mid, m
        mtime = m.time()
        path_rows.append(((m - entry_minute).total_seconds() / 60.0, float(debit)))

        winner = debit <= WINNER_DEBIT
        stopped = debit >= stop_debit

        for name in ARM_NAMES:
            if results[name] is not None:
                continue
            # winner / stop bind on every arm first (they are the disaster / max-profit brakes).
            if winner:
                resolve(name, "winner", m, debit, debit_mid); continue
            if stopped:
                resolve(name, "stop", m, debit, debit_mid); continue
            # arm-specific early exit.
            if name == "A_hold":
                pass  # only winner/stop/settle
            elif name.startswith("B_pt"):
                f = {"B_pt25": 0.25, "B_pt50": 0.50, "B_pt75": 0.75}[name]
                if debit <= pt_debit(f):
                    resolve(name, "target", m, debit, debit_mid)
            elif name.startswith("C_t"):
                t = {"C_t1500": _dt.time(15, 0), "C_t1530": _dt.time(15, 30)}[name]
                if mtime >= t:
                    resolve(name, "time", m, debit, debit_mid)
            elif name == "D_combo":
                if debit <= pt_debit(combo_profit_frac):
                    resolve(name, "target", m, debit, debit_mid)
                elif mtime >= combo_time_exit:
                    resolve(name, "time", m, debit, debit_mid)

    # Settle any arm still open at the last marked minute.
    for name in ARM_NAMES:
        if results[name] is None:
            resolve(name, "settle", last_minute, last_debit, last_mid)
    path = np.asarray(path_rows, dtype=float) if path_rows else np.empty((0, 2))
    return results, path


def _scan_managed_exits_at_fill(
    nbbo: pd.DataFrame,
    legs: list[tuple],
    entry_credit_f: float,
    fill_frac: float,
    entry_minute: pd.Timestamp,
    settle_minute: pd.Timestamp,
    combo_profit_frac: float = COMBO_PROFIT_FRAC,
    combo_time_exit: _dt.time = COMBO_TIME_EXIT,
) -> tuple[dict[str, dict], np.ndarray]:
    """Same causal minute-walk as _scan_managed_exits, but every close mark is the NET-COMBO
    BLENDED debit at fill fraction `fill_frac`, and the profit-target triggers are measured
    against `entry_credit_f` (the blended entry credit at the SAME fraction). This is how the
    fill fraction PROPAGATES through the management logic: a friendlier fill (smaller f) both
    raises the credit and lowers the running debit, so the 25/50/75% target is touched at a
    DIFFERENT minute than under full-cross. No look-ahead: each arm freezes at the first minute
    its own rule binds on THIS minute's blended debit; a later minute never rewrites it.

    Returns ({arm: {exit_reason, exit_minute, exit_debit, hold_min}}, path) where path is the
    (offset_min, blended_debit) series at this fill fraction (used by the matched placebo so the
    placebo is evaluated on the SAME fill the arm actually traded).
    """
    minutes = sorted(m for m in nbbo["minute"].unique()
                     if entry_minute < m <= settle_minute)
    stop_debit = (1.0 + STOP_MULTIPLE) * entry_credit_f

    def pt_debit(fr: float) -> float:
        return (1.0 - fr) * entry_credit_f

    results: dict[str, dict] = {name: None for name in ARM_NAMES}
    last_debit = float("nan")
    last_minute = entry_minute
    path_rows: list[tuple[float, float]] = []

    def resolve(name, reason, minute, debit):
        if results[name] is None:
            hold_min = (minute - entry_minute).total_seconds() / 60.0
            results[name] = {"exit_reason": reason, "exit_minute": minute,
                             "exit_debit": float(debit), "hold_min": float(hold_min)}

    for m in minutes:
        snap = ctrl._snap_at(nbbo, m)
        debit = _blended_debit_to_close(snap, legs, fill_frac)
        if debit is None:
            continue
        last_debit, last_minute = debit, m
        mtime = m.time()
        path_rows.append(((m - entry_minute).total_seconds() / 60.0, float(debit)))
        winner = debit <= WINNER_DEBIT
        stopped = debit >= stop_debit
        for name in ARM_NAMES:
            if results[name] is not None:
                continue
            if winner:
                resolve(name, "winner", m, debit); continue
            if stopped:
                resolve(name, "stop", m, debit); continue
            if name == "A_hold":
                pass
            elif name.startswith("B_pt"):
                fr = {"B_pt25": 0.25, "B_pt50": 0.50, "B_pt75": 0.75}[name]
                if debit <= pt_debit(fr):
                    resolve(name, "target", m, debit)
            elif name.startswith("C_t"):
                t = {"C_t1500": _dt.time(15, 0), "C_t1530": _dt.time(15, 30)}[name]
                if mtime >= t:
                    resolve(name, "time", m, debit)
            elif name == "D_combo":
                if debit <= pt_debit(combo_profit_frac):
                    resolve(name, "target", m, debit)
                elif mtime >= combo_time_exit:
                    resolve(name, "time", m, debit)

    for name in ARM_NAMES:
        if results[name] is None:
            resolve(name, "settle", last_minute, last_debit)
    path = np.asarray(path_rows, dtype=float) if path_rows else np.empty((0, 2))
    return results, path


# --------------------------------------------------------------------------- #
# One day: build the 0.15-delta condor at 14:00, run every management arm.
# --------------------------------------------------------------------------- #
def run_day(d: _dt.date, clf: mx.DayClassifier,
            day_data: s5.DayData | None = None,
            save_path: bool = True) -> DayRecord:
    rec = DayRecord(day=d, arms={}, fills={})
    rec.half = "train" if d <= TRAIN_END else "test"
    lab = clf.classify(d)
    rec.gamma_regime, rec.vix_regime = lab["gamma_regime"], lab["vix_regime"]

    try:
        dd = day_data if day_data is not None else s5.load_day(d)
        chain = s5.zero_dte_chain(d, day_data=dd)
        nbbo = chain.nbbo
    except Exception as e:
        rec.skip_reason = f"load error: {type(e).__name__}"
        return rec
    if nbbo.empty:
        rec.skip_reason = "no 0dte chain"
        return rec

    entry_minute = pd.Timestamp(_dt.datetime.combine(d, ENTRY_TIME))
    settle_minute = pd.Timestamp(_dt.datetime.combine(d, SETTLEMENT_TIME))
    if entry_minute not in set(nbbo["minute"].unique()):
        rec.skip_reason = "no 14:00 snapshot"
        return rec

    entry_snap = ctrl._snap_at(nbbo, entry_minute)
    sr = recon.recover_forward_spot(entry_snap, entry_minute, d)
    if sr is None:
        rec.skip_reason = "spot recon failed at entry"
        return rec
    rec.entry_spot = float(sr.spot)
    delta_tbl = recon.per_strike_delta(entry_snap, entry_minute, d, rec.entry_spot)
    build = ctrl._build_iron_condor(entry_snap, delta_tbl, TARGET_SHORT_DELTA)
    if build is None:
        rec.skip_reason = "could not build iron condor at entry"
        return rec
    rec.short_put_k = build["short_strike"]
    rec.short_call_k = build["short_strike_2"]
    rec.entry_credit = build["entry_credit"]
    rec.entry_credit_mid = _credit_mid(entry_snap, build["legs"]) or float("nan")
    if not np.isfinite(build["entry_credit"]) or build["entry_credit"] < MIN_ENTRY_CREDIT:
        rec.skip_reason = f"entry credit {build['entry_credit']:.2f} < {MIN_ENTRY_CREDIT}"
        return rec

    exits, path = _scan_managed_exits(nbbo, build["legs"], build["entry_credit"],
                                      entry_minute, settle_minute)
    # Any arm with no quoted minute to mark is a skip.
    if any(not np.isfinite(exits[n]["exit_debit"]) for n in ARM_NAMES):
        rec.skip_reason = "no quoted minute to mark/close"
        return rec

    # Persist the honest debit path off-Drive for the exact random-exit placebo.
    if save_path and len(path):
        PATHS_DIR.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(path, columns=["offset_min", "debit"]).to_parquet(
            PATHS_DIR / f"{d.strftime('%Y%m%d')}.parquet", index=False)

    rec.traded = True
    credit = build["entry_credit"]
    credit_mid = rec.entry_credit_mid
    for name in ARM_NAMES:
        ex = exits[name]
        pnl = (credit - ex["exit_debit"]) * CONTRACT_MULTIPLIER * N_CONTRACTS
        # gross (mid) P&L: mid credit at entry - mid debit at the SAME exit minute the honest
        # arm chose. Isolates the bid/ask cost drag (same trade, mid vs honest fills).
        if np.isfinite(credit_mid) and np.isfinite(ex["exit_debit_mid"]):
            pnl_mid = (credit_mid - ex["exit_debit_mid"]) * CONTRACT_MULTIPLIER * N_CONTRACTS
        else:
            pnl_mid = float("nan")
        rec.arms[name] = {"pnl": pnl, "pnl_mid": pnl_mid,
                          "exit_reason": ex["exit_reason"], "hold_min": ex["hold_min"]}

    # ---------------------- FILL BAND (pre-registered net-combo axis) ----------------------
    # Re-run the whole managed minute-walk at each fill fraction. The blended entry credit AND
    # the blended running debit both move with f, so the profit-target triggers fire at
    # different minutes -- the fraction propagates through the management logic, not applied as
    # a post-hoc P&L multiplier. Persist each fraction's debit path off-Drive for the matched
    # placebo (so the placebo is evaluated on the SAME fill the arm traded).
    for frac in FILL_FRACS:
        tag = _FILL_TAG[frac]
        credit_f = _blended_credit_to_open(entry_snap, build["legs"], frac)
        if credit_f is None or not np.isfinite(credit_f):
            rec.fills[tag] = {"entry_credit": float("nan")}
            continue
        exits_f, path_f = _scan_managed_exits_at_fill(
            nbbo, build["legs"], credit_f, frac, entry_minute, settle_minute)
        fill_block = {"entry_credit": float(credit_f)}
        for name in ARM_NAMES:
            exf = exits_f[name]
            pnl_f = (credit_f - exf["exit_debit"]) * CONTRACT_MULTIPLIER * N_CONTRACTS
            fill_block[name] = {"pnl": pnl_f, "exit_reason": exf["exit_reason"],
                                "hold_min": exf["hold_min"]}
        rec.fills[tag] = fill_block
        if save_path and len(path_f):
            fdir = PATHS_DIR / tag
            fdir.mkdir(parents=True, exist_ok=True)
            pd.DataFrame(path_f, columns=["offset_min", "debit"]).to_parquet(
                fdir / f"{d.strftime('%Y%m%d')}.parquet", index=False)
    return rec


# --------------------------------------------------------------------------- #
# Full-history run -- crash-resilient + resumable + heartbeat.
# --------------------------------------------------------------------------- #
def run_history(days: list[_dt.date] | None = None, verbose: bool = True,
                save: bool = True, resume: bool = True,
                max_new_days: int = 0) -> pd.DataFrame:
    if days is None:
        days = s5.available_days()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    clf = mx.DayClassifier()

    done_days: set[str] = set()
    if resume and _PARTIAL_CSV.is_file():
        try:
            prev = pd.read_csv(_PARTIAL_CSV, usecols=["day"])
            done_days = set(prev["day"].astype(str).unique())
        except Exception:
            done_days = set()
    if verbose and done_days:
        print(f"resume: {len(done_days)} days already done; skipping", flush=True)

    n = len(days)
    fieldnames = _flat_fieldnames()
    # Guard against a stale-schema partial: if an existing CSV's header does not match the
    # current fieldnames (e.g. the fill band was added since it was written), refuse to append
    # blindly -- that would misalign columns. Require a fresh file.
    if _PARTIAL_CSV.is_file() and not write_header_ok(fieldnames):
        raise SystemExit(
            f"{_PARTIAL_CSV} header does not match the current schema (fill band added?). "
            f"Move/delete it for a clean run, or migrate it first.")
    write_header = not _PARTIAL_CSV.is_file()

    with open(_PARTIAL_CSV, "a", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, restval="", extrasaction="ignore")
        if write_header:
            writer.writeheader()
        n_crash_skips = 0
        n_new = 0                     # not-yet-done days actually processed this invocation
        hit_chunk_cap = False
        for i, d in enumerate(days, 1):
            if str(d) in done_days:
                continue
            # CHUNK CAP: process at most `max_new_days` not-yet-done days, then exit cleanly
            # (the partial CSV is flushed per day, so the resume point is already durable).
            # This is how a FRESH process per chunk beats the OOM: RAM resets between chunks.
            if max_new_days and n_new >= max_new_days:
                hit_chunk_cap = True
                if verbose:
                    print(f"chunk cap reached: {n_new} new day(s) this run; exiting cleanly "
                          f"(resume next chunk from here).", flush=True)
                break
            dd = None
            # HARDENING: wrap the ENTIRE per-day pipeline. A single malformed tape / unquoted
            # chain / transient read error is logged with a skip_reason and SKIPPED -- it can
            # never kill the whole run (the failure mode that silently died at ~75% before).
            try:
                dd = s5.load_day(d)
                rec = run_day(d, clf, day_data=dd)
            except Exception as e:
                n_crash_skips += 1
                rec = DayRecord(day=d, arms={}, fills={})
                rec.half = "train" if d <= TRAIN_END else "test"
                rec.skip_reason = f"crash-skip: {type(e).__name__}: {str(e)[:80]}"
                if verbose:
                    print(f"[{i}/{n}] {d} CRASH-SKIP {rec.skip_reason}", flush=True)
            writer.writerow(rec.flat())
            fh.flush()
            n_new += 1
            last = rec.arms.get("A_hold", {}).get("pnl") if rec.arms else None
            if verbose and (i % 25 == 0 or i == n):
                print(f"[{i}/{n}] {d} done  (A_hold pnl={last}) "
                      f"[crash-skips so far: {n_crash_skips}]", flush=True)
            # MEMORY HYGIENE: the ~5M-row 1-min NBBO quote frame for the day lives inside `dd`
            # (and the chain/nbbo derived from it). Drop every big reference and force a GC each
            # iteration so RAM does not climb across days (the OOM that killed prior runs).
            del dd, rec
            gc.collect()
        if verbose:
            done_msg = "chunk done" if hit_chunk_cap else "run_history complete"
            print(f"{done_msg}: {n_new} new day(s) processed, "
                  f"{n_crash_skips} crash-skipped day(s).", flush=True)

    df = pd.read_csv(_PARTIAL_CSV)
    df["traded"] = df["traded"].astype(str).str.lower().isin(["true", "1"])
    df["day"] = pd.to_datetime(df["day"]).dt.date
    df = df.sort_values("day").reset_index(drop=True)
    if save:
        df.to_csv(OUTPUT_DIR / "condor_management_days.csv", index=False)
        if verbose:
            print(f"Saved {OUTPUT_DIR / 'condor_management_days.csv'}", flush=True)
    return df


# --------------------------------------------------------------------------- #
# Stats + terrain reporting.
# --------------------------------------------------------------------------- #
def _annualized_sharpe(daily_pnl: np.ndarray) -> float:
    """Sharpe of the per-day P&L series, annualized at 252 trading days. NaN if degenerate."""
    x = daily_pnl[np.isfinite(daily_pnl)]
    if len(x) < 2 or x.std(ddof=1) == 0:
        return float("nan")
    return float(x.mean() / x.std(ddof=1) * np.sqrt(252))


def _annualized_sortino(daily_pnl: np.ndarray) -> float:
    x = daily_pnl[np.isfinite(daily_pnl)]
    if len(x) < 2:
        return float("nan")
    downside = x[x < 0]
    if len(downside) < 1 or downside.std(ddof=1) == 0:
        return float("nan")
    dd = np.sqrt(np.mean(downside ** 2))   # downside deviation (vs 0 target)
    if dd == 0:
        return float("nan")
    return float(x.mean() / dd * np.sqrt(252))


def arm_stats(df: pd.DataFrame, arm: str, sub: pd.DataFrame | None = None) -> dict:
    t = (sub if sub is not None else df)
    t = t[t["traded"]].copy()
    col = f"pnl_{arm}"
    x = t[col].to_numpy(dtype=float)
    x = x[np.isfinite(x)]
    n = len(x)
    if n == 0:
        return {"arm": arm, "trades": 0, "total_$": 0.0}
    wins = x[x > 0]
    mid = t[f"pnl_mid_{arm}"].to_numpy(dtype=float)
    mid = mid[np.isfinite(mid)]
    hold = t[f"holdmin_{arm}"].to_numpy(dtype=float)
    hold = hold[np.isfinite(hold)]
    return {
        "arm": arm,
        "trades": n,
        "total_$": round(float(x.sum()), 2),
        "total_mid_$": round(float(mid.sum()), 2) if len(mid) else float("nan"),
        "cost_drag_$": round(float(mid.sum() - x.sum()), 2) if len(mid) else float("nan"),
        "win_rate": round(len(wins) / n, 4),
        "avg_$": round(float(x.mean()), 2),
        "worst_day_$": round(float(x.min()), 2),
        "p05_$": round(float(np.percentile(x, 5)), 2),
        "std_$": round(float(x.std(ddof=1)), 2),
        "sharpe_ann": round(_annualized_sharpe(x), 3),
        "sortino_ann": round(_annualized_sortino(x), 3),
        "avg_hold_min": round(float(hold.mean()), 1) if len(hold) else float("nan"),
    }


def arm_stats_fill(df: pd.DataFrame, arm: str, tag: str,
                   sub: pd.DataFrame | None = None) -> dict:
    """Per-arm stats at ONE fill fraction (column pnl_{arm}_{tag}). Same fields as arm_stats,
    minus the mid/cost-drag pair (the fill band IS the cost axis)."""
    t = (sub if sub is not None else df)
    t = t[t["traded"]].copy()
    col = f"pnl_{arm}_{tag}"
    if col not in t.columns:
        return {"arm": arm, "fill": tag, "trades": 0, "total_$": 0.0}
    x = t[col].to_numpy(dtype=float)
    x = x[np.isfinite(x)]
    n = len(x)
    if n == 0:
        return {"arm": arm, "fill": tag, "trades": 0, "total_$": 0.0}
    wins = x[x > 0]
    hcol = f"holdmin_{arm}_{tag}"
    hold = t[hcol].to_numpy(dtype=float) if hcol in t.columns else np.array([])
    hold = hold[np.isfinite(hold)]
    return {
        "arm": arm, "fill": tag, "trades": n,
        "total_$": round(float(x.sum()), 2),
        "win_rate": round(len(wins) / n, 4),
        "avg_$": round(float(x.mean()), 2),
        "worst_day_$": round(float(x.min()), 2),
        "p05_$": round(float(np.percentile(x, 5)), 2),
        "std_$": round(float(x.std(ddof=1)), 2),
        "sharpe_ann": round(_annualized_sharpe(x), 3),
        "sortino_ann": round(_annualized_sortino(x), 3),
        "avg_hold_min": round(float(hold.mean()), 1) if len(hold) else float("nan"),
    }


# --------------------------------------------------------------------------- #
# PLACEBO: matched random-exit control. For an arm with mean hold H minutes, exit each day
# at a RANDOM minute whose duration matches the arm's realized mean hold, using the SAME
# honest fill at that minute. If the arm does not beat this, "early harvest" added nothing.
# --------------------------------------------------------------------------- #
def random_exit_placebo(df: pd.DataFrame, arm: str, n_draws: int = 2000,
                        seed: int = 7, verbose: bool = False) -> dict:
    """Compare the arm's total P&L to a random-exit control matched to its mean holding time.

    Method (honest, causal-per-draw): the arm's realized mean hold is H minutes. For each of
    n_draws, we assign every traded day a random exit OFFSET (minutes after 14:00 entry) drawn
    to have MEAN ~ H, then mark that day's condor at that offset's honest debit and total the
    book. The arm beats the placebo only if its total P&L exceeds the random-exit distribution
    (report the fraction of draws the placebo does as well or better). This isolates the value
    of the exit LOGIC from the mere effect of shorter time-in-market.

    Because re-marking every day at an arbitrary minute needs that minute's honest debit, we
    reuse the per-day intraday debit PATH already captured in the days CSV IF present; otherwise
    we approximate the random-exit mark by INTERPOLATING between the entry (debit at 14:00, ~=
    credit's own open cost) and the arm's realized exit debit along the realized hold. To avoid
    inventing a path we do not have, this function operates on a PRE-COMPUTED debit-path table
    when available and falls back to the conservative two-point model otherwise. See run() --
    we pass the full per-minute debit path for the placebo to be exact.
    """
    # This wrapper is kept for API symmetry; the exact placebo is computed in
    # random_exit_placebo_from_paths using the per-minute debit paths. Here we provide the
    # honest fallback used only if paths are unavailable.
    raise NotImplementedError("use random_exit_placebo_from_paths (exact, path-based)")


def random_exit_placebo_from_paths(paths: dict, credit_by_day: dict, arm_hold_min: float,
                                   arm_total: float, n_draws: int = 2000, seed: int = 7) -> dict:
    """EXACT matched random-exit placebo using per-day honest debit PATHS.

    paths: {day -> np.ndarray of (offset_min, honest_debit) rows}, the realized minute path of
           the condor's honest debit-to-close AFTER entry (only quoted minutes).
    credit_by_day: {day -> entry_credit}.
    arm_hold_min: the arm's realized MEAN holding time (minutes) -- the target to match.
    arm_total: the arm's realized total P&L ($) -- the value the placebo must not beat.

    Each draw: for every day, pick the quoted offset closest to a random target offset drawn so
    the ACROSS-DAY MEAN offset ~ arm_hold_min (draw per-day offsets from an exponential with
    mean = arm_hold_min, clipped to the day's available offset range), mark the condor at that
    offset's honest debit, total the book. Returns the placebo distribution + the fraction of
    draws that do as well or better than the arm.
    """
    rng = np.random.default_rng(seed)
    days = [d for d in paths if len(paths[d]) > 0 and d in credit_by_day]
    # Pre-extract arrays per day for speed.
    offs = {d: paths[d][:, 0] for d in days}
    debs = {d: paths[d][:, 1] for d in days}
    credits = {d: credit_by_day[d] for d in days}

    totals = np.empty(n_draws)
    for k in range(n_draws):
        tot = 0.0
        for d in days:
            o = offs[d]; db = debs[d]
            target = rng.exponential(arm_hold_min)          # mean = arm_hold_min
            target = min(max(target, o.min()), o.max())     # clip to available range
            j = int(np.argmin(np.abs(o - target)))
            tot += (credits[d] - db[j]) * CONTRACT_MULTIPLIER * N_CONTRACTS
        totals[k] = tot
    frac_placebo_ge_arm = float(np.mean(totals >= arm_total))
    return {
        "n_days": len(days),
        "placebo_mean_$": round(float(totals.mean()), 2),
        "placebo_sd_$": round(float(totals.std()), 2),
        "placebo_p50_$": round(float(np.percentile(totals, 50)), 2),
        "placebo_p95_$": round(float(np.percentile(totals, 95)), 2),
        "arm_total_$": round(float(arm_total), 2),
        "frac_placebo_ge_arm": round(frac_placebo_ge_arm, 4),
        "arm_beats_placebo": frac_placebo_ge_arm < 0.05,   # arm in top 5% of the placebo dist
    }


# --------------------------------------------------------------------------- #
# Path cache loader (for the placebo).
# --------------------------------------------------------------------------- #
def load_paths(days: list[_dt.date], tag: str | None = None) -> dict:
    """Load the per-day debit paths from the off-Drive cache. {day -> (n,2) array}.

    tag=None loads the legacy full-cross honest paths (PATHS_DIR root). A fill tag
    (mid/f25/f50/full) loads that fraction's blended paths (PATHS_DIR/<tag>), so the matched
    placebo is evaluated on the SAME fill the arm actually traded."""
    base = PATHS_DIR if tag is None else PATHS_DIR / tag
    out = {}
    for d in days:
        p = base / f"{d.strftime('%Y%m%d')}.parquet"
        if p.is_file():
            try:
                pdf = pd.read_parquet(p)
                out[d] = pdf[["offset_min", "debit"]].to_numpy(dtype=float)
            except Exception:
                pass
    return out


# =========================================================================== #
# 45-DTE EOD BENCHMARK (longer-duration comparator the 0DTE thesis must beat).
# ~16-delta short strikes, iron condor, managed "close at 50% of max profit OR 21 DTE".
# Daily marks from the EOD SPX chains (2018-2026). Honest fills (sell shorts at BID, buy
# wings at ASK on open; reverse on close). One position at a time (no overlap) so the
# comparison is a clean single-book series, mirroring the 1-contract 0DTE control.
# =========================================================================== #
SPX_EOD_DIR = Path(r"C:\TradingDesk-Local\warehouse\raw\options\SPX")
BM_TARGET_DTE = 45
BM_SHORT_DELTA = 0.16
BM_WING_WIDTH = 50.0        # 50-point wings (a plain, liquid choice for 45-DTE SPX; not swept)
BM_PROFIT_TAKE = 0.50      # close at 50% of max profit (credit) -- the documented management rule
BM_EXIT_DTE = 21           # or at 21 DTE, whichever first
BM_MIN_CREDIT = 0.50       # skip if entry credit below this (a plain no-trade floor)


def _bm_eod_days() -> list[_dt.date]:
    days = []
    for p in sorted(SPX_EOD_DIR.glob("*.parquet")):
        try:
            days.append(_dt.datetime.strptime(p.stem, "%Y%m%d").date())
        except ValueError:
            pass
    return days


_BM_COLS = ["expiration", "strike", "right", "bid", "ask", "delta", "underlying_price"]


def _bm_load(d: _dt.date) -> pd.DataFrame | None:
    """Load one EOD SPX chain, or None if the file is missing/empty/placeholder.

    ~84 of the 2219 files are empty placeholders (holidays) with no option schema; we detect
    them via the parquet schema and skip cleanly rather than raise.
    """
    p = SPX_EOD_DIR / f"{d.strftime('%Y%m%d')}.parquet"
    if not p.is_file():
        return None
    try:
        import pyarrow.parquet as pq
        names = set(pq.ParquetFile(p).schema.names)
    except Exception:
        return None
    if not {"expiration", "strike", "right"}.issubset(names):
        return None
    return pd.read_parquet(p, columns=_BM_COLS)


def _bm_pick_short(chain_exp: pd.DataFrame, right: str, target_abs_delta: float) -> float | None:
    side = chain_exp[(chain_exp["right"] == right) & chain_exp["delta"].notna()].copy()
    side = side[(side["bid"] > 0) & (side["ask"] > 0)]
    if side.empty:
        return None
    side["d_err"] = (side["delta"].abs() - target_abs_delta).abs()
    return float(side.sort_values("d_err").iloc[0]["strike"])


def _bm_leg(chain_exp: pd.DataFrame, strike: float, right: str) -> tuple[float, float] | None:
    row = chain_exp[(chain_exp["strike"] == strike) & (chain_exp["right"] == right)]
    if row.empty:
        return None
    b, a = float(row["bid"].iloc[0]), float(row["ask"].iloc[0])
    if not (np.isfinite(b) and np.isfinite(a) and a > 0):
        return None
    return b, a


def _bm_condor_debit_to_close(chain_exp: pd.DataFrame, legs: list[tuple]) -> float | None:
    """Honest cost to close: buy back shorts at ASK, sell wings at BID."""
    total = 0.0
    for strike, right, side in legs:
        q = _bm_leg(chain_exp, strike, right)
        if q is None:
            return None
        b, a = q
        total += a if side > 0 else -b
    return total


def run_benchmark(verbose: bool = True, save: bool = True) -> pd.DataFrame:
    """45-DTE ~16-delta iron condor, one book, managed 50%-profit / 21-DTE. Daily marks.

    Enter when flat: on each EOD, find the expiration nearest 45 DTE, pick ~16-delta short
    put + call, 50-wide wings, honest fills. Hold; each subsequent EOD mark the position at
    honest debit-to-close; exit at the first EOD where open profit >= 50% of credit OR days-
    to-expiry <= 21 OR expiration reached. Book one position at a time (re-enter the next EOD
    after a close). No look-ahead: every decision uses only that EOD's chain.
    """
    days = _bm_eod_days()
    trades = []
    open_pos = None   # dict: entry_day, expiration, legs, credit
    n = len(days)
    for i, d in enumerate(days, 1):
        chain = _bm_load(d)
        if chain is None or chain.empty:
            continue
        chain = chain.copy()
        chain["exp_date"] = pd.to_datetime(chain["expiration"]).dt.date

        if open_pos is not None:
            exp = open_pos["expiration"]
            sub = chain[chain["exp_date"] == exp]
            dte = (exp - d).days
            debit = _bm_condor_debit_to_close(sub, open_pos["legs"]) if not sub.empty else None
            if debit is not None:
                open_profit = open_pos["credit"] - debit
                take = open_profit >= BM_PROFIT_TAKE * open_pos["credit"]
                time_out = dte <= BM_EXIT_DTE
                expired = dte <= 0
                if take or time_out or expired:
                    pnl = open_profit * CONTRACT_MULTIPLIER * N_CONTRACTS
                    trades.append({
                        "entry_day": open_pos["entry_day"], "exit_day": d,
                        "expiration": exp, "credit": open_pos["credit"],
                        "exit_debit": debit, "pnl_dollars": pnl,
                        "hold_days": (d - open_pos["entry_day"]).days,
                        "exit_reason": ("take" if take else ("dte21" if time_out else "expiry")),
                    })
                    open_pos = None
            elif dte <= 0:
                open_pos = None   # lost the chain at/after expiry; drop (rare)

        if open_pos is None:
            # Enter a fresh 45-DTE condor at this EOD.
            chain["dte"] = chain["exp_date"].map(lambda e: (e - d).days)
            fwd = chain[chain["dte"] >= 25]
            if not fwd.empty:
                target_exp = fwd.iloc[(fwd["dte"] - BM_TARGET_DTE).abs().argsort()].iloc[0]["exp_date"]
                sub = chain[chain["exp_date"] == target_exp]
                spk = _bm_pick_short(sub, "PUT", BM_SHORT_DELTA)
                sck = _bm_pick_short(sub, "CALL", BM_SHORT_DELTA)
                if spk is not None and sck is not None:
                    legs = [(spk, "PUT", +1), (spk - BM_WING_WIDTH, "PUT", -1),
                            (sck, "CALL", +1), (sck + BM_WING_WIDTH, "CALL", -1)]
                    # Honest entry credit: sell shorts at BID, buy wings at ASK.
                    ok = True; credit = 0.0
                    for strike, right, side in legs:
                        q = _bm_leg(sub, strike, right)
                        if q is None:
                            ok = False; break
                        b, a = q
                        credit += b if side > 0 else -a
                    if ok and credit >= BM_MIN_CREDIT:
                        open_pos = {"entry_day": d, "expiration": target_exp,
                                    "legs": legs, "credit": credit}
        if verbose and (i % 200 == 0 or i == n):
            print(f"[benchmark {i}/{n}] {d}  trades so far={len(trades)}", flush=True)

    bt = pd.DataFrame(trades)
    if save and not bt.empty:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        bt.to_csv(OUTPUT_DIR / "benchmark_45dte_trades.csv", index=False)
    return bt


def benchmark_stats(bt: pd.DataFrame) -> dict:
    if bt.empty:
        return {"trades": 0}
    x = bt["pnl_dollars"].to_numpy(dtype=float)
    wins = x[x > 0]
    # Sharpe/Sortino on a PER-TRADE basis (positions are ~monthly; annualize by trades/year).
    yrs = (pd.to_datetime(bt["exit_day"]).max() - pd.to_datetime(bt["entry_day"]).min()).days / 365.25
    trades_per_yr = len(x) / yrs if yrs > 0 else float("nan")
    sharpe = (x.mean() / x.std(ddof=1) * np.sqrt(trades_per_yr)) if x.std(ddof=1) > 0 else float("nan")
    downside = x[x < 0]
    dd = np.sqrt(np.mean(downside ** 2)) if len(downside) else float("nan")
    sortino = (x.mean() / dd * np.sqrt(trades_per_yr)) if dd and dd > 0 else float("nan")
    return {
        "trades": len(x), "total_$": round(float(x.sum()), 2),
        "win_rate": round(len(wins) / len(x), 4), "avg_$": round(float(x.mean()), 2),
        "worst_$": round(float(x.min()), 2), "std_$": round(float(x.std(ddof=1)), 2),
        "avg_hold_days": round(float(bt["hold_days"].mean()), 1),
        "trades_per_yr": round(float(trades_per_yr), 1),
        "sharpe_ann": round(float(sharpe), 3), "sortino_ann": round(float(sortino), 3),
    }


# =========================================================================== #
# Top-level terrain report.
# =========================================================================== #
def run(verbose: bool = True, save: bool = True, do_benchmark: bool = True,
        n_placebo: int = 2000) -> dict:
    df = run_history(verbose=verbose, save=save)
    t = df[df["traded"]].copy()
    days = list(t["day"])
    paths = load_paths(days)
    credit_by_day = dict(zip(t["day"], t["entry_credit"]))

    # Per-arm terrain (overall + halves + regimes).
    overall = pd.DataFrame([arm_stats(df, a) for a in ARM_NAMES])
    halves = {}
    for half in ("train", "test"):
        halves[half] = pd.DataFrame([arm_stats(df, a, sub=t[t["half"] == half]) for a in ARM_NAMES])

    # Per-year P&L per arm.
    t = t.copy()
    t["year"] = pd.to_datetime(t["day"]).dt.year
    year_tbl = {}
    for a in ARM_NAMES:
        year_tbl[a] = t.groupby("year")[f"pnl_{a}"].sum().round(2).to_dict()

    # Placebo for every arm that beats hold-to-settle (A_hold).
    a_hold_total = float(t["pnl_A_hold"].sum())
    placebos = {}
    for a in ARM_NAMES:
        if a == "A_hold":
            continue
        arm_total = float(t[f"pnl_{a}"].sum())
        arm_hold = float(t[f"holdmin_{a}"].replace([np.inf, -np.inf], np.nan).dropna().mean())
        if arm_total > a_hold_total and paths:
            placebos[a] = random_exit_placebo_from_paths(
                paths, credit_by_day, arm_hold, arm_total, n_draws=n_placebo)
        else:
            placebos[a] = {"skipped": "does not beat hold-to-settle" if arm_total <= a_hold_total
                           else "no paths cached"}

    bm = pd.DataFrame(); bm_stats = {"trades": 0}
    if do_benchmark:
        bm = run_benchmark(verbose=verbose, save=save)
        bm_stats = benchmark_stats(bm)

    if verbose:
        print("\n===== 0DTE MANAGEMENT ARMS — OVERALL (honest fills) =====", flush=True)
        with pd.option_context("display.width", 220, "display.max_columns", 30):
            print(overall.to_string(index=False), flush=True)
            for half in ("train", "test"):
                print(f"\n--- {half.upper()} ---", flush=True)
                print(halves[half].to_string(index=False), flush=True)
        print("\n===== PER-YEAR TOTAL P&L PER ARM =====", flush=True)
        print(pd.DataFrame(year_tbl).to_string(), flush=True)
        print("\n===== PLACEBO (matched random-exit) for arms that beat hold-to-settle =====",
              flush=True)
        for a, p in placebos.items():
            print(f"  {a}: {p}", flush=True)
        print("\n===== 45-DTE BENCHMARK =====", flush=True)
        print(bm_stats, flush=True)

    return {"days": df, "overall": overall, "halves": halves, "year_tbl": year_tbl,
            "placebos": placebos, "benchmark": bm, "benchmark_stats": bm_stats}


# =========================================================================== #
# FILL-BAND analysis + dated markdown report (the pre-registered deliverable).
# =========================================================================== #
def analyze_fill_band(df: pd.DataFrame, n_placebo: int = 2000, verbose: bool = True) -> dict:
    """Build the arm x fill-fraction x (overall/train/test) tables, per-year P&L per arm at the
    headline fill, and the matched random-exit placebo for every arm that beats A_hold at the
    HEADLINE fill (50%). Returns a dict of DataFrames/dicts consumed by the report writer."""
    t = df[df["traded"]].copy()
    t["year"] = pd.to_datetime(t["day"]).dt.year

    # arm x fill overall + per half.
    band = {}   # scope -> DataFrame(rows = arm x fill)
    scopes = {"overall": t, "train": t[t["half"] == "train"], "test": t[t["half"] == "test"]}
    for scope, sub in scopes.items():
        rows = []
        for a in ARM_NAMES:
            for frac in FILL_FRACS:
                rows.append(arm_stats_fill(df, a, _FILL_TAG[frac], sub=sub))
        band[scope] = pd.DataFrame(rows)

    # Total-$ pivot: arm (rows) x fill tag (cols), for the headline table.
    def total_pivot(sub):
        piv = {}
        for frac in FILL_FRACS:
            tag = _FILL_TAG[frac]
            piv[tag] = {a: round(float(sub[f"pnl_{a}_{tag}"].sum()), 0) for a in ARM_NAMES}
        return pd.DataFrame(piv).reindex(ARM_NAMES)
    total_by_fill = {s: total_pivot(sub) for s, sub in scopes.items()}

    # Per-year total P&L per arm at the HEADLINE fill.
    htag = _FILL_TAG[HEADLINE_FILL]
    year_tbl = {a: t.groupby("year")[f"pnl_{a}_{htag}"].sum().round(0).to_dict()
                for a in ARM_NAMES}

    # Per gamma/vix regime total at the headline fill.
    regime_tbl = {}
    for rk in ("gamma_regime", "vix_regime"):
        regime_tbl[rk] = {a: t.groupby(rk)[f"pnl_{a}_{htag}"].sum().round(0).to_dict()
                          for a in ARM_NAMES}

    # PLACEBO at the headline fill: any arm whose HEADLINE-fill total beats A_hold's headline
    # total gets the matched random-exit test, evaluated on the SAME (headline) fill paths.
    days = list(t["day"])
    hpaths = load_paths(days, tag=htag)
    hcredit = {row.day: row for row in t.itertuples()}
    credit_by_day = {d: float(getattr(hcredit[d], f"entry_credit_{htag}")) for d in days
                     if d in hcredit and np.isfinite(getattr(hcredit[d], f"entry_credit_{htag}"))}
    a_hold_h = float(t[f"pnl_A_hold_{htag}"].sum())
    placebos = {}
    for a in ARM_NAMES:
        if a == "A_hold":
            continue
        arm_total = float(t[f"pnl_{a}_{htag}"].sum())
        arm_hold = float(t[f"holdmin_{a}_{htag}"].replace([np.inf, -np.inf], np.nan).dropna().mean())
        if arm_total > a_hold_h and hpaths and credit_by_day:
            placebos[a] = random_exit_placebo_from_paths(
                hpaths, credit_by_day, arm_hold, arm_total, n_draws=n_placebo)
        else:
            placebos[a] = {"skipped": ("does not beat A_hold at headline fill"
                                       if arm_total <= a_hold_h else "no headline paths cached")}
    if verbose:
        print("\n===== TOTAL $ by ARM x FILL (overall) =====", flush=True)
        print(total_by_fill["overall"].to_string(), flush=True)
        print("\n===== HEADLINE-FILL (50%) PLACEBO =====", flush=True)
        for a, p in placebos.items():
            print(f"  {a}: {p}", flush=True)
    return {"band": band, "total_by_fill": total_by_fill, "year_tbl": year_tbl,
            "regime_tbl": regime_tbl, "placebos": placebos, "headline_tag": htag,
            "a_hold_headline_total": a_hold_h}


def _md_table(dframe: pd.DataFrame, floatfmt: str = "{:,.0f}") -> str:
    """Render a DataFrame as a GitHub markdown table (index becomes the first column)."""
    d = dframe.copy()
    cols = list(d.columns)
    head = "| " + " | ".join([str(d.index.name or "")] + [str(c) for c in cols]) + " |"
    sep = "| " + " | ".join(["---"] * (len(cols) + 1)) + " |"
    lines = [head, sep]
    for idx, row in d.iterrows():
        cells = []
        for c in cols:
            v = row[c]
            if isinstance(v, float) and np.isfinite(v):
                cells.append(floatfmt.format(v))
            else:
                cells.append(str(v))
        lines.append("| " + " | ".join([str(idx)] + cells) + " |")
    return "\n".join(lines)


def write_markdown_report(df: pd.DataFrame, fb: dict, bm_stats: dict, crash_note: str,
                          out_path: Path) -> Path:
    """Write the dated deliverable: crash diagnosis, arm x fill x split tables, placebo verdict,
    explicit VERDICT + precondition outcome."""
    htag = fb["headline_tag"]
    tot = fb["total_by_fill"]
    band = fb["band"]
    placebos = fb["placebos"]

    traded = df[df["traded"]].copy()
    n_traded = len(traded)
    n_days = len(df)
    n_skip = int((~df["traded"]).sum())
    crash_skips = df["skip_reason"].astype(str).str.startswith("crash-skip").sum()
    dmin, dmax = df["day"].min(), df["day"].max()

    # Headline determinations.
    a_hold_h = fb["a_hold_headline_total"]
    manage_beats_hold = {a: float(traded[f"pnl_{a}_{htag}"].sum()) - a_hold_h
                         for a in ARM_NAMES if a != "A_hold"}
    any_pos_headline = {a: float(traded[f"pnl_{a}_{htag}"].sum()) for a in ARM_NAMES}
    # Does ANY arm hold positive across mid->50% AND OOS?
    def arm_pos_band(a):
        mids = {tg: float(traded[f"pnl_{a}_{tg}"].sum()) for tg in ("mid", "f25", "f50")}
        test = float(traded[traded["half"] == "test"][f"pnl_{a}_{htag}"].sum())
        return all(v > 0 for v in mids.values()) and test > 0
    robust_arms = [a for a in ARM_NAMES if arm_pos_band(a)]

    L = []
    L.append(f"# 0DTE Iron-Condor MANAGEMENT terrain map — finished-window report\n")
    L.append(f"_Generated {_dt.date.today().isoformat()}. Window {dmin} -> {dmax}, "
             f"{n_days} session-days ({n_traded} traded, {n_skip} no-trade/skip, "
             f"{crash_skips} crash-skipped). PAPER / research only._\n")

    L.append("## 1. Crash diagnosis + hardening\n")
    L.append(crash_note + "\n")

    L.append("## 2. Total P&L ($) by ARM x FILL FRACTION — OVERALL\n")
    L.append("Fill fractions of the NET COMBO spread: `mid`=0% (optimistic), `f25`=25%, "
             "`f50`=50% (**HEADLINE**), `full`=100% worst-side (the control's honest bound). "
             "The fraction propagates through the profit-target triggers.\n")
    L.append(_md_table(tot["overall"]) + "\n")
    L.append("### TRAIN (2022-01 .. 2024-06)\n")
    L.append(_md_table(tot["train"]) + "\n")
    L.append("### TEST / OOS (2024-07 .. end)\n")
    L.append(_md_table(tot["test"]) + "\n")

    L.append("## 3. Full per-arm stats at the HEADLINE 50% fill\n")
    hband = band["overall"][band["overall"]["fill"] == htag].set_index("arm")
    keep = [c for c in ("trades", "total_$", "win_rate", "avg_$", "worst_day_$", "p05_$",
                        "std_$", "sharpe_ann", "sortino_ann", "avg_hold_min") if c in hband.columns]
    L.append(_md_table(hband[keep], floatfmt="{:,.3f}") + "\n")

    L.append("## 4. Per-year total P&L per arm (headline 50% fill)\n")
    ydf = pd.DataFrame(fb["year_tbl"]).T
    ydf.index.name = "arm"
    L.append(_md_table(ydf) + "\n")

    L.append("## 5. Per-regime total P&L per arm (headline 50% fill)\n")
    for rk, tbl in fb["regime_tbl"].items():
        rdf = pd.DataFrame(tbl).T
        rdf.index.name = "arm"
        L.append(f"### by {rk}\n")
        L.append(_md_table(rdf) + "\n")

    L.append("## 6. Matched random-exit PLACEBO (headline 50% fill)\n")
    L.append("Run only for arms whose headline-fill total beats hold-to-settle (A_hold). "
             "`arm_beats_placebo=True` means the management logic clears the 5% bar vs a random "
             "exit matched to the same mean holding time.\n")
    if placebos:
        prows = []
        for a, p in placebos.items():
            if "skipped" in p:
                prows.append({"arm": a, "verdict": p["skipped"]})
            else:
                prows.append({"arm": a, "arm_total_$": p["arm_total_$"],
                              "placebo_p50_$": p["placebo_p50_$"],
                              "placebo_p95_$": p["placebo_p95_$"],
                              "frac_placebo_ge_arm": p["frac_placebo_ge_arm"],
                              "arm_beats_placebo": p["arm_beats_placebo"]})
        pdf = pd.DataFrame(prows).set_index("arm")
        L.append(_md_table(pdf, floatfmt="{:,.4g}") + "\n")
    else:
        L.append("_No arm beat A_hold at the headline fill — no placebo needed._\n")

    L.append("## 7. 45-DTE benchmark (longer-duration comparator)\n")
    L.append("```\n" + str(bm_stats) + "\n```\n")

    # --------------------- explicit VERDICT ---------------------
    L.append("## 8. VERDICT + precondition outcome\n")
    L.append(f"**(a) Real net-positive edge at the 50% fill holding mid->50% AND OOS?** "
             f"Arms passing that band test: {robust_arms if robust_arms else 'NONE'}.\n")
    beats_on_total = [a for a, d in manage_beats_hold.items() if d > 0]
    L.append(f"**(b) Do management arms (B/C/D) beat plain hold-to-settle (A) on TOTAL P&L at "
             f"the 50% fill?** Arms beating A on total-$: {beats_on_total if beats_on_total else 'NONE'} "
             f"(A_hold headline total = ${a_hold_h:,.0f}).\n")
    # win-rate check
    a_wr = float((traded["pnl_A_hold_%s" % htag] > 0).mean())
    wr_beats = [a for a in ARM_NAMES if a != "A_hold"
                and float((traded[f"pnl_{a}_{htag}"] > 0).mean()) > a_wr]
    L.append(f"   Arms beating A on WIN RATE: {wr_beats} (A_hold win rate = {a_wr:.3f}).\n")
    passed_placebo = [a for a, p in placebos.items()
                      if isinstance(p, dict) and p.get("arm_beats_placebo")]
    precondition_met = bool(robust_arms) and bool(passed_placebo)
    L.append(f"**(c) Base-edge PRECONDITION for the regime-modulation follow-on:** "
             f"{'MET' if precondition_met else 'NOT MET'}. "
             f"Arms clearing BOTH the mid->50%/OOS band AND the placebo: "
             f"{[a for a in robust_arms if a in passed_placebo] or 'NONE'}.\n")
    if not precondition_met:
        L.append("\n> **The regime-conditioned profit-target modulation follow-on stays GATED "
                 "and is NOT run.** Per the pre-registration, a dynamic overlay is not used to "
                 "rescue an edgeless base.\n")
    else:
        L.append("\n> Base precondition MET — the regime-modulation follow-on is unlocked "
                 "(a separate, still-pre-registered test).\n")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(L), encoding="utf-8")
    return out_path


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="0DTE iron-condor MANAGEMENT/exit experiment")
    ap.add_argument("--no-save", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="run only the first N days (smoke test)")
    ap.add_argument("--history-only", action="store_true", help="run the 0DTE history, skip report")
    ap.add_argument("--resume", action="store_true",
                    help="resume from the partial CSV (default behavior; explicit for clarity)")
    ap.add_argument("--max-new-days", type=int, default=0,
                    help="process at most N not-yet-done days this run, then exit cleanly "
                         "(fresh-process chunk loop to beat OOM). 0 = no cap.")
    ap.add_argument("--no-benchmark", action="store_true")
    ap.add_argument("--report-only", action="store_true",
                    help="load the finished days CSV and (re)build the dated markdown report")
    args = ap.parse_args()
    if args.report_only:
        _df = pd.read_csv(OUTPUT_DIR / "condor_management_days.csv")
        _df["traded"] = _df["traded"].astype(str).str.lower().isin(["true", "1"])
        _df["day"] = pd.to_datetime(_df["day"]).dt.date
        _fb = analyze_fill_band(_df, verbose=not args.quiet)
        _bm = benchmark_stats(run_benchmark(verbose=not args.quiet, save=False)) \
            if not args.no_benchmark else {"trades": 0}
        _crash = ("(report-only rebuild — see the run log for the crash diagnosis.)")
        _out = write_markdown_report(
            _df, _fb, _bm, _crash,
            OUTPUT_DIR.parent / f"condor_management_{_dt.date.today():%Y%m%d}.md")
        print(f"Report written: {_out}", flush=True)
    elif args.history_only or args.limit or args.resume or args.max_new_days:
        days = s5.available_days()
        if args.limit:
            days = days[: args.limit]
        run_history(days=days, verbose=not args.quiet, save=not args.no_save,
                    max_new_days=args.max_new_days)
    else:
        run(verbose=not args.quiet, save=not args.no_save,
            do_benchmark=not args.no_benchmark)
