"""custom_tier.py — the whole-share SIZE TIER for the CUSTOM (Andrew-authored) model family.

WHY THIS EXISTS (and why it is NOT small_tier)
----------------------------------------------
Andrew authors his own model portfolios in the CRM. Because the desk can only place WHOLE
SHARES over the TWS socket API, one hand-authored book cannot serve every account size, so
the family ships in THREE sizes and an account is moved between them by its NAV:

  * full     — the 15-line book, ``"<Risk> (Custom)"``
  * small    — the 11-line book, ``"<Risk> (Small, Custom)"``
  * starter  — the 2-line book, ``"Starter (Custom)"`` (SCHB 80 / USFR 20)

The starter rung is the measured answer to a real defect, not a preference: at $2,000 the
11-line small Growth book deploys only 76.2% of the account, because its 3% long-Treasury
and gold-bullion slices are worth less than one share and those orders are therefore never
created at all. The 2-line starter book deploys 95.9% at the same size.

THIS MODULE DELIBERATELY DOES NOT REUSE ``strategies/small_tier.py``. It does not import it,
and it never calls its parent-version / is-small / collapse helpers. Those helpers are
SPELLING-based — the parent-version one turns "Growth (Small)" into "Growth" by string
surgery — so feeding them a hand-authored label is one CRM rename away from REWRITING that
account onto an S0 model and silently discarding Andrew's whole book. Everything here
dispatches on an EXPLICIT, CLOSED label set (:data:`FULL_LABELS`, :data:`SMALL_LABELS`,
:data:`STARTER_LABEL`); a label that is not literally in that set is NOT in this family and
:func:`tier_for` refuses to touch it. Every value this module can ever return comes out of
that same table, so a custom label can never be rewritten to an S0 model.

PURE. Reads no data, contacts no broker, decides no regime, and — load-bearing — writes
NOTHING to the CRM. The CRM->desk seam is one-way read-only.

THE RULE (mirrored by the CRM's own SQL re-tiering job — the two MUST agree exactly, or an
account oscillates between models every day). Fully specified 2026-08-25.
-------------------------------------------------------------------------------------
1. CURRENT RUNG, from the account's current custom label: starter / small / full.

2. NATURAL RUNG, from value alone, on the PLAIN boundaries:
       value <  5,000                  -> starter
       5,000 <= value < 25,000         -> small
       value >= 25,000                 -> full

3. FIRST ASSIGNMENT (the account has never held any other custom model — see
   :data:`HAS_PRIOR_FIELD`): target = the NATURAL rung, no band. There is no incumbent to be
   sticky about. Existing house behaviour.

4. INCUMBENT:
   a. natural == current                     -> STAY.
   b. natural ADJACENT to current            -> apply THAT boundary's band; move only if it
                                                clears, otherwise STAY:
          starter -> small   when value >= 5,500
          small   -> starter when value <  4,500
          small   -> full    when value >= 27,500
          full    -> small   when value <  22,500
   c. natural TWO rungs away (starter<->full) -> MOVE DIRECTLY, NO BAND.

$22,500 / $27,500 around $25,000 is the existing house band (``small_tier._bounds`` and the
CRM's ``run_model_tier_scan`` both use it). $4,500 / $5,500 is the same +/-10% shape around
$5,000.

WHY 4(c) IS DIRECT AND NOT A LADDER (Andrew's call, 2026-08-25). An earlier reading walked
one rung per evaluation. It is wrong for the client: a Starter account funded to $30,000
would trade into the 11-line small book tonight and the 15-line full book tomorrow — the
spread paid TWICE for an intermediate book nobody chose. A band exists to damp oscillation
around a boundary, and a $30,000 account is nowhere near the $5,000 boundary, so there is no
oscillation to guard against and the band would only force a pointless trade.

RISK LEVEL (Andrew's decision, 2026-08-25). small <-> full PRESERVES the risk level, because
both labels carry it. ``Starter (Custom)`` is ONE book shared by all three risk levels and
its label carries none, so LEAVING starter — to small OR, in the two-rung case, straight to
full — must RECOVER the risk level from the account's assignment history, which the CRM owns
(:data:`PRIOR_RISK_FIELD`). When no prior non-Starter custom model exists, the answer is
GROWTH — Andrew: "Being a small account balance I don't know why any account would be less
than growth moving from $5000 up." That is a decision, not a guess: never flagged, never
skipped.

THE TWO CRM-SUPPLIED FACTS. Both arrive as PARAMETERS; this module stays pure and the roster
read stays in the caller.
  * ``has_prior_assignment`` (:data:`HAS_PRIOR_FIELD`) decides rule 3 vs rule 4. It replaced
    a "current_label is None" test, which was a REAL DIVERGENCE from the CRM: a freshly
    assigned $4,900 account resolved to ``Starter (Custom)`` in the CRM but to
    ``Growth (Small, Custom)`` on the desk, because the desk saw a label and therefore
    treated a brand-new assignment as a sticky incumbent.
  * ``prior_risk`` (:data:`PRIOR_RISK_FIELD`) supplies the risk level when leaving starter.

MISSING INPUTS FAIL TOWARD THE INCUMBENT. If ``has_prior_assignment`` is None (the view lags
the code), an account that HAS a current label is treated as an incumbent and the band
applies. Re-tiering a live account off a plain boundary is the more damaging error, so the
uncertain case takes the stickier branch.
"""
from __future__ import annotations

