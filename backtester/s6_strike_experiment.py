r"""
s6_strike_experiment.py — the S6 STRIKE-SELECTION experiment (zone vs blind delta).

PAPER / research only. OFFLINE. Windows. STRICTLY READ-ONLY on the warehouse.

THE CORE TEST
-------------
Does Brandon Wendell's supply/demand ZONE method pick better SHORT strikes than blind delta
placement? Everything except the short-strike RULE is frozen at the control's documented
constants (14:00 entry, 5-wide, $0.05 winner, 2x-credit stop, $0.30 min credit, HONEST
bid/ask fills, 1 contract). We reuse s6_control's fill/P&L engine UNCHANGED.

THREE ARMS (per day, per structure: bull_put / bear_call / iron_condor)
-----------------------------------------------------------------------
  A  blind control      : short strike at fixed 0.15 delta (= the existing s6_control output).
  B  zone placement     : short put just below the nearest fresh DEMAND zone below spot;
                          short call just above the nearest fresh SUPPLY zone above spot.
                          NO qualifying fresh zone that day => NO TRADE (recorded as a skip;
                          we NEVER fall back to delta).
  C  delta-matched blind: read the DELTA that arm B's zone strike landed at that day, and
                          place a blind strike at that SAME delta (nearest strike), with no
                          zone input.

  *** IMPORTANT — per-day arm C is DEGENERATE by construction, and that is itself a finding. ***
  On a discrete 5-point strike grid the map strike->delta is a bijection: the strike whose
  |delta| is nearest to "the zone strike's own delta" IS the zone strike (error zero). So
  per-day arm C reproduces arm B's exact strike on essentially every day (verified on the
  eyeball sample: B==C strike every time). A same-day delta-match therefore has NO power to
  separate "the zone" from "its delta." We still RECORD arm C (it makes the degeneracy
  auditable), but the REAL, non-degenerate fooling-guard is the DELTA-STRATIFIED POOLED test
  below.

  FOOLING-GUARD (the version with power): DELTA-STRATIFIED POOLED comparison of B vs the blind
  pool. We bin every trade by short-strike |delta| (fixed 0.05-wide bins), and within each
  delta bin compare the ZONE trades (B) against the BLIND trades placed by delta alone (the
  arm-A pool, whose strikes span all deltas). If, at MATCHED delta, zone-placed strikes do NOT
  breach less / earn more reward-for-risk than blind strikes at the same delta, the zone adds
  nothing beyond the delta it implies — it is COSMETIC. This is the honest, powered fooling
  test and is what the B-vs-blind@matched-delta verdict is computed from.

REWARD-FOR-RISK LENS (Andrew's refinement): the credit collected is a REAL observable read
from the historical NBBO at the chosen strike — NOT a tuned parameter — so we do NOT gate on
the documented $0.30 min credit (that would silently discard the far-OTM zone trades we are
studying and hide the economics). We take EVERY qualifying zone trade, record its actual
credit, and REPORT results bucketed by credit received (<$0.30 / $0.30-0.50 / $0.50+) plus a
$0.30-floored subset for comparison to the documented rule. The edge, if real, is the zone
earning MORE credit per unit of BREACH RISK than blind placement at the same delta.

PRIMARY METRIC: short-strike BREACH RATE — fraction of trades where the recovered SPX spot
reaches/exceeds the short strike before PM settlement (a direct, low-noise measure of strike
quality), shown alongside avg credit collected (reward-for-risk). Also win rate, avg win/loss,
loss/win, total P&L, worst day, max consec losing days. Reported per arm x structure, both
time-halves, per day-type bucket, and per credit bucket.

NO CURVE-FITTING (rule #1): the zone definition is FROZEN in s6_zones (pre-registered). We do
NOT sweep the zone params, we do NOT pick a best interpretation, we do NOT select a config. We
report the surface and the two verdicts (B-vs-A, B-vs-C) with a plateau/peak robustness call.

NO LOOK-AHEAD: strike selection (all arms) uses ONLY the 14:00 snapshot + bars closed by 14:00.
The exit / breach scan walks minutes forward and stops at the first firing minute. The day
classifier uses only the PRIOR EOD. All pinned by tests.

CRASH-RESILIENT + RESUMABLE: per-day incremental CSV append + resume-skip, like the prior
harnesses — a killed run loses at most the in-flight day.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, asdict, field
from pathlib import Path

import numpy as np
import pandas as pd

import s5_intraday_data as s5
import s6_recon as recon
import s6_control as ctrl
import s6_zones as zones
import s6_matrix as mx

# --------------------------------------------------------------------------- #
# Frozen constants — inherited verbatim from the control (NOT re-tuned here).
# --------------------------------------------------------------------------- #
ENTRY_TIME = ctrl.ENTRY_TIME              # 14:00 ET
SETTLEMENT_TIME = ctrl.SETTLEMENT_TIME    # 16:00 ET
SPREAD_WIDTH = ctrl.SPREAD_WIDTH          # 5.0
TARGET_SHORT_DELTA = ctrl.TARGET_SHORT_DELTA  # 0.15 (arm A only)
MIN_ENTRY_CREDIT = ctrl.MIN_ENTRY_CREDIT  # 0.30
CONTRACT_MULTIPLIER = ctrl.CONTRACT_MULTIPLIER
N_CONTRACTS = ctrl.N_CONTRACTS

OUTPUT_DIR = ctrl.OUTPUT_DIR
_PARTIAL_CSV = OUTPUT_DIR / "s6_strike_experiment_partial.csv"

ARMS = ("A_blind015", "B_zone", "C_deltamatched")
STRUCTURES = ("bull_put", "bear_call", "iron_condor")


# --------------------------------------------------------------------------- #
# Trade record — one per (day, structure, arm).
# --------------------------------------------------------------------------- #
@dataclass
class ArmTrade:
    day: _dt.date
    structure: str
    arm: str
    traded: bool = False
    skip_reason: str = ""
    spot_entry: float = float("nan")
    entry_credit: float = float("nan")
    short_strike: float = float("nan")
    long_strike: float = float("nan")
    short_strike_2: float = float("nan")   # IC call side
    long_strike_2: float = float("nan")
    entry_short_delta: float = float("nan")   # put side (or single side)
    entry_short_delta_2: float = float("nan")  # IC call side
    # zone provenance (arm B only)
    zone_tf: float = float("nan")
    zone_low: float = float("nan")
    zone_high: float = float("nan")
    zone_pattern: str = ""
    zone_tf_2: float = float("nan")           # IC supply side
    zone_low_2: float = float("nan")
    zone_high_2: float = float("nan")
    zone_pattern_2: str = ""
    exit_reason: str = ""
    exit_minute: pd.Timestamp | None = None
    exit_debit: float = float("nan")
    pnl_points: float = float("nan")
    pnl_dollars: float = float("nan")
    credit_bucket: str = ""                     # '<0.30' | '0.30-0.50' | '0.50+'
    meets_min_credit: bool = False             # credit >= documented $0.30 floor (REPORTED, not a gate)
    breached: bool = False                     # PRIMARY: short strike reached/exceeded
    day_type_gamma: str = "unknown"
    day_type_vix: str = "unknown"
    half: str = ""


# --------------------------------------------------------------------------- #
# Spot series recovery for the whole session (for zone bars + breach scan).
# --------------------------------------------------------------------------- #
def recover_spot_series(nbbo: pd.DataFrame, day: _dt.date) -> pd.Series:
    """Per-minute recovered SPX spot for the whole 0DTE session (put-call parity each minute).

    Returns a Series indexed by minute timestamp. Minutes where recon fails are NaN (dropped
    by the zone resampler). This is the SAME recon used by the control at entry, just applied
    to every minute so we can build intraday bars and scan the breach path.
    """
    out = {}
    for m in sorted(nbbo["minute"].unique()):
        snap = nbbo[nbbo["minute"] == m][["strike", "right", "bid", "ask"]]
        sr = recon.recover_forward_spot(snap, pd.Timestamp(m), day)
        if sr is not None and np.isfinite(sr.spot):
            out[pd.Timestamp(m)] = sr.spot
    if not out:
        return pd.Series(dtype=float)
    return pd.Series(out).sort_index()


# --------------------------------------------------------------------------- #
# Strike pickers per arm.
# --------------------------------------------------------------------------- #
def _round_to_5(x: float) -> float:
    return round(x / 5.0) * 5.0


def _pick_strike_below_demand(zone: zones.Zone) -> float:
    """Short put = nearest 5-point strike AT OR BELOW the demand zone low (Brandon's rule)."""
    return float(np.floor(zone.zone_low / 5.0) * 5.0)


def _pick_strike_above_supply(zone: zones.Zone) -> float:
    """Short call = nearest 5-point strike AT OR ABOVE the supply zone high (Brandon's rule)."""
    return float(np.ceil(zone.zone_high / 5.0) * 5.0)


def _delta_at_strike(delta_tbl: pd.DataFrame, strike: float, right: str) -> float:
    row = delta_tbl[(delta_tbl["strike"] == strike) & (delta_tbl["right"] == right)]
    if row.empty or not np.isfinite(row["delta"].iloc[0]):
        return float("nan")
    return float(row["delta"].iloc[0])


def _pick_short_by_delta(delta_tbl: pd.DataFrame, right: str, target_abs: float) -> float | None:
    """Nearest strike to a target |delta| on a side (same rule the control uses)."""
    side = delta_tbl[(delta_tbl["right"] == right) & (delta_tbl["delta"].notna())].copy()
    if side.empty:
        return None
    side["d_err"] = (side["delta"].abs() - target_abs).abs()
    return float(side.sort_values("d_err").iloc[0]["strike"])


# --------------------------------------------------------------------------- #
# Build one spread leg-set from chosen short strikes, honest fills at entry.
# --------------------------------------------------------------------------- #
def _build_put_side(snap, short_k) -> dict | None:
    long_k = short_k - SPREAD_WIDTH
    sq = ctrl._leg_quote(snap, short_k, "PUT")
    lq = ctrl._leg_quote(snap, long_k, "PUT")
    if sq is None or lq is None:
        return None
    credit = ctrl._credit_to_open(sq[0], lq[1])  # short_bid - long_ask
    return {"short": short_k, "long": long_k, "credit": credit,
            "legs": [(short_k, "PUT", +1), (long_k, "PUT", -1)]}


def _build_call_side(snap, short_k) -> dict | None:
    long_k = short_k + SPREAD_WIDTH
    sq = ctrl._leg_quote(snap, short_k, "CALL")
    lq = ctrl._leg_quote(snap, long_k, "CALL")
    if sq is None or lq is None:
        return None
    credit = ctrl._credit_to_open(sq[0], lq[1])
    return {"short": short_k, "long": long_k, "credit": credit,
            "legs": [(short_k, "CALL", +1), (long_k, "CALL", -1)]}


# --------------------------------------------------------------------------- #
# Breach detection (PRIMARY metric) — did spot reach/exceed the short strike?
# --------------------------------------------------------------------------- #
def _breached(spot_after: pd.Series, structure: str, put_short: float, call_short: float) -> bool:
    """True iff the recovered spot reaches/exceeds a SHORT strike after entry (<= settlement).

    bull_put   : breach if spot <= short put.
    bear_call  : breach if spot >= short call.
    iron_condor: breach if EITHER side breaches.
    `spot_after` = recovered spot for minutes strictly after entry, up to settlement.
    """
    if spot_after.empty:
        return False
    lo = float(spot_after.min())
    hi = float(spot_after.max())
    put_breach = np.isfinite(put_short) and lo <= put_short
    call_breach = np.isfinite(call_short) and hi >= call_short
    if structure == "bull_put":
        return bool(put_breach)
    if structure == "bear_call":
        return bool(call_breach)
    return bool(put_breach or call_breach)


# --------------------------------------------------------------------------- #
# One day: build all arms for all structures (shares the entry snapshot + spot series).
# --------------------------------------------------------------------------- #
def run_day(d: _dt.date, clf: mx.DayClassifier, day_data: s5.DayData | None = None) -> list[ArmTrade]:
    """Compute every (structure, arm) trade for one day. Returns a flat list of ArmTrade.

    Never raises on a single-day data quirk — bad days yield non-traded rows with a reason.
    """
    results: list[ArmTrade] = []
    half = "train" if d <= mx.TRAIN_END else "test"
    lab = clf.classify(d)
    g_reg, v_reg = lab["gamma_regime"], lab["vix_regime"]

    def blank(structure, arm, reason) -> ArmTrade:
        return ArmTrade(day=d, structure=structure, arm=arm, traded=False,
                        skip_reason=reason, day_type_gamma=g_reg, day_type_vix=v_reg,
                        half=half)

    try:
        dd = day_data if day_data is not None else s5.load_day(d)
        chain = s5.zero_dte_chain(d, day_data=dd)
        nbbo = chain.nbbo
    except Exception as e:
        for structure in STRUCTURES:
            for arm in ARMS:
                results.append(blank(structure, arm, f"load error: {type(e).__name__}"))
        return results

    if nbbo.empty:
        for structure in STRUCTURES:
            for arm in ARMS:
                results.append(blank(structure, arm, "no 0dte chain"))
        return results

    entry_minute = pd.Timestamp(_dt.datetime.combine(d, ENTRY_TIME))
    settle_minute = pd.Timestamp(_dt.datetime.combine(d, SETTLEMENT_TIME))
    minute_set = set(nbbo["minute"].unique())
    if entry_minute not in minute_set:
        for structure in STRUCTURES:
            for arm in ARMS:
                results.append(blank(structure, arm, "no 14:00 snapshot"))
        return results

    entry_snap = ctrl._snap_at(nbbo, entry_minute)
    sr = recon.recover_forward_spot(entry_snap, entry_minute, d)
    if sr is None:
        for structure in STRUCTURES:
            for arm in ARMS:
                results.append(blank(structure, arm, "spot recon failed at entry"))
        return results
    spot = sr.spot
    delta_tbl = recon.per_strike_delta(entry_snap, entry_minute, d, spot)

    # Full-session spot series for zone bars (causal to 14:00) + breach path (after entry).
    spot_series = recover_spot_series(nbbo, d)
    spot_after = spot_series[(spot_series.index > entry_minute)
                             & (spot_series.index <= settle_minute)]

    # Zone universe as of 14:00 (only bars closed by 14:00 => no look-ahead).
    universe = zones.build_zone_universe(spot_series, d, entry_minute)
    demand_zone = zones.select_zone(universe, spot, "demand")
    supply_zone = zones.select_zone(universe, spot, "supply")

    # ------------------------------------------------------------------- #
    # Helper: finish a trade given a leg-set, credit, short strikes, deltas.
    # ------------------------------------------------------------------- #
    def finish(tr: ArmTrade, structure, legs, credit, put_short, call_short):
        # NOTE (Andrew's design refinement): the $0.30 min-credit is NOT a silent skip gate.
        # The credit is a REAL observable read from the historical NBBO at the chosen strike;
        # gating on it would discard the far-OTM zone trades we want to study and hide the
        # economics. So we take EVERY qualifying trade regardless of credit, record the actual
        # collected credit, and REPORT it bucketed. The documented $0.30 floor is kept only as
        # a reported flag (meets_min_credit), never a filter. A degenerate/negative credit that
        # cannot even be marked is still skipped (that is a data/quote failure, not a rule).
        if not np.isfinite(credit):
            tr.skip_reason = "credit not computable (unquoted legs)"
            return tr
        reason, exit_minute, exit_debit = ctrl._scan_exit(
            nbbo, legs, credit, entry_minute, settle_minute)
        if not np.isfinite(exit_debit):
            tr.skip_reason = "no quoted minute to mark/close"
            return tr
        tr.traded = True
        tr.entry_credit = credit
        tr.meets_min_credit = bool(credit >= MIN_ENTRY_CREDIT)
        tr.credit_bucket = ("<0.30" if credit < 0.30
                            else ("0.30-0.50" if credit < 0.50 else "0.50+"))
        tr.exit_reason = reason
        tr.exit_minute = exit_minute
        tr.exit_debit = exit_debit
        tr.pnl_points = credit - exit_debit
        tr.pnl_dollars = tr.pnl_points * CONTRACT_MULTIPLIER * N_CONTRACTS
        tr.breached = _breached(spot_after, structure, put_short, call_short)
        return tr

    # ------------------------------------------------------------------- #
    # Per structure, build all three arms.
    # ------------------------------------------------------------------- #
    for structure in STRUCTURES:
        # ---- ARM B: zone placement (the crown-jewel arm). Also drives arm C's delta. ----
        trB = blank(structure, "B_zone", "")
        trB.spot_entry = spot
        b_put_delta = float("nan")
        b_call_delta = float("nan")
        b_put_short = float("nan")
        b_call_short = float("nan")
        b_ok = True

        if structure in ("bull_put", "iron_condor"):
            if demand_zone is None:
                b_ok = False
                trB.skip_reason = "no fresh demand zone"
            else:
                b_put_short = _pick_strike_below_demand(demand_zone)
        if b_ok and structure in ("bear_call", "iron_condor"):
            if supply_zone is None:
                b_ok = False
                trB.skip_reason = "no fresh supply zone" if not trB.skip_reason else \
                    "no fresh demand+supply zone"
            else:
                b_call_short = _pick_strike_above_supply(supply_zone)

        b_build_put = b_build_call = None
        if b_ok and structure in ("bull_put", "iron_condor"):
            b_build_put = _build_put_side(entry_snap, b_put_short)
            if b_build_put is None:
                b_ok = False
                trB.skip_reason = "put legs unquoted at zone strike"
        if b_ok and structure in ("bear_call", "iron_condor"):
            b_build_call = _build_call_side(entry_snap, b_call_short)
            if b_build_call is None:
                b_ok = False
                trB.skip_reason = "call legs unquoted at zone strike"

        if b_ok:
            if structure == "bull_put":
                legs, credit = b_build_put["legs"], b_build_put["credit"]
                b_put_delta = _delta_at_strike(delta_tbl, b_put_short, "PUT")
                trB.short_strike, trB.long_strike = b_build_put["short"], b_build_put["long"]
                trB.entry_short_delta = b_put_delta
                trB.zone_tf, trB.zone_low, trB.zone_high, trB.zone_pattern = (
                    demand_zone.timeframe_min, demand_zone.zone_low,
                    demand_zone.zone_high, demand_zone.pattern)
            elif structure == "bear_call":
                legs, credit = b_build_call["legs"], b_build_call["credit"]
                b_call_delta = _delta_at_strike(delta_tbl, b_call_short, "CALL")
                trB.short_strike, trB.long_strike = b_build_call["short"], b_build_call["long"]
                trB.entry_short_delta = b_call_delta
                trB.zone_tf, trB.zone_low, trB.zone_high, trB.zone_pattern = (
                    supply_zone.timeframe_min, supply_zone.zone_low,
                    supply_zone.zone_high, supply_zone.pattern)
            else:  # iron_condor
                legs = b_build_put["legs"] + b_build_call["legs"]
                credit = b_build_put["credit"] + b_build_call["credit"]
                b_put_delta = _delta_at_strike(delta_tbl, b_put_short, "PUT")
                b_call_delta = _delta_at_strike(delta_tbl, b_call_short, "CALL")
                trB.short_strike, trB.long_strike = b_build_put["short"], b_build_put["long"]
                trB.short_strike_2, trB.long_strike_2 = b_build_call["short"], b_build_call["long"]
                trB.entry_short_delta = b_put_delta
                trB.entry_short_delta_2 = b_call_delta
                trB.zone_tf, trB.zone_low, trB.zone_high, trB.zone_pattern = (
                    demand_zone.timeframe_min, demand_zone.zone_low,
                    demand_zone.zone_high, demand_zone.pattern)
                trB.zone_tf_2, trB.zone_low_2, trB.zone_high_2, trB.zone_pattern_2 = (
                    supply_zone.timeframe_min, supply_zone.zone_low,
                    supply_zone.zone_high, supply_zone.pattern)
            trB = finish(trB, structure, legs, credit, b_put_short, b_call_short)
        results.append(trB)

        # ---- ARM A: blind 0.15 delta (rebuild here so breach + spot are consistent). ----
        trA = blank(structure, "A_blind015", "")
        trA.spot_entry = spot
        a_put_short = a_call_short = float("nan")
        a_ok = True
        a_build_put = a_build_call = None
        if structure in ("bull_put", "iron_condor"):
            k = _pick_short_by_delta(delta_tbl, "PUT", TARGET_SHORT_DELTA)
            if k is None:
                a_ok = False
                trA.skip_reason = "no PUT delta strike"
            else:
                a_put_short = k
                a_build_put = _build_put_side(entry_snap, k)
                if a_build_put is None:
                    a_ok = False
                    trA.skip_reason = "put legs unquoted (A)"
        if a_ok and structure in ("bear_call", "iron_condor"):
            k = _pick_short_by_delta(delta_tbl, "CALL", TARGET_SHORT_DELTA)
            if k is None:
                a_ok = False
                trA.skip_reason = "no CALL delta strike"
            else:
                a_call_short = k
                a_build_call = _build_call_side(entry_snap, k)
                if a_build_call is None:
                    a_ok = False
                    trA.skip_reason = "call legs unquoted (A)"
        if a_ok:
            if structure == "bull_put":
                legs, credit = a_build_put["legs"], a_build_put["credit"]
                trA.short_strike, trA.long_strike = a_build_put["short"], a_build_put["long"]
                trA.entry_short_delta = _delta_at_strike(delta_tbl, a_put_short, "PUT")
            elif structure == "bear_call":
                legs, credit = a_build_call["legs"], a_build_call["credit"]
                trA.short_strike, trA.long_strike = a_build_call["short"], a_build_call["long"]
                trA.entry_short_delta = _delta_at_strike(delta_tbl, a_call_short, "CALL")
            else:
                legs = a_build_put["legs"] + a_build_call["legs"]
                credit = a_build_put["credit"] + a_build_call["credit"]
                trA.short_strike, trA.long_strike = a_build_put["short"], a_build_put["long"]
                trA.short_strike_2, trA.long_strike_2 = a_build_call["short"], a_build_call["long"]
                trA.entry_short_delta = _delta_at_strike(delta_tbl, a_put_short, "PUT")
                trA.entry_short_delta_2 = _delta_at_strike(delta_tbl, a_call_short, "CALL")
            trA = finish(trA, structure, legs, credit, a_put_short, a_call_short)
        results.append(trA)

        # ---- ARM C: delta-matched blind (uses arm B's zone-implied delta, no zone). ----
        # Only defined if arm B produced a zone strike + a finite delta for the needed side(s).
        trC = blank(structure, "C_deltamatched", "")
        trC.spot_entry = spot
        c_put_short = c_call_short = float("nan")
        c_ok = True
        need_put = structure in ("bull_put", "iron_condor")
        need_call = structure in ("bear_call", "iron_condor")
        if not b_ok:
            c_ok = False
            trC.skip_reason = "arm B produced no zone => no delta to match"
        if c_ok and need_put and not np.isfinite(b_put_delta):
            c_ok = False
            trC.skip_reason = "no zone put delta to match"
        if c_ok and need_call and not np.isfinite(b_call_delta):
            c_ok = False
            trC.skip_reason = "no zone call delta to match"

        c_build_put = c_build_call = None
        if c_ok and need_put:
            k = _pick_short_by_delta(delta_tbl, "PUT", abs(b_put_delta))
            if k is None:
                c_ok = False
                trC.skip_reason = "no PUT strike near matched delta"
            else:
                c_put_short = k
                c_build_put = _build_put_side(entry_snap, k)
                if c_build_put is None:
                    c_ok = False
                    trC.skip_reason = "put legs unquoted (C)"
        if c_ok and need_call:
            k = _pick_short_by_delta(delta_tbl, "CALL", abs(b_call_delta))
            if k is None:
                c_ok = False
                trC.skip_reason = "no CALL strike near matched delta"
            else:
                c_call_short = k
                c_build_call = _build_call_side(entry_snap, k)
                if c_build_call is None:
                    c_ok = False
                    trC.skip_reason = "call legs unquoted (C)"
        if c_ok:
            if structure == "bull_put":
                legs, credit = c_build_put["legs"], c_build_put["credit"]
                trC.short_strike, trC.long_strike = c_build_put["short"], c_build_put["long"]
                trC.entry_short_delta = _delta_at_strike(delta_tbl, c_put_short, "PUT")
            elif structure == "bear_call":
                legs, credit = c_build_call["legs"], c_build_call["credit"]
                trC.short_strike, trC.long_strike = c_build_call["short"], c_build_call["long"]
                trC.entry_short_delta = _delta_at_strike(delta_tbl, c_call_short, "CALL")
            else:
                legs = c_build_put["legs"] + c_build_call["legs"]
                credit = c_build_put["credit"] + c_build_call["credit"]
                trC.short_strike, trC.long_strike = c_build_put["short"], c_build_put["long"]
                trC.short_strike_2, trC.long_strike_2 = c_build_call["short"], c_build_call["long"]
                trC.entry_short_delta = _delta_at_strike(delta_tbl, c_put_short, "PUT")
                trC.entry_short_delta_2 = _delta_at_strike(delta_tbl, c_call_short, "CALL")
            trC = finish(trC, structure, legs, credit, c_put_short, c_call_short)
        results.append(trC)

    return results


# --------------------------------------------------------------------------- #
# Full-history run — crash-resilient + resumable.
# --------------------------------------------------------------------------- #
def run_history(
    days: list[_dt.date] | None = None,
    verbose: bool = True,
    save: bool = True,
    resume: bool = True,
) -> pd.DataFrame:
    """Run all arms/structures over every available 0DTE day, checkpointing per-day."""
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
    fieldnames = list(asdict(ArmTrade(day=days[0], structure="x", arm="x")).keys())
    write_header = not _PARTIAL_CSV.is_file()
    import csv
    with open(_PARTIAL_CSV, "a", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        if write_header:
            writer.writeheader()
        for i, d in enumerate(days, 1):
            if str(d) in done_days:
                continue
            try:
                dd = s5.load_day(d)
            except Exception as e:
                if verbose:
                    print(f"[{i}/{n}] {d} LOAD-SKIP {type(e).__name__}", flush=True)
                continue
            for tr in run_day(d, clf, day_data=dd):
                writer.writerow(asdict(tr))
            fh.flush()
            if verbose and (i % 25 == 0 or i == n):
                print(f"[{i}/{n}] {d} done", flush=True)

    df = pd.read_csv(_PARTIAL_CSV)
    df["traded"] = df["traded"].astype(str).str.lower().isin(["true", "1"])
    df["breached"] = df["breached"].astype(str).str.lower().isin(["true", "1"])
    if save:
        df.to_csv(OUTPUT_DIR / "s6_strike_experiment_trades.csv", index=False)
        if verbose:
            print(f"Saved {OUTPUT_DIR / 's6_strike_experiment_trades.csv'}", flush=True)
    return df


# --------------------------------------------------------------------------- #
# Metrics + verdicts.
# --------------------------------------------------------------------------- #
def _arm_stats(sub: pd.DataFrame) -> dict:
    """Metrics for one (arm, structure[, split]) cell.

    Reward-for-risk lens (Andrew's refinement): the edge, if real, is the zone earning MORE
    credit per unit of BREACH RISK. So we surface avg credit collected AND breach rate AND
    net P&L together. Breach rate remains the primary strike-quality metric.
    """
    traded = sub[sub["traded"]].copy()
    n = len(traded)
    n_skip = int((~sub["traded"]).sum())
    if n == 0:
        return {"trades": 0, "skipped": n_skip, "avg_credit_$": float("nan"),
                "breach_rate": float("nan"), "win_rate": float("nan"), "total_pnl_$": 0.0}
    breach_rate = float(traded["breached"].mean())
    avg_credit = float(traded["entry_credit"].mean())
    wins = traded[traded["pnl_dollars"] > 0]
    losses = traded[traded["pnl_dollars"] <= 0]
    avg_win = float(wins["pnl_dollars"].mean()) if len(wins) else 0.0
    avg_loss = float(losses["pnl_dollars"].mean()) if len(losses) else 0.0
    daily = traded.sort_values("day").groupby("day")["pnl_dollars"].sum()
    losing = (daily < 0).astype(int)
    max_streak = cur = 0
    for v in losing:
        cur = cur + 1 if v else 0
        max_streak = max(max_streak, cur)
    # reward-for-risk: dollars of credit collected per point of breach probability.
    rfr = round(avg_credit / breach_rate, 3) if breach_rate > 0 else float("inf")
    return {
        "trades": n,
        "skipped": n_skip,
        "avg_credit_$": round(avg_credit, 3),
        "breach_rate": round(breach_rate, 4),
        "credit_per_breach": rfr,          # avg credit / breach rate (reward-for-risk)
        "win_rate": round(len(wins) / n, 4),
        "avg_win_$": round(avg_win, 2),
        "avg_loss_$": round(avg_loss, 2),
        "loss_over_win": round(abs(avg_loss) / avg_win, 3) if avg_win > 0 else float("nan"),
        "total_pnl_$": round(float(traded["pnl_dollars"].sum()), 2),
        "worst_day_$": round(float(daily.min()), 2),
        "max_consec_losing_days": int(max_streak),
    }


def build_results_table(df: pd.DataFrame) -> pd.DataFrame:
    """Full arm x structure x split table (overall + train/test + day-type buckets)."""
    rows = []
    for structure in STRUCTURES:
        for arm in ARMS:
            base = df[(df["structure"] == structure) & (df["arm"] == arm)]
            # overall
            rows.append({"structure": structure, "arm": arm, "split": "all",
                         **_arm_stats(base)})
            # time halves
            for half in ("train", "test"):
                rows.append({"structure": structure, "arm": arm, "split": half,
                             **_arm_stats(base[base["half"] == half])})
            # day-type buckets (gamma x vix)
            for g in ("positive", "negative"):
                for v in ("contango", "backwardation"):
                    cell = base[(base["day_type_gamma"] == g) & (base["day_type_vix"] == v)]
                    rows.append({"structure": structure, "arm": arm,
                                 "split": f"{g[:3]}/{v[:4]}", **_arm_stats(cell)})
            # credit-received buckets (REPORTED dimension, never a silent filter)
            for cb in ("<0.30", "0.30-0.50", "0.50+"):
                cell = base[base["credit_bucket"] == cb]
                rows.append({"structure": structure, "arm": arm,
                             "split": f"cr:{cb}", **_arm_stats(cell)})
            # the documented $0.30-floored subset (for comparison to the stated rule only)
            rows.append({"structure": structure, "arm": arm, "split": "cr>=0.30",
                         **_arm_stats(base[base["meets_min_credit"]])})
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- #
# DELTA-STRATIFIED POOLED fooling test — the version WITH power (see module docstring).
# --------------------------------------------------------------------------- #
DELTA_BINS = np.round(np.arange(0.0, 0.55, 0.05), 2)  # 0.00,0.05,...,0.50 (fixed, not tuned)


def delta_stratified_fooling(df: pd.DataFrame) -> pd.DataFrame:
    """Compare ZONE (B) vs BLIND (A) trades WITHIN matched short-strike |delta| bins.

    For each structure and each 0.05-wide |delta| bin, pool the B trades and the A trades that
    fell in that bin and report, side by side, avg credit / breach rate / reward-for-risk /
    P&L. A real zone edge = at MATCHED delta, B breaches less and/or earns more credit than the
    blind pool. If they are indistinguishable, the zone is cosmetic. Fixed bins, no tuning.
    """
    rows = []
    traded = df[df["traded"]].copy()
    traded["abs_delta"] = traded["entry_short_delta"].abs()
    traded["dbin"] = pd.cut(traded["abs_delta"], bins=DELTA_BINS, right=False,
                            labels=[f"{DELTA_BINS[i]:.2f}-{DELTA_BINS[i+1]:.2f}"
                                    for i in range(len(DELTA_BINS) - 1)])
    for structure in STRUCTURES:
        for dbin in traded["dbin"].cat.categories:
            cell = traded[(traded["structure"] == structure) & (traded["dbin"] == dbin)]
            B = cell[cell["arm"] == "B_zone"]
            A = cell[cell["arm"] == "A_blind015"]
            if len(B) == 0 and len(A) == 0:
                continue
            def stat(g):
                if len(g) == 0:
                    return dict(n=0, credit=float("nan"), breach=float("nan"),
                                rfr=float("nan"), pnl=0.0)
                br = float(g["breached"].mean())
                cr = float(g["entry_credit"].mean())
                return dict(n=len(g), credit=round(cr, 3), breach=round(br, 4),
                            rfr=round(cr / br, 3) if br > 0 else float("inf"),
                            pnl=round(float(g["pnl_dollars"].sum()), 2))
            sb, sa = stat(B), stat(A)
            rows.append({
                "structure": structure, "delta_bin": dbin,
                "B_n": sb["n"], "A_n": sa["n"],
                "B_credit": sb["credit"], "A_credit": sa["credit"],
                "B_breach": sb["breach"], "A_breach": sa["breach"],
                "B_rfr": sb["rfr"], "A_rfr": sa["rfr"],
                "B_pnl": sb["pnl"], "A_pnl": sa["pnl"],
            })
    return pd.DataFrame(rows)


def _cmp(a: dict, b: dict) -> str:
    """Human comparison of credit + breach rate + P&L between two arm-stat dicts (reward-for-risk)."""
    parts = []
    cr_a, cr_b = a.get("avg_credit_$"), b.get("avg_credit_$")
    if cr_a is not None and cr_b is not None and np.isfinite(cr_a) and np.isfinite(cr_b):
        parts.append(f"credit ${cr_a:.3f} vs ${cr_b:.3f} (Δ${cr_a - cr_b:+.3f})")
    br_a, br_b = a.get("breach_rate"), b.get("breach_rate")
    if br_a is not None and br_b is not None and np.isfinite(br_a) and np.isfinite(br_b):
        parts.append(f"breach {br_a:.3f} vs {br_b:.3f} (Δ{br_a - br_b:+.3f})")
    pnl_a, pnl_b = a.get("total_pnl_$", 0), b.get("total_pnl_$", 0)
    parts.append(f"P&L ${pnl_a} vs ${pnl_b} (Δ${pnl_a - pnl_b:+.2f})")
    return "; ".join(parts)


def verdicts(df: pd.DataFrame) -> str:
    """The two decisive verdicts: B-vs-A and B-vs-C (fooling-test), with robustness call.

    B beats A / C on strike quality iff LOWER breach rate. We demand the advantage be a
    PLATEAU (holds in BOTH time-halves and n not thin) not an isolated PEAK before calling it
    real. We do NOT recommend adoption — that is Andrew's call under the frozen-config rule.
    """
    THIN = 30
    lines = []
    for structure in STRUCTURES:
        base = df[df["structure"] == structure]

        def stat(arm, mask=None):
            sub = base[base["arm"] == arm]
            if mask is not None:
                sub = sub[mask(sub)]
            return _arm_stats(sub)

        A = stat("A_blind015")
        B = stat("B_zone")
        C = stat("C_deltamatched")
        A_tr = stat("A_blind015", lambda s: s["half"] == "train")
        A_te = stat("A_blind015", lambda s: s["half"] == "test")
        B_tr = stat("B_zone", lambda s: s["half"] == "train")
        B_te = stat("B_zone", lambda s: s["half"] == "test")
        C_tr = stat("C_deltamatched", lambda s: s["half"] == "train")
        C_te = stat("C_deltamatched", lambda s: s["half"] == "test")

        lines.append(f"\n===== {structure} =====")
        lines.append(f"  n: A={A['trades']} B={B['trades']} C={C['trades']} "
                     f"(B skips={B['skipped']})")

        # --- Verdict 1: B vs A ---
        def better_breach(x, y, xt, yt):
            """x beats y on breach if lower overall AND lower in both halves AND n>=THIN."""
            if x["trades"] < THIN or y["trades"] < THIN:
                return False, "thin-n"
            bx, by = x["breach_rate"], y["breach_rate"]
            if not (np.isfinite(bx) and np.isfinite(by)):
                return False, "no breach data"
            overall = bx < by
            both = (xt["trades"] >= 10 and yt["trades"] >= 10
                    and np.isfinite(xt["breach_rate"]) and np.isfinite(yt["breach_rate"]))
            plateau = overall and (xt["breach_rate"] < yt["breach_rate"])
            return (overall and plateau), ("PLATEAU" if plateau else
                                           ("PEAK(overall-only)" if overall else "no"))

        bvsa_break_tr, bvsa_reason_tr = better_breach(B_tr, A_tr, B_tr, A_tr)
        bvsa, _ = better_breach(B, A, B_tr, A_tr)
        # robustness: also require test half
        bvsa_test = (np.isfinite(B_te["breach_rate"]) and np.isfinite(A_te["breach_rate"])
                     and B_te["breach_rate"] < A_te["breach_rate"])
        robust_BA = bvsa and (B_tr["breach_rate"] < A_tr["breach_rate"]) and bvsa_test \
            if np.isfinite(B_tr["breach_rate"]) and np.isfinite(A_tr["breach_rate"]) else False
        lines.append(f"  [B vs A] {_cmp(B, A)}")
        lines.append(f"           train: {_cmp(B_tr, A_tr)}")
        lines.append(f"           test:  {_cmp(B_te, A_te)}")
        if B["trades"] < THIN:
            lines.append("           VERDICT: THIN — cannot conclude (zone arm too few trades).")
        elif robust_BA:
            lines.append("           VERDICT: B beats A on breach in BOTH halves = PLATEAU "
                         "(zone placement improves strike quality vs arbitrary 0.15).")
        elif np.isfinite(B["breach_rate"]) and np.isfinite(A["breach_rate"]) \
                and B["breach_rate"] < A["breach_rate"]:
            lines.append("           VERDICT: B lower breach overall but NOT in both halves = "
                         "PEAK — not robust; reject.")
        else:
            lines.append("           VERDICT: B does NOT beat A on breach — zone placement is "
                         "no better than arbitrary 0.15.")

        # --- Note on per-day arm C: degenerate by construction (strike==B on the grid). ---
        same_strike = int((base[base["arm"] == "C_deltamatched"].reset_index(drop=True)
                           ["short_strike"].round(2).eq(
                               base[base["arm"] == "B_zone"].reset_index(drop=True)
                               ["short_strike"].round(2))).sum()) if len(B) == len(C) else -1
        lines.append(f"  [per-day arm C] DEGENERATE — same-day delta-match reproduces B's "
                     f"strike (bijection on the 5-pt grid). Use the delta-stratified pooled "
                     f"test below for the real fooling-guard.")

        # --- Verdict 2: B vs BLIND @ MATCHED DELTA (delta-stratified pooled fooling test). ---
        strat = delta_stratified_fooling(df)
        s = strat[strat["structure"] == structure]
        # Only bins where BOTH arms have enough trades to compare (n>=15 each).
        cmp_bins = s[(s["B_n"] >= 15) & (s["A_n"] >= 15)]
        lines.append("  [B vs BLIND @ matched delta — FOOLING TEST, pooled by |delta| bin]")
        if cmp_bins.empty:
            lines.append("           no delta bin has both zone and blind n>=15 — cannot "
                         "conclude the fooling test for this structure (thin overlap).")
        else:
            wins_breach = 0
            wins_rfr = 0
            tot = 0
            for _, r in cmp_bins.iterrows():
                tot += 1
                lines.append(f"           |d| {r['delta_bin']}: "
                             f"B(n={r['B_n']}) credit ${r['B_credit']} breach {r['B_breach']} "
                             f"rfr {r['B_rfr']} | "
                             f"BLIND(n={r['A_n']}) credit ${r['A_credit']} breach {r['A_breach']} "
                             f"rfr {r['A_rfr']}")
                if np.isfinite(r["B_breach"]) and np.isfinite(r["A_breach"]) \
                        and r["B_breach"] < r["A_breach"]:
                    wins_breach += 1
                if np.isfinite(r["B_rfr"]) and np.isfinite(r["A_rfr"]) \
                        and r["B_rfr"] > r["A_rfr"]:
                    wins_rfr += 1
            lines.append(f"           SUMMARY: in {wins_breach}/{tot} matched-delta bins the "
                         f"zone breaches LESS; in {wins_rfr}/{tot} bins the zone has BETTER "
                         f"reward-for-risk than blind at the same delta.")
            if wins_breach >= tot - (tot // 4) and wins_rfr >= tot - (tot // 4):
                lines.append("           VERDICT: zone adds edge BEYOND its delta (beats blind "
                             "at matched delta in most bins) — NOT cosmetic.")
            elif wins_breach <= tot // 4 and wins_rfr <= tot // 4:
                lines.append("           VERDICT: zone is COSMETIC — at matched delta it does "
                             "NOT beat blind placement (no edge beyond the delta it implies).")
            else:
                lines.append("           VERDICT: MIXED/ambiguous — no consistent matched-delta "
                             "edge; treat as no robust edge (reject, per curve-fit caution).")
    return "\n".join(lines)


def run(verbose: bool = True, save: bool = True) -> dict:
    """Full pipeline: run history -> results table -> verdicts."""
    df = run_history(verbose=verbose, save=save)
    table = build_results_table(df)
    strat = delta_stratified_fooling(df)
    if save:
        table.to_csv(OUTPUT_DIR / "s6_strike_experiment_results.csv", index=False)
        strat.to_csv(OUTPUT_DIR / "s6_strike_experiment_delta_stratified.csv", index=False)
    if verbose:
        with pd.option_context("display.width", 240, "display.max_columns", 40,
                               "display.max_rows", 500):
            print("\n=== S6 STRIKE EXPERIMENT — arm x structure x split ===", flush=True)
            print(table.to_string(index=False), flush=True)
            print("\n=== DELTA-STRATIFIED FOOLING TEST (B zone vs A blind, matched |delta|) ===",
                  flush=True)
            print(strat.to_string(index=False), flush=True)
        print("\n=== VERDICTS ===", flush=True)
        print(verdicts(df), flush=True)
    return {"trades": df, "table": table, "delta_stratified": strat}


if __name__ == "__main__":
    run()
