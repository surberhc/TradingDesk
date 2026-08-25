"""
custom_target.py — what an ANDREW-AUTHORED ("custom") model wants to hold RIGHT NOW,
packed into the paperbot's existing Target shape.

SIBLING to strategy_target.py (S0) and s4_strategy_target.py (S4). Those two COMPUTE a
book: S0 re-runs the validated month-by-month backtester, S4 runs the shared-brain vol
control engine. This one computes NOTHING. Andrew authors the book himself in the CRM —
tickers and percentages — and the desk reads it READ-ONLY out of the CRM view
``v_tradingdesk_custom_allocations`` and repackages it into the SAME
:class:`strategy_target.Target` dataclass (imported, never forked) so the unchanged
rebalance engine sizes it exactly like an S0 model. It is Andrew's deliberate intermediate
step before moving the book into fully-computed S0.

READ-ONLY, both ends. It SELECTs from one CRM view, loads local price history, and returns
a dataclass. It writes nothing to the CRM (the seam is one-way), contacts no broker, and
places no order. Nothing in this module is wired into any executor — Stage 5 does that.

FOUR TRAPS THIS MODULE EXISTS TO DEFEND (each one verified in the code it names, each one
covered by a test in test_custom_target.py)
-------------------------------------------------------------------------------------
1. NEVER route a custom model through the S0 target path. ``strategy_target.current_target``
   (strategy_target.py:54-62) collapses ANY label ending in " (Small)" into {SCHB, USFR} by
   re-running the S0 backtester. Today's naming ("Growth (Small, Custom)") does not end in
   " (Small)" so it dodges that by luck. Luck is not a control: every dispatch decision here
   keys on the allocation's SOURCE — does this label have rows in the CRM custom-allocation
   view? — via :func:`is_custom_allocation`, NEVER on the label's spelling. A name-based test
   is one CRM rename away from silently discarding Andrew's whole book. This module never
   imports small_tier and never calls strategy_target.current_target.

2. ``Target.version`` IS the model label, verbatim. batch_rebalance_execute.py:442 passes
   ``target.version`` through as ``plan.version`` and crm_execute.py:78 then does
   ``targets[plan.version]``. Any decoration ("Growth (Custom)@v7") is a hard KeyError at
   execution time, not a graceful skip. The allocation's version_number / version_id /
   effective_from / published_at travel SEPARATELY, in :class:`AllocationMeta`, for the
   Stage 6 audit trail.

3. Prices fail LOUDLY on an unknown ticker. ``data_loader.load_prices()`` with
   ``tickers=None`` silently DROPS any ticker it has no file for; downstream that becomes
   price=NaN -> target_shares=0 -> the ticker is never bought AND never trips the rebalance
   band: a silent, permanent under-hold with no exception anywhere. So we always pass the
   allocation's EXPLICIT ticker list, which makes data_loader raise KeyError naming the
   missing ones (data_loader.py:90-95), and we re-raise that as a
   :class:`CustomAllocationError` naming both the tickers and the model.

4. The allocation's own ticker set is exposed (:func:`universe_for`). Downstream,
   batch_rebalance_execute passes S0's universe to ``plan_account``; a held ticker outside
   that set is classified ALIEN, and ALIEN is in ``rebalance_engine._NO_AUTOTRADE_STATUSES``
   (rebalance_engine.py:113) — it never breaches the band and never produces a delta, so
   rotating OUT of a custom ticker would silently do nothing. Stage 5 must pass a custom
   universe; this module builds it.

NO CACHING. Every call reads the CRM. Caching is a caller concern (one run should read once
and pass the rows/labels down), and a stale cached allocation is exactly the failure mode
this seam exists to avoid.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping, Optional, Sequence

import pandas as pd

# The backtester is a local `src` package (run via `python -m src.run`), not an installed
# dependency — same path bootstrap strategy_target.py uses, derived from this file's location
# so it is independent of the current directory.
_BACKTESTER = Path(__file__).resolve().parent.parent / "backtester"
if str(_BACKTESTER) not in sys.path:
    sys.path.insert(0, str(_BACKTESTER))

from src import data_loader  # noqa: E402  (after sys.path setup)

import crm_roster  # noqa: E402
# Reuse the EXISTING Target dataclass (do not fork the target seam). Note this also imports
# strategy_target's module-level deps — we use NOTHING else from it, and in particular never
# call its current_target() for a custom label (trap 1).
from strategy_target import Target  # noqa: E402

# Weights in the CRM are PERCENTAGES that must sum to exactly 100 (DB triggers enforce it on
# publish, and a published version is immutable). The desk still checks, because "the
# database guarantees it" is an assumption about someone else's code, and a book that does
# not sum to 100 is an under- or over-invested account.
PCT_TOTAL = 100.0

# How far the percentage total may stray from 100 before we refuse. Tight on purpose: the
# publish-time trigger enforces EXACT 100, so any drift here means the contract broke or the
# rows were assembled by something other than the view.
PCT_TOTAL_TOLERANCE = 1e-6

# Weights at or below this are treated as "not held" and dropped, matching
# strategy_target.current_target's own 1e-9 filter so a custom book and an S0 book carry the
# same shape into the engine.
_WEIGHT_EPS = 1e-9


class CustomAllocationError(RuntimeError):
    """The custom allocation exists but cannot be turned into a tradeable Target — an unknown
    ticker, a total that is not 100%, a duplicate ticker, a negative weight, or rows that
    disagree about which version they belong to. Always fail LOUD: the alternative is an
    account sized against a book Andrew did not author."""


class NoCustomAllocation(CustomAllocationError):
    """This model has NO published allocation in the CRM (the view returned no rows).

    Raised — never returned as an empty Target. An empty Target means "hold nothing", which
    the rebalance engine would faithfully execute by LIQUIDATING the account. "Nothing
    published yet" and "publish a 100% cash book" must never be the same object."""