from typing import Optional

# --- the closed label set -------------------------------------------------------
# Explicit and enumerated ON PURPOSE. Never derive membership from a suffix: "Starter
# (Custom)" also ends in " (Custom)" but carries no risk level, and a renamed S0 label must
# never test positive here.
RISK_LEVELS: tuple[str, ...] = ("Conservative", "Balanced", "Growth")

STARTER_LABEL = "Starter (Custom)"                     # the 2-line book, risk-level-free
FULL_LABELS: dict[str, str] = {f"{r} (Custom)": r for r in RISK_LEVELS}
SMALL_LABELS: dict[str, str] = {f"{r} (Small, Custom)": r for r in RISK_LEVELS}

TIER_STARTER = "starter"
TIER_SMALL = "small"
TIER_FULL = "full"

# Andrew's default when the shared Starter book has no risk level to recover (2026-08-25).
DEFAULT_RISK = "Growth"

# --- the boundaries -------------------------------------------------------------
# starter <-> small, the +/-10% band around $5,000.
STARTER_THRESHOLD = 5_000.0    # the decision boundary for a NEW/unassigned account
STARTER_PROMOTE_AT = 5_500.0   # starter -> small only at/above this
STARTER_DEMOTE_AT = 4_500.0    # small -> starter only below this

# small <-> full, the existing house band around $25,000 (same numbers as small_tier's).
FULL_THRESHOLD = 25_000.0      # the decision boundary for a NEW/unassigned account
FULL_PROMOTE_AT = 27_500.0     # small -> full only at/above this
FULL_DEMOTE_AT = 22_500.0      # full -> small only below this

# The two CRM roster fields this rule needs, both computed CRM-side by the same job that runs
# the SQL half of this ladder, and both READ-ONLY. The desk never writes assignment history.
#
# has_prior_custom_assignment (boolean NOT NULL): true when the account has ANY custom-family
#   assignment row OTHER than its current open one. FALSE means "first assignment" -> the
#   plain boundaries of rule 3. Absent -> fail toward the incumbent (rule 4).
# prior_custom_risk_level (text NULL): the risk word from the most recent NON-Starter custom
#   model the account has held; NULL when none, and the desk then applies DEFAULT_RISK.
HAS_PRIOR_FIELD = "has_prior_custom_assignment"
PRIOR_RISK_FIELD = "prior_custom_risk_level"

# The rungs, low to high. Adjacency and the two-rung case are computed from these positions,
# never from the label's spelling.
_RUNG_ORDER: tuple[str, ...] = (TIER_STARTER, TIER_SMALL, TIER_FULL)


# --- label <-> (tier, risk) -----------------------------------------------------
def tier_of(label: Optional[str]) -> Optional[str]:
    """Which rung this label is, or ``None`` if the label is NOT in the custom family.

    ``None`` is the whole safety story: an S0 label ("Growth", "Growth (Small)"), a renamed
    custom book, or anything unrecognised answers ``None`` and :func:`tier_for` then refuses
    to touch it."""
    if label is None:
        return None
    label = str(label).strip()
    if label == STARTER_LABEL:
        return TIER_STARTER
    if label in SMALL_LABELS:
        return TIER_SMALL
    if label in FULL_LABELS:
        return TIER_FULL
    return None


def risk_of(label: Optional[str]) -> Optional[str]:
    """The risk level a custom label CARRIES, or ``None``.

    ``Starter (Custom)`` answers ``None`` — that is the point: the starter book is shared by
    all three risk levels, so the label cannot tell you which one the account came from."""
    if label is None:
        return None
    label = str(label).strip()
    return FULL_LABELS.get(label) or SMALL_LABELS.get(label)


def is_custom_family(label: Optional[str]) -> bool:
    """True iff `label` is one of the seven custom-family labels, matched EXACTLY."""
    return tier_of(label) is not None


def label_for(tier: str, risk: Optional[str] = None) -> str:
    """The label for a rung. The ONLY place a label is ever produced, so every output of this
    module is provably inside the custom family."""
    if tier == TIER_STARTER:
        return STARTER_LABEL
    risk = _normalise_risk(risk)
    if tier == TIER_SMALL:
        return f"{risk} (Small, Custom)"
    if tier == TIER_FULL:
        return f"{risk} (Custom)"
    raise ValueError(f"unknown custom tier {tier!r}")


def _normalise_risk(risk: Optional[str]) -> str:
    """A usable risk level, defaulting to GROWTH (Andrew's decision — never a flag, never a
    skip). Anything unrecognised — including the shared Starter book's absent risk level —
    becomes the default rather than propagating a bad label."""
    risk = str(risk).strip() if risk is not None else ""
    return risk if risk in RISK_LEVELS else DEFAULT_RISK


