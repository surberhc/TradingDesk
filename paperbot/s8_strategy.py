"""
s8_strategy.py — S8 (British IC + B2 long-leg auto-close) pure strategy logic.

Stage 2 of a 5-stage build (see docs/S8_SPEC.md and paperbot/s8_config.py for stage 1).
PURE FUNCTIONS ONLY. NO IBKR IMPORTS ANYWHERE IN THIS FILE — this module never connects,
never transmits, never touches a broker. It answers exactly two questions, both offline:

  1. stop_price(entry_credit, stop_multiple) -> float
     The frozen stop formula, S8_SPEC.md Sec 2.3, verified exact on 4,610 real rows:
         PriceStopTarget = floor(10 * (PriceOpen + StopMultiple)) / 10

  2. pick_spread_by_credit(chain_snapshot, template_name, template_config) -> SpreadPick | None
     Given one minute's option-chain snapshot, search short-strike/width combinations to
     find the pair whose HONEST net credit (sell short at bid, buy long at ask — same
     convention as backtester/s5_harvest_engine.py's _build_condor) lands closest to the
     template's target_credit (S8_SPEC.md Sec 2.2: "Strike selection is credit-driven, not
     delta-driven or fixed-point ... the algorithm selects short strike + width to hit the
     target credit"). Realized short-leg delta is reported ONLY as a diagnostic (the real
     strategy's own selection dial is credit, not delta — Sec 2.2 is explicit that delta is
     an EMERGENT, roughly-constant ~0.22-0.29 property of the credit-driven selection, not
     the input), computed via backtester/s6_recon.py's existing, already-validated
     put-call-parity spot recovery + Black-Scholes delta lookup — reused, not reinvented,
     exactly per the build plan (calm-riding-hammock.md item 3).

WHAT THIS FUNCTION CAN AND CANNOT COMPUTE FROM A BARE SNAPSHOT (stated plainly):
  `pick_spread_by_credit`'s signature takes a bare one-minute chain_snapshot (columns
  strike, right, bid, ask — no timestamp, no expiration, no spot). Credit/width/strike
  selection needs none of that — bid/ask alone fully determine it. But the DIAGNOSTIC
  delta needs a time-to-expiry (S6's Black-Scholes) which needs `minute` + `expiration`,
  and a spot (recovered from the snapshot's own put-call parity, or supplied directly).
  Those are OPTIONAL keyword args here precisely because the live runner (s8_chain.py,
  a later stage) will get delta directly from IBKR's own greeks feed and won't need this
  recon path at all — this path exists so the selector is unit-testable against the
  read-only historical warehouse (s5_intraday_data.py) before any live wiring exists. If
  `minute`/`expiration` are omitted, the diagnostic delta is honestly left uncomputed
  (short_delta=None) with a stated reason, rather than silently guessing or defaulting to
  0 — see SpreadPick.delta_note.

TOLERANCE CHOICE (stated plainly, not hidden): the search accepts the best-matching combo
only if its realized credit is within `tolerance` of target_credit, default
max($0.50, 20% of target_credit). Real strikes sit on a discrete grid (S8_SPEC.md Sec 2.2:
observed widths range 5-85 points), so the achievable credit surface is a step function,
not continuous — a real fill can land a real, non-curve-fit distance from the label
(Sec 2.2's own measured medians span $2.05-2.15 / $3.00-3.08 / $3.95-4.20 for the $2/$3/$4
labels respectively, i.e. real fills already deviate from the label by ~$0.05-0.20 even
in aggregate; a single day's discrete-strike best match can reasonably deviate more than
that). A flat $0.50 floor comfortably covers that observed label-vs-fill drift; the 20%
scaling keeps the tolerance sane as target_credit itself changes ($2 vs $4). This is a
SEARCH tolerance (what counts as "no viable combo"), not a curve-fit parameter — it does
not change which combo is picked, only whether a bad day returns None instead of a wild
outlier.
"""
from __future__ import annotations

import math
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np

# --------------------------------------------------------------------------- #
# Reuse backtester/s6_recon.py's already-validated put-call-parity spot recovery
# + Black-Scholes delta, for the DIAGNOSTIC delta only (see module docstring). This
# mirrors paperbot/strategy_target.py's existing precedent for reaching into the
# backtester folder: path derived relative to this file, not the current directory,
# and s6_recon.py itself has NO IBKR imports (pure numpy/pandas), so this does not
# violate the "no IBKR imports in this file" rule.
# --------------------------------------------------------------------------- #
_BACKTESTER = Path(__file__).resolve().parent.parent / "backtester"
if str(_BACKTESTER) not in sys.path:
    sys.path.insert(0, str(_BACKTESTER))

import s6_recon as recon  # noqa: E402  (after sys.path setup)

# The template config's "side" string (s8_config.TEMPLATES) -> chain snapshot's "right".
_SIDE_TO_RIGHT = {"Puts": "PUT", "Calls": "CALL"}