@dataclass(frozen=True)
class AllocationMeta:
    """The published-version identity behind a Target, carried SEPARATELY from
    ``Target.version`` (trap 2) for the Stage 6 audit trail / compliance stamp."""
    label: str                      # the model label == Target.version
    strategy_code: Optional[str]
    version_number: Optional[int]
    version_id: Optional[str]
    effective_from: Optional[object]     # date the version took effect (CRM `date`)
    published_at: Optional[object]       # timestamp the version was published (CRM)
    strategy_id: Optional[str]
    tickers: tuple[str, ...] = field(default_factory=tuple)

    def stamp(self) -> dict:
        """Flat dict for the ledger / report header."""
        return {
            "custom_model": self.label,
            "custom_strategy_code": self.strategy_code,
            "custom_version_number": self.version_number,
            "custom_version_id": None if self.version_id is None else str(self.version_id),
            "custom_effective_from": None if self.effective_from is None
                                     else str(self.effective_from),
            "custom_published_at": None if self.published_at is None
                                   else str(self.published_at),
        }


# --- source-based dispatch (trap 1) ---------------------------------------------
def custom_allocation_labels(conn=None) -> set[str]:
    """Every model label that CURRENTLY has a published custom allocation. Thin pass-through
    to the CRM reader, re-exported here so Stage 5 has ONE import for the whole feature."""
    return crm_roster.custom_allocation_labels(conn=conn)


def is_custom_allocation(label: str, conn=None) -> bool:
    """True iff ``label`` has a published allocation in the CRM custom-allocation view.

    THE dispatch test (trap 1). It asks about the allocation's SOURCE, not its NAME, so a
    rename in the CRM can never re-route a hand-authored book into the S0 backtester or into
    small_tier.collapse. Note the consequence, which is deliberate: a custom-NAMED model with
    nothing published yet is NOT custom-dispatched — it has no book to trade — and the caller
    falls through to its normal path, which fails loudly on an unknown version rather than
    trading an empty one."""
    return str(label) in custom_allocation_labels(conn=conn)


def split_labels(labels: Iterable[str], conn=None) -> tuple[list[str], list[str]]:
    """Partition ``labels`` into (custom, non_custom) by allocation SOURCE, in one CRM read.

    Stage 5's dispatch at batch_rebalance_execute.py:313 is then a small, obvious change:
    build the custom ones with :func:`custom_targets_for`, the rest with
    ``strategy_target.current_target`` exactly as today."""
    wanted = [str(v) for v in labels]
    published = custom_allocation_labels(conn=conn)
    custom = [v for v in wanted if v in published]
    other = [v for v in wanted if v not in published]
    return custom, other