# --- the tier decision ----------------------------------------------------------
def natural_tier(value: float) -> str:
    """Rule 2 — the rung `value` alone implies, on the PLAIN boundaries. No band, no
    incumbent. This is also rule 3's answer for a first assignment."""
    value = float(value)
    if value < STARTER_THRESHOLD:
        return TIER_STARTER
    if value < FULL_THRESHOLD:
        return TIER_SMALL
    return TIER_FULL


def _band_clears(current: str, natural: str, value: float) -> bool:
    """Rule 4(b) — for two ADJACENT rungs, has `value` cleared that boundary's band?"""
    if current == TIER_STARTER and natural == TIER_SMALL:
        return value >= STARTER_PROMOTE_AT
    if current == TIER_SMALL and natural == TIER_STARTER:
        return value < STARTER_DEMOTE_AT
    if current == TIER_SMALL and natural == TIER_FULL:
        return value >= FULL_PROMOTE_AT
    if current == TIER_FULL and natural == TIER_SMALL:
        return value < FULL_DEMOTE_AT
    raise ValueError(f"{current!r} and {natural!r} are not adjacent rungs")


def tier_for(value: float,
             current_label: Optional[str] = None,
             prior_risk: Optional[str] = None,
             has_prior_assignment: Optional[bool] = None) -> str:
    """Which custom label this account should hold at `value`. See the module docstring for
    the rule verbatim; this function IS that rule and nothing else.

    `current_label` is what it holds today (``None`` = no current custom assignment at all).
    `has_prior_assignment` is the CRM's ``has_prior_custom_assignment``: ``False`` means this
    is the account's FIRST custom assignment and the plain boundaries apply; ``None`` means
    the CRM did not tell us, and an account holding a label is then treated as an incumbent
    (the stickier, less damaging branch). `prior_risk` is the CRM's
    ``prior_custom_risk_level`` — the risk level to recover when LEAVING the shared Starter
    book, which is the only case where the incumbent label cannot supply one.

    Raises ``ValueError`` for a `current_label` outside the custom family. That is deliberate
    and is the caller's contract: check :func:`is_custom_family` first and pass an S0 (or
    renamed) label through UNCHANGED. This function must never be the thing that decides an
    S0 account's model."""
    value = float(value)
    current = tier_of(current_label)
    if current_label is not None and current is None:
        raise ValueError(
            f"{current_label!r} is not a custom-family model label — custom_tier must never "
            f"re-tier it. Check is_custom_family() and pass it through unchanged.")

    # RISK, resolved once and uniformly: the incumbent label wherever it carries one
    # (small <-> full preserves it), otherwise the CRM's history, otherwise GROWTH. Starter
    # and an absent label both answer None from risk_of, which is exactly the "recover it"
    # case — including the two-rung starter -> full jump.
    risk = risk_of(current_label) or _normalise_risk(prior_risk)

    natural = natural_tier(value)

    # RULE 3 — FIRST ASSIGNMENT. Either there is no current custom assignment at all, or the
    # CRM says the one it holds is its first ever. No incumbent to be sticky about, so the
    # natural rung wins outright. Testing has_prior_assignment (not "is the label None") is
    # what keeps the desk and the CRM in step: a freshly assigned $4,900 account is a
    # Starter account in BOTH, not a sticky Small one here and a Starter one there.
    if current is None or has_prior_assignment is False:
        return label_for(natural, risk)

    # RULE 4 — INCUMBENT.
    if natural == current:                                   # (a) stay
        return label_for(current, risk)
    if abs(_RUNG_ORDER.index(natural) - _RUNG_ORDER.index(current)) >= 2:
        return label_for(natural, risk)                      # (c) two rungs -> direct, no band
    if _band_clears(current, natural, value):                # (b) adjacent -> band
        return label_for(natural, risk)
    return label_for(current, risk)


def prior_risk_from_row(row) -> Optional[str]:
    """The prior risk level a CRM roster row reports (``prior_custom_risk_level``), or
    ``None``.

    Read DEFENSIVELY via ``.get``: a missing key or a NULL must not raise, so the desk keeps
    running if the view lags the code. ``None`` here means "no history" and the caller's
    :func:`tier_for` then applies the GROWTH default, which is Andrew's stated decision.
    READ-ONLY — the desk never writes assignment history back."""
    try:
        value = row.get(PRIOR_RISK_FIELD)
    except AttributeError:
        return None
    if value is None:
        return None
    value = str(value).strip()
    return value if value in RISK_LEVELS else None


def has_prior_assignment_from_row(row) -> Optional[bool]:
    """The CRM's ``has_prior_custom_assignment`` for a roster row, or ``None`` if absent.

    ``None`` is a MEANINGFUL third state, not a synonym for False: it says the CRM did not
    tell us, and :func:`tier_for` then treats an account holding a label as an INCUMBENT and
    applies the band. Failing that way round is deliberate — silently re-tiering a live
    account off a plain boundary is the more damaging error. Read defensively so a lagging
    view degrades instead of raising."""
    try:
        value = row.get(HAS_PRIOR_FIELD)
    except AttributeError:
        return None
    if value is None:
        return None
    return bool(value)