# Real observed short-leg delta band (S8_SPEC.md Sec 2.2), reported as a diagnostic flag
# only — NOT a selection input, NOT a veto. A pick outside this band is not rejected;
# it is just flagged in SpreadPick.delta_note as a live sanity signal for a human/monitor.
_REAL_DELTA_BAND = (0.20, 0.29)

_EPS = 1e-9  # float-representation guard for the floor(); see stop_price().


# --------------------------------------------------------------------------- #
# 1. Stop formula (S8_SPEC.md Sec 2.3, exact, verified 99.98% match on 4,610 real rows)
# --------------------------------------------------------------------------- #
def stop_price(entry_credit: float, stop_multiple: float) -> float:
    """PriceStopTarget = floor(10 * (entry_credit + stop_multiple)) / 10.

    A constant-DOLLAR stop (not constant-percentage): the spread is stopped when its
    mark-to-market cost to close rises to entry_credit + stop_multiple, rounded DOWN to
    the nearest $0.10 tick. `stop_multiple` comes from s8_config.TEMPLATES — frozen,
    never computed or tuned here.

    The `+ _EPS` before flooring guards only against float-representation noise (e.g.
    10*(1.1+2.2) landing at 32.999999999998 instead of 33.0 for certain inputs) — it
    does not change any real value on the $0.10 grid this formula operates on.
    """
    raw = 10.0 * (float(entry_credit) + float(stop_multiple))
    return math.floor(raw + _EPS) / 10.0


# --------------------------------------------------------------------------- #
# 2. Credit-target strike/width selection (S8_SPEC.md Sec 2.2)
# --------------------------------------------------------------------------- #
@dataclass
class SpreadPick:
    """One credit-spread candidate selected to hit a template's target credit.

    short_delta/delta_note: DIAGNOSTIC ONLY (see module docstring) — never the selection
    dial. short_delta is None (with delta_note explaining why) whenever this function
    was not given enough time/spot context to compute it from a bare snapshot.
    """

    template_name: str
    side: str                  # "PUT" or "CALL" (chain-snapshot convention)
    short_strike: float
    long_strike: float
    width: float                # abs(short_strike - long_strike), points
    realized_credit: float       # short_bid - long_ask (honest fill, points)
    target_credit: float
    short_delta: float | None    # abs(BS delta) of the short leg; None if uncomputed
    delta_note: str              # human-readable status of the diagnostic delta


def _usable_quotes(chain_snapshot, right: str) -> dict[float, tuple[float, float]]:
    """{strike -> (bid, ask)} for one option right, valid two-sided quotes only.

    A valid quote requires ask > 0 and 0 <= bid <= ask (same NBBO-sanity bar as
    s6_recon._mid / s5_harvest_engine._leg_quote) and both finite.
    """
    snap = chain_snapshot[chain_snapshot["right"] == right]
    out: dict[float, tuple[float, float]] = {}
    for row in snap.itertuples(index=False):
        bid, ask = float(row.bid), float(row.ask)
        if not (np.isfinite(bid) and np.isfinite(ask)):
            continue
        if ask <= 0 or bid < 0 or bid > ask:
            continue
        out[float(row.strike)] = (bid, ask)
    return out