# --- row -> Target --------------------------------------------------------------
def _rows_by_label(rows: Iterable[Mapping]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for r in rows:
        grouped.setdefault(str(r["strategy_name"]), []).append(dict(r))
    return grouped


def weights_from_rows(rows: Sequence[Mapping], label: str) -> pd.Series:
    """Convert one model's allocation rows into engine weights: PERCENT (sums to 100) ->
    FRACTION of NAV (sums to 1.0), the unit every downstream consumer assumes.

    Fails loud on: no rows (that is :class:`NoCustomAllocation`, never an empty book), a
    duplicate ticker, a negative weight, and a total that is not 100%."""
    if not rows:
        raise NoCustomAllocation(
            f"custom model {label!r} has NO published allocation in "
            f"{crm_roster.CUSTOM_ALLOCATIONS_VIEW} — refusing to build an empty Target "
            f"(an empty target liquidates the account). Publish a version in the CRM first.")

    seen: dict[str, float] = {}
    for r in rows:
        ticker = str(r["ticker"]).strip().upper()
        pct = float(r["weight_pct"])
        if ticker in seen:
            raise CustomAllocationError(
                f"custom model {label!r}: ticker {ticker} appears more than once in the "
                f"published allocation — ambiguous, refusing to guess the intended weight.")
        if pct < 0:
            raise CustomAllocationError(
                f"custom model {label!r}: ticker {ticker} has a negative weight {pct} — the "
                f"desk does not short a hand-authored model.")
        seen[ticker] = pct

    total = sum(seen.values())
    if abs(total - PCT_TOTAL) > PCT_TOTAL_TOLERANCE:
        raise CustomAllocationError(
            f"custom model {label!r}: published weights sum to {total}% , not {PCT_TOTAL}% "
            f"({len(seen)} ticker(s)). The CRM is supposed to enforce this on publish, so "
            f"treat a mismatch as a broken contract — refusing to size an account against a "
            f"book that is not fully invested.")

    weights = pd.Series({t: p / PCT_TOTAL for t, p in seen.items()}, dtype="float64")
    # Drop 0% lines (legal in the CRM, meaningless to the engine) — same 1e-9 filter S0 uses.
    weights = weights[weights > _WEIGHT_EPS]
    if weights.empty:
        raise CustomAllocationError(
            f"custom model {label!r}: every published weight is zero — refusing to build an "
            f"empty Target (an empty target liquidates the account).")
    return weights.sort_index()


def meta_from_rows(rows: Sequence[Mapping], label: str) -> AllocationMeta:
    """The published-version identity for one model's rows. Fails loud if the rows disagree
    about which version they belong to — the view promises exactly one current published
    version per model, so a mix means the contract broke and the book is not knowable."""
    if not rows:
        raise NoCustomAllocation(
            f"custom model {label!r} has NO published allocation in "
            f"{crm_roster.CUSTOM_ALLOCATIONS_VIEW}.")
    versions = {str(r.get("version_id")) for r in rows}
    numbers = {r.get("version_number") for r in rows}
    if len(versions) > 1 or len(numbers) > 1:
        raise CustomAllocationError(
            f"custom model {label!r}: rows span MULTIPLE allocation versions "
            f"(version_number(s)={sorted(str(n) for n in numbers)}). "
            f"{crm_roster.CUSTOM_ALLOCATIONS_VIEW} promises exactly one current published "
            f"version per model — refusing to blend two books.")
    first = rows[0]
    tickers = tuple(sorted({str(r["ticker"]).strip().upper() for r in rows}))
    vnum = first.get("version_number")
    return AllocationMeta(
        label=label,
        strategy_code=None if first.get("strategy_code") is None
                      else str(first.get("strategy_code")),
        version_number=None if vnum is None else int(vnum),
        version_id=None if first.get("version_id") is None else str(first.get("version_id")),
        effective_from=first.get("effective_from"),
        published_at=first.get("published_at"),
        strategy_id=None if first.get("strategy_id") is None else str(first.get("strategy_id")),
        tickers=tickers,
    )


def _load_prices(tickers: Sequence[str], label: str) -> pd.DataFrame:
    """Price history for EXACTLY these tickers — the loud-failure loader (trap 3).

    ``data_loader.load_prices(tickers=None)`` silently omits any ticker it has no file for,
    which downstream becomes price=NaN -> target_shares=0 -> never bought, never band-
    breaching: a silent permanent under-hold. Passing the explicit list makes data_loader
    raise KeyError listing what is missing (data_loader.py:90-95); we translate that into an
    actionable message naming the model AND the tickers."""
    wanted = list(dict.fromkeys(str(t) for t in tickers))
    try:
        prices = data_loader.load_prices(wanted)
    except KeyError as exc:
        # data_loader's KeyError already names the missing tickers; we do NOT parse its text
        # (that would couple us to its wording). We name the model and the whole requested
        # set, and quote the loader's own message verbatim for the exact offenders.
        raise CustomAllocationError(
            f"custom model {label!r}: NO PRICE HISTORY for one or more of its tickers "
            f"({', '.join(wanted)}). The desk cannot size a "
            f"ticker it has no data for, and it must not silently skip one (that would leave "
            f"the position permanently unfilled and invisible to the rebalance band). Run the "
            f"downloader for these tickers, or correct the allocation in the CRM. "
            f"Underlying loader error: {exc}") from exc
    except CustomAllocationError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise CustomAllocationError(
            f"custom model {label!r}: could not load prices for {wanted}: {exc}") from exc

    missing_cols = [t for t in wanted if t not in prices.columns]
    if missing_cols:
        # Belt-and-suspenders: if a future loader ever drops a column WITHOUT raising, this
        # turns trap 3 back into a loud failure instead of a silent under-hold.
        raise CustomAllocationError(
            f"custom model {label!r}: the price loader returned no column for "
            f"{', '.join(missing_cols)} — refusing to size a book with an unpriced leg.")
    return prices


def build_target(rows: Sequence[Mapping], label: str,
                 *, prices: Optional[pd.DataFrame] = None) -> tuple[Target, AllocationMeta]:
    """Build (Target, AllocationMeta) from one model's allocation rows. Pure apart from the
    price load; contacts no CRM (pass rows in) and no broker.

    ``Target.version`` is set to ``label`` VERBATIM — the exact string the caller uses as the
    key of its ``targets`` dict (trap 2). ``prices`` is an injection seam for tests; production
    leaves it None and gets the loud loader.
    """
    label = str(label)
    weights = weights_from_rows(rows, label)
    meta = meta_from_rows(rows, label)

    tickers = list(weights.index)
    frame = _load_prices(tickers, label) if prices is None else prices
    if frame is None or len(frame.index) == 0:
        raise CustomAllocationError(
            f"custom model {label!r}: price history is empty — nothing to size against.")

    price_date = frame.index[-1]
    # Latest available close per ticker (ffill covers a ticker that did not print on the very
    # last date) — identical handling to strategy_target.current_target.
    latest = frame.ffill().loc[price_date]
    latest = latest.reindex(tickers).astype("float64")
    unpriced = [t for t in tickers if pd.isna(latest.get(t))]
    if unpriced:
        raise CustomAllocationError(
            f"custom model {label!r}: no usable price for {', '.join(unpriced)} as of "
            f"{price_date} (the series exists but is entirely NaN through that date). "
            f"Refusing to size a book with an unpriced leg — it would silently never be "
            f"bought and never breach the rebalance band.")

    # as_of = the date this published version took effect (its "rebalance date"). Falls back
    # to the price date only when the CRM did not supply one, so the field is never invented.
    as_of = meta.effective_from if meta.effective_from is not None else price_date

    target = Target(
        weights=weights,
        prices=latest,
        as_of=pd.Timestamp(as_of),
        price_date=pd.Timestamp(price_date),
        version=label,          # trap 2: EXACTLY the caller's targets-dict key. Never decorated.
    )
    return target, meta


# --- CRM-backed entry points ----------------------------------------------------
def current_target(label: str, *, conn=None,
                   prices: Optional[pd.DataFrame] = None) -> Target:
    """The Target for ONE custom model, read live from the CRM.

    Raises :class:`NoCustomAllocation` if the model has no published allocation (never
    returns an empty Target), :class:`CustomAllocationError` on a malformed book or a
    ticker with no price history, and ``crm_roster.CrmRosterUnavailable`` if the CRM cannot
    be read. Signature mirrors strategy_target.current_target / s4_strategy_target."""
    target, _meta = target_with_meta(label, conn=conn, prices=prices)
    return target


def target_with_meta(label: str, *, conn=None,
                     prices: Optional[pd.DataFrame] = None) -> tuple[Target, AllocationMeta]:
    """:func:`current_target` plus the published-version identity for the audit trail."""
    label = str(label)
    rows = crm_roster.fetch_custom_allocations([label], conn=conn)
    return build_target(rows, label, prices=prices)


def custom_targets_for(labels: Iterable[str], conn=None) -> dict[str, Target]:
    """``{label: Target}`` for the CUSTOM labels among ``labels`` — the Stage 5 entry point.

    Non-custom labels (no published allocation) are simply absent from the result; the caller
    keeps building those with ``strategy_target.current_target`` exactly as today. Each key is
    the caller's own label string, and ``Target.version`` equals that key (trap 2), so
    ``targets[plan.version]`` in crm_execute.py:78 resolves.

    ONE CRM read for the whole batch. Raises rather than skipping if a label HAS a published
    allocation the desk cannot turn into a book — a model that is custom must not fall through
    to the S0 backtester (trap 1)."""
    wanted = [str(v) for v in labels]
    if not wanted:
        return {}
    rows = crm_roster.fetch_custom_allocations(wanted, conn=conn)
    grouped = _rows_by_label(rows)
    out: dict[str, Target] = {}
    for label in wanted:
        if label not in grouped:
            continue           # not a custom model — caller's normal path owns it
        target, _meta = build_target(grouped[label], label)
        out[label] = target
    return out


def custom_targets_with_meta(labels: Iterable[str],
                             conn=None) -> dict[str, tuple[Target, AllocationMeta]]:
    """:func:`custom_targets_for` keeping each model's published-version identity alongside
    its Target, for the Stage 6 audit trail."""
    wanted = [str(v) for v in labels]
    if not wanted:
        return {}
    rows = crm_roster.fetch_custom_allocations(wanted, conn=conn)
    grouped = _rows_by_label(rows)
    out: dict[str, tuple[Target, AllocationMeta]] = {}
    for label in wanted:
        if label not in grouped:
            continue
        out[label] = build_target(grouped[label], label)
    return out


# --- the universe (trap 4) ------------------------------------------------------
def universe_for(target: Target, held: Iterable[str] = (),
                 base: Optional[Iterable[str]] = None) -> set[str]:
    """The tradeable universe to hand ``rebalance_engine.plan_account(universe=...)`` for an
    account on this custom model.

    WHY THIS EXISTS. batch_rebalance_execute currently passes S0's ALL_TICKERS as the
    universe. reconcile classifies a HELD symbol that is outside the universe as ALIEN, and
    ALIEN is in ``rebalance_engine._NO_AUTOTRADE_STATUSES`` (rebalance_engine.py:113): an
    ALIEN line never breaches the band and never produces a delta. So with S0's universe, a
    custom ticker Andrew has since REMOVED from his model would sit in the account forever —
    the rotation would silently do nothing, with no error anywhere. The universe must contain
    every symbol the account may legitimately be traded out of.

    Parameters
    ----------
    target : the custom model's Target (its weights' index is the model's ticker set).
    held : the account's CURRENTLY HELD symbols that the caller wants to be rotatable. Supply
        the symbols this account may legitimately be sold out of — a previous version of this
        model, or the S0 sleeve it is migrating off. Note the trade-off, deliberately left
        with the caller: every symbol you put in here stops being ALIEN, i.e. it loses the
        corp-action human-review guard and becomes eligible for an automatic SELL. Do not
        blanket-pass every position in the account.
    base : optional extra universe to union in (Stage 5 passes S0's ALL_TICKERS so an account
        migrating from an S0 model can rotate out of its old sleeve).
    """
    uni = {str(t) for t in target.weights.index}
    uni |= {str(t) for t in held}
    if base:
        uni |= {str(t) for t in base}
    return uni


def universe_for_targets(targets: Mapping[str, Target], held: Iterable[str] = (),
                         base: Optional[Iterable[str]] = None) -> set[str]:
    """:func:`universe_for` across MANY custom models at once — the union of every model's
    tickers plus the supplied held symbols. Use the per-account
    :func:`universe_for` where you can; this is for a whole-book single-universe caller."""
    uni: set[str] = set()
    for t in targets.values():
        uni |= {str(x) for x in t.weights.index}
    uni |= {str(x) for x in held}
    if base:
        uni |= {str(x) for x in base}
    return uni