def pick_spread_by_credit(
    chain_snapshot,
    template_name: str,
    template_config: dict,
    *,
    minute=None,
    expiration=None,
    spot: float | None = None,
    min_width: float = 5.0,
    max_width: float = 85.0,
    tolerance: float | None = None,
) -> SpreadPick | None:
    """Search short/long strike combinations for the pair whose honest net credit is
    closest to `template_config["target_credit"]`.

    chain_snapshot : one minute's full chain snap (BOTH rights), columns
                      strike, right, bid, ask — e.g. s5_intraday_data's
                      `_snap_at(nbbo, minute)` output (see s5_harvest_engine.py).
    template_name   : e.g. "Puts-80-$4" (for the returned pick's provenance only).
    template_config : one value from s8_config.TEMPLATES (side, target_credit, ...).
    minute/expiration/spot : OPTIONAL context enabling the diagnostic delta lookup via
                      s6_recon (see module docstring). expiration is the 0DTE
                      expiration date (== the trade date for SPXW 0DTE).

    Search: width ranges over [min_width, max_width] points (S8_SPEC.md Sec 2.2:
    real observed widths span 5-85 — NOT a single hardcoded width), computed directly
    as the distance between two REAL quoted strikes (not assumed to be a uniform 5pt
    grid), so irregular strike spacing is handled correctly. For puts, the long strike
    protects BELOW the short (long_k < short_k); for calls, ABOVE (long_k > short_k) —
    the same convention as s5_harvest_engine.py's _build_condor. Credit = short_bid -
    long_ask (sell short at bid, buy long at ask — honest fill, never mid).

    OTM CONSTRAINT (only applied when a spot is derivable — see below): a real 0DTE
    short premium leg is always sold OTM (S8_SPEC.md Sec 2.2's ~0.22-0.29 delta band
    is itself only meaningful for an OTM leg). Without ANY spot reference, multiple
    strike/width combos can tie on realized credit — including economically-wrong ITM
    combos whose vertical value coincidentally nets out near target_credit — with no
    principled way to break the tie from bid/ask alone. So: whenever `spot` is passed,
    OR `minute`+`expiration` are passed (letting this function recover spot itself via
    s6_recon's existing put-call-parity method, reusing the SAME snapshot), the search
    is restricted to strikes OTM of that spot (puts: strike < spot; calls: strike >
    spot) before ranking by credit distance. This is a structural, economically-motivated
    constraint (not a curve-fit parameter, and NOT width_label-based — s8_config.py is
    explicit that width_label must never be treated as a literal width target). If
    neither spot nor minute/expiration is given, NO OTM filter is applied (this function
    then honestly has no way to know which side of the market is OTM) — the raw
    bare-snapshot credit search may occasionally pick a degenerate ITM tie; this
    limitation is intentional rather than silently guessing a spot.

    Returns None if no combination's realized credit is within `tolerance` of
    target_credit (see module docstring for the tolerance default/rationale), or if
    the requested side has no usable two-sided quotes at all in this snapshot.
    """
    side = template_config["side"]
    if side not in _SIDE_TO_RIGHT:
        raise ValueError(f"unknown template side {side!r} (expected 'Puts' or 'Calls')")
    right = _SIDE_TO_RIGHT[side]
    is_put = right == "PUT"
    target_credit = float(template_config["target_credit"])

    if tolerance is None:
        tolerance = max(0.50, 0.20 * target_credit)

    quotes = _usable_quotes(chain_snapshot, right)
    if not quotes:
        return None

    # Resolve spot ONCE up front (if derivable) so it can both (a) restrict the short
    # strike search to the OTM side and (b) feed the diagnostic delta below without a
    # redundant recon call.
    spot_val = spot
    if spot_val is None and minute is not None and expiration is not None:
        sr = recon.recover_forward_spot(chain_snapshot, minute, expiration)
        spot_val = sr.spot if sr is not None else None

    strikes = sorted(quotes.keys())
    short_candidates = strikes
    if spot_val is not None:
        short_candidates = [
            k for k in strikes if (k < spot_val if is_put else k > spot_val)
        ]
        if not short_candidates:
            return None

    best_err = float("inf")
    best: tuple[float, float, float, float] | None = None  # short_k, long_k, credit, width
    for short_k in short_candidates:
        short_bid, _short_ask = quotes[short_k]
        for long_k in strikes:
            width = (short_k - long_k) if is_put else (long_k - short_k)
            if width < min_width or width > max_width:
                continue
            _long_bid, long_ask = quotes[long_k]
            credit = short_bid - long_ask
            if credit <= 0:
                continue  # not a credit spread at this combo; cannot be a target-credit hit
            err = abs(credit - target_credit)
            if err < best_err:
                best_err = err
                best = (short_k, long_k, credit, width)

    if best is None or best_err > tolerance:
        return None

    short_k, long_k, credit, width = best

    short_delta, delta_note = _diagnostic_delta(
        chain_snapshot, right, short_k, minute, expiration, spot_val
    )

    return SpreadPick(
        template_name=template_name,
        side=right,
        short_strike=short_k,
        long_strike=long_k,
        width=width,
        realized_credit=credit,
        target_credit=target_credit,
        short_delta=short_delta,
        delta_note=delta_note,
    )


def _diagnostic_delta(
    chain_snapshot, right: str, short_strike: float, minute, expiration, spot: float | None
) -> tuple[float | None, str]:
    """Best-effort diagnostic short-leg delta via s6_recon. Honestly returns
    (None, reason) whenever the bare snapshot doesn't carry enough context (see the
    module docstring's "WHAT THIS FUNCTION CAN AND CANNOT COMPUTE" section) — never
    fabricates a delta."""
    if minute is None or expiration is None:
        return None, (
            "not computed: no minute/expiration supplied to pick_spread_by_credit "
            "(a bare chain snapshot alone has no time-to-expiry context for a "
            "Black-Scholes delta lookup; the live runner gets delta directly from "
            "IBKR's own greeks feed instead of this recon path)"
        )

    spot_val = spot
    if spot_val is None:
        sr = recon.recover_forward_spot(chain_snapshot, minute, expiration)
        if sr is None:
            return None, (
                "not computed: put-call-parity spot recovery failed for this "
                "minute/expiration (too few valid near-ATM strikes in the snapshot)"
            )
        spot_val = sr.spot

    right_snap = chain_snapshot[chain_snapshot["right"] == right]
    delta_tbl = recon.per_strike_delta(right_snap, minute, expiration, spot_val)
    row = delta_tbl[delta_tbl["strike"] == short_strike]
    if row.empty or not np.isfinite(row["delta"].iloc[0]):
        return None, "not computed: Black-Scholes delta inversion failed at this strike/quote"

    d = abs(float(row["delta"].iloc[0]))
    lo, hi = _REAL_DELTA_BAND
    if lo <= d <= hi:
        return d, f"delta {d:.3f} within known real-world band [{lo:.2f}, {hi:.2f}]"
    return d, f"FLAG: delta {d:.3f} OUTSIDE known real-world band [{lo:.2f}, {hi:.2f}]"
