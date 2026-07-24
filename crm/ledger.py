"""ledger.py — the CRM sleeve-ledger CORE (conductor #42/#43).

The heart of Option A (docs/CRM_DESIGN_groups_brain.md §3.5): the book IBKR does NOT
keep for us. IBKR shows only the *blended* client account; this ledger is the ONLY
source of the per-`(account, sleeve)` split of those blended totals. It holds the split
(positions + cash per sleeve), applies attributed fills to it, detects the netting hazard
that a single blended account creates (§7.3), and produces the reconciliation checksum
(§7.4) whose classifications a future fault-latch layer (§12.3) will consume.

Source-of-truth boundary (§3.5): the broker is authoritative for the BLENDED totals; the
CRM ledger is authoritative for the per-sleeve SPLIT of those totals. Reconciliation (§7.4)
is what keeps the split honest against the totals.

Attribution reality (§13.3, verified 2026-07-23): IBKR returns allocation-order executions
ONLY at the FA master (`DF8922141`), never at a `DU…` sub. So a block's per-account split is
NOT read from `execution.acctNumber` — it comes from OUR OWN written `ContractsOrShares`
split, confirmed against per-account position deltas. `attribute_block_fill` below is built
around that fact: the caller passes the split it wrote, not an execution stream.

HARD BOUNDARIES honored here (load-bearing — do not cross):
  * PURE / OFFLINE. stdlib only (dataclasses, enum, datetime, math, typing) + crm.domain
    (itself pure). NO broker, NO ib_async, NO order path, NO gateway, NO reqPositions /
    accountSummary — those live in a FUTURE live driver that FEEDS this module its numbers.
    crm/ is deliberately dependency-free so it unit-tests with zero infra and can never drag
    broker plumbing into the brain.
  * TRANSPORT IS OPEN (spec §8 / §10.2 — "do not build until chosen"). We provide clean
    to_dict()/from_dict() on the stateful entities as the FUTURE transport boundary, but
    write NO persistence: no JSON reader/writer, no DB, no file I/O anywhere in this file.
  * WEIGHTS ARE FROZEN (CLAUDE.md rule #1). The reconciliation tolerances (`pos_tol`,
    `cash_tol`, position-deletion epsilon) are MECHANICAL float-slop params with sensible
    defaults — NOT frozen strategy numbers, NOT tunable-as-strategy. They only decide
    "is this the same number within float/rounding noise," never anything about allocation.
  * THIS SLICE IS LEDGER STATE + OPERATIONS ONLY. It does NOT build the §12 fault-latch /
    triage lifecycle (next slice) — but the §7.4 checksum here PRODUCES the classifications
    (LEDGER_DRIFT / ALIEN / CASH_DRIFT and the OK/DRIFT/REVIEW verdict) that the future
    latch layer (§12.3) consumes: a DRIFT verdict is what will latch an account out.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Mapping, Optional

import domain  # crm.domain — pure; used for SLEEVE_REGISTRY / the group→sleeve invariant


# =============================================================================
# Mechanical tolerances — float/rounding slop only (NOT frozen strategy numbers)
# =============================================================================
# When an attributed position lands within this of zero we DELETE the key (a fully-closed
# sleeve slice holds no line). Pure float-cleanup — mechanical, not a strategy threshold.
POS_EPS = 1e-9

# Reconciliation defaults (overridable per call). MECHANICAL params, clearly not strategy:
#   POS_TOL — shares/contracts are effectively integer; this only absorbs float noise.
#   CASH_TOL — $1.00 absorbs sub-dollar rounding/commission-allocation slop in TotalCash.
# These decide "same number within noise," never allocation. Rule #1 does not touch them.
POS_TOL = 1e-6
CASH_TOL = 1.0


# =============================================================================
# 1) Instrument identity  (§3.5 "symbol → qty (+ per-option conId/right/strike/expiry)")
# =============================================================================
@dataclass(frozen=True, eq=False)
class Instrument:
    """One tradeable line the ledger can attribute to a sleeve.

    Frozen + hashable so it can KEY the `attributed_positions` dict. Equality is deliberately
    NOT field-by-field (hence `eq=False` + hand-written __eq__/__hash__):
      * an EQUITY is identified by (symbol, sec_type) — a stock is a stock regardless of the
        con_id/multiplier a caller happened to fill in;
      * an OPTION (or any non-STK) is identified by the FULL tuple so two SPX legs with
        different strikes/expiries/rights are distinct lines, per §3.5.
    This is what lets the S8 sleeve's individual option legs live side by side in one map."""
    symbol: str
    sec_type: str = "STK"
    con_id: Optional[int] = None
    expiry: Optional[str] = None          # YYYYMMDD
    strike: Optional[float] = None
    right: Optional[str] = None           # "C" / "P"
    multiplier: float = 1.0

    # --- identity ---------------------------------------------------------------
    def _identity(self) -> tuple:
        """The tuple that defines equality/hash. Equity collapses to (symbol, sec_type);
        anything else disambiguates on the full contract tuple."""
        if self.sec_type == "STK":
            return (self.symbol, self.sec_type)
        return (self.symbol, self.sec_type, self.con_id, self.expiry,
                self.strike, self.right, self.multiplier)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Instrument):
            return NotImplemented
        return self._identity() == other._identity()

    def __hash__(self) -> int:
        return hash(self._identity())

    # --- serialization ----------------------------------------------------------
    def key(self) -> str:
        """Canonical, reversible string id (see from_key) used as the serialized map key
        for `attributed_positions` (§3.5). Every field is encoded so a round-trip through
        key()/from_key() reconstructs an equal Instrument. Empty field == None."""
        return "|".join([
            self.symbol,
            self.sec_type,
            "" if self.con_id is None else str(self.con_id),
            self.expiry or "",
            "" if self.strike is None else repr(self.strike),
            self.right or "",
            repr(self.multiplier),
        ])

    @classmethod
    def from_key(cls, s: str) -> "Instrument":
        symbol, sec_type, con_id, expiry, strike, right, mult = s.split("|")
        return cls(
            symbol=symbol,
            sec_type=sec_type,
            con_id=int(con_id) if con_id else None,
            expiry=expiry or None,
            strike=float(strike) if strike else None,
            right=right or None,
            multiplier=float(mult),
        )

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "sec_type": self.sec_type,
            "con_id": self.con_id,
            "expiry": self.expiry,
            "strike": self.strike,
            "right": self.right,
            "multiplier": self.multiplier,
        }

    @classmethod
    def from_dict(cls, d: Mapping) -> "Instrument":
        return cls(
            symbol=d["symbol"],
            sec_type=d.get("sec_type", "STK"),
            con_id=d.get("con_id"),
            expiry=d.get("expiry"),
            strike=d.get("strike"),
            right=d.get("right"),
            multiplier=d.get("multiplier", 1.0),
        )

    # --- convenience constructors ----------------------------------------------
    @classmethod
    def stock(cls, symbol: str) -> "Instrument":
        """An equity line (multiplier 1)."""
        return cls(symbol=symbol, sec_type="STK", multiplier=1.0)

    @classmethod
    def option(cls, symbol: str, expiry: str, strike: float, right: str,
               con_id: Optional[int] = None, multiplier: float = 100.0) -> "Instrument":
        """An option line (default multiplier 100). Disambiguated by the full tuple."""
        return cls(symbol=symbol, sec_type="OPT", con_id=con_id, expiry=expiry,
                   strike=float(strike), right=right, multiplier=multiplier)


# =============================================================================
# 2) Ledger entry + ledger  (§3.5)
# =============================================================================
@dataclass
class SleeveLedgerEntry:
    """One row of the sleeve ledger: the CRM's attributed slice of a blended account for a
    single sleeve (§3.5). `ledger_version` is the monotonic transport/versioning key — bumped
    on every attributed mutation. NOT frozen: this is the mutable state the ledger maintains."""
    account_id: str
    sleeve_id: str
    target_weight: float = 0.0
    attributed_positions: dict = field(default_factory=dict)  # Instrument -> qty (float)
    attributed_cash: float = 0.0
    last_reconciled_at: Optional[datetime] = None
    ledger_version: int = 0

    def to_dict(self) -> dict:
        return {
            "account_id": self.account_id,
            "sleeve_id": self.sleeve_id,
            "target_weight": self.target_weight,
            # Instrument keys serialized via key() (§3.5); qty is the value.
            "attributed_positions": {
                inst.key(): qty for inst, qty in self.attributed_positions.items()
            },
            "attributed_cash": self.attributed_cash,
            "last_reconciled_at": (
                None if self.last_reconciled_at is None
                else self.last_reconciled_at.isoformat()
            ),
            "ledger_version": self.ledger_version,
        }

    @classmethod
    def from_dict(cls, d: Mapping) -> "SleeveLedgerEntry":
        return cls(
            account_id=d["account_id"],
            sleeve_id=d["sleeve_id"],
            target_weight=d.get("target_weight", 0.0),
            attributed_positions={
                Instrument.from_key(k): qty
                for k, qty in d.get("attributed_positions", {}).items()
            },
            attributed_cash=d.get("attributed_cash", 0.0),
            last_reconciled_at=(
                None if d.get("last_reconciled_at") is None
                else datetime.fromisoformat(d["last_reconciled_at"])
            ),
            ledger_version=d.get("ledger_version", 0),
        )


class SleeveLedger:
    """In-memory sleeve ledger: entries keyed `(account_id, sleeve_id)` (§3.5).

    PURE / in-memory only — no persistence (transport is the open question §8). `now` is
    INJECTABLE on every mutation so tests fully control time (no hidden datetime.now)."""

    def __init__(self) -> None:
        self._entries: dict[tuple[str, str], SleeveLedgerEntry] = {}

    # --- access / get-or-create -------------------------------------------------
    def entry(self, account: str, sleeve: str, *,
              target_weight: float = 0.0) -> SleeveLedgerEntry:
        """Get-or-create the `(account, sleeve)` row. A freshly created row starts EMPTY at
        version 0 with the given target_weight. `target_weight` is only applied on CREATE —
        an existing row is returned unchanged (get-or-create semantics)."""
        k = (account, sleeve)
        e = self._entries.get(k)
        if e is None:
            e = SleeveLedgerEntry(account_id=account, sleeve_id=sleeve,
                                  target_weight=target_weight)
            self._entries[k] = e
        return e

    # --- mutation ---------------------------------------------------------------
    def apply_delta(self, account: str, sleeve: str, instrument: Instrument,
                    qty_delta: float, cash_delta: float, *,
                    now: Optional[datetime] = None,
                    reconciled: bool = False) -> SleeveLedgerEntry:
        """Apply a signed position + cash delta to one sleeve slice (§3.5 / §7.2 primitive).

        `attributed_positions[instrument] += qty_delta` (the key is DELETED when the running
        qty lands within POS_EPS of zero — a fully-closed line holds nothing);
        `attributed_cash += cash_delta`; `ledger_version` bumps by 1. `last_reconciled_at` is
        set to `now` IFF `reconciled=True` (a plain fill is NOT a reconcile). Positive
        `qty_delta` buys INTO the sleeve, negative sells OUT. Returns the updated entry."""
        e = self.entry(account, sleeve)
        new_qty = e.attributed_positions.get(instrument, 0.0) + qty_delta
        if abs(new_qty) <= POS_EPS:
            e.attributed_positions.pop(instrument, None)
        else:
            e.attributed_positions[instrument] = new_qty
        e.attributed_cash += cash_delta
        e.ledger_version += 1
        if reconciled:
            e.last_reconciled_at = now
        return e

    def attribute_fill(self, account: str, sleeve: str, instrument: Instrument,
                       qty_delta: float, price: float, *,
                       commission: float = 0.0,
                       now: Optional[datetime] = None) -> float:
        """Attribute ONE filled leg to a sleeve (§7.2 primitive). Computes

            cash_delta = -(qty_delta * price * instrument.multiplier) - commission

        so a BUY (qty_delta > 0) drains cash and a SELL (qty_delta < 0) adds it, while the
        commission is ALWAYS a cost. Then delegates to apply_delta. A combo/spread caller
        invokes this ONCE PER LEG (each leg its own instrument/qty/price). Returns the
        cash_delta applied (handy for block-level reporting)."""
        cash_delta = -(qty_delta * price * instrument.multiplier) - commission
        self.apply_delta(account, sleeve, instrument, qty_delta, cash_delta, now=now)
        return cash_delta

    # --- blended views (the ledger's side of the §7.4 checksum) -----------------
    def blended_positions(self, account: str) -> dict:
        """Σ attributed_positions across the account's sleeves — the ledger's view of the
        BLENDED holdings, to check against broker truth (§7.4). Lines that net to ~0 across
        sleeves are dropped (POS_EPS)."""
        out: dict[Instrument, float] = {}
        for (acct, _sleeve), e in self._entries.items():
            if acct != account:
                continue
            for inst, qty in e.attributed_positions.items():
                out[inst] = out.get(inst, 0.0) + qty
        return {inst: qty for inst, qty in out.items() if abs(qty) > POS_EPS}

    def blended_cash(self, account: str) -> float:
        """Σ attributed_cash across the account's sleeves (≈ broker TotalCash, §3.5/§7.4)."""
        return sum(e.attributed_cash for (acct, _s), e in self._entries.items()
                   if acct == account)

    def entries_for_account(self, account: str) -> list:
        return [e for (acct, _s), e in self._entries.items() if acct == account]

    def all_entries(self) -> list:
        return list(self._entries.values())

    # --- serialization ----------------------------------------------------------
    def to_dict(self) -> dict:
        return {"entries": [e.to_dict() for e in self._entries.values()]}

    @classmethod
    def from_dict(cls, d: Mapping) -> "SleeveLedger":
        led = cls()
        for ed in d.get("entries", []):
            e = SleeveLedgerEntry.from_dict(ed)
            led._entries[(e.account_id, e.sleeve_id)] = e
        return led


# =============================================================================
# 3) Group→sleeve map + block attribution  (§7.1 / §7.2 / §13.3)
# =============================================================================
def build_group_sleeve_map(
        registry: Mapping[str, "domain.Sleeve"] = None) -> dict[str, str]:
    """Invert the sleeve registry to `fa_group_name → sleeve_id` (§7.1).

    RAISES if any FA group maps to >1 sleeve — this enforces the load-bearing §7.1 invariant
    "one group == one sleeve," which is the ONLY reason a block fill can recover its sleeve
    from the group instead of from the orderRef. If that invariant ever breaks, the block
    orderRef would need a sleeve tag and this map would be ambiguous — so we fail loud here."""
    reg = domain.SLEEVE_REGISTRY if registry is None else registry
    out: dict[str, str] = {}
    for sleeve_id, sleeve in reg.items():
        g = sleeve.fa_group_name
        if g in out and out[g] != sleeve_id:
            raise ValueError(
                f"FA group {g!r} maps to >1 sleeve ({out[g]!r} and {sleeve_id!r}) — the "
                f"§7.1 'one group == one sleeve' invariant is broken; block attribution "
                f"could not recover the sleeve from the group.")
        out[g] = sleeve_id
    return out


def attribute_block_fill(ledger: SleeveLedger, *,
                         fa_group: str,
                         per_account_split: Mapping[str, float],
                         instrument: Instrument,
                         price: float,
                         commission_total: float = 0.0,
                         side: str = "BUY",
                         group_sleeve_map: Optional[Mapping[str, str]] = None,
                         now: Optional[datetime] = None) -> dict:
    """The §7.2 block-attribution engine, built on the §13.3 reality that there are no
    per-subaccount executions.

    The sleeve is recovered from the GROUP (`group_sleeve_map[fa_group]`, built from the
    registry if not passed) — NOT from `execution.acctNumber` (§7.1/§13.3). The per-account
    quantities come from `per_account_split`, which is OUR OWN written `ContractsOrShares`
    split for the block (the intended-allocation record), and each account's commission is
    pro-rata to its share of the total quantity (mirroring the observed avgCost split in
    §13.1: PDBC 2 @ 18.14, commission 0.368706, each of two accounts booked at 18.3244).

    `side` sets the sign: BUY → +qty (cash down), SELL → −qty (cash up). One
    `ledger.attribute_fill` call per account. Returns a per-account report of what was
    applied: {account: {sleeve, signed_qty, commission_share, cash_delta}}."""
    s = side.upper()
    if s not in ("BUY", "SELL"):
        raise ValueError(f"side must be BUY or SELL, got {side!r}")
    sign = 1.0 if s == "BUY" else -1.0

    gsm = group_sleeve_map if group_sleeve_map is not None else build_group_sleeve_map()
    if fa_group not in gsm:
        raise ValueError(
            f"fa_group {fa_group!r} has no sleeve in the group→sleeve map "
            f"({', '.join(sorted(gsm))}).")
    sleeve = gsm[fa_group]

    total_qty = sum(abs(q) for q in per_account_split.values())
    if total_qty <= 0:
        raise ValueError(
            f"per_account_split total quantity is {total_qty} — nothing to attribute.")

    applied: dict[str, dict] = {}
    for account, qty in per_account_split.items():
        signed_qty = sign * abs(qty)
        commission_share = commission_total * (abs(qty) / total_qty)
        cash_delta = ledger.attribute_fill(
            account, sleeve, instrument, signed_qty, price,
            commission=commission_share, now=now)
        applied[account] = {
            "sleeve": sleeve,
            "signed_qty": signed_qty,
            "commission_share": commission_share,
            "cash_delta": cash_delta,
        }
    return applied


# =============================================================================
# 4) Netting watch  (§7.3) — PURE detection + split
# =============================================================================
@dataclass(frozen=True)
class NettingConflict:
    """One account-level netting hazard (§7.3): a single instrument that ≥2 sleeves would
    trade in the same cycle. The broker holds ONE blended position and will NET the two
    orders the ledger thinks are separate, so this must be detected before block aggregation.

    `per_sleeve` maps sleeve_id → that sleeve's signed requested delta; `net` is their sum."""
    instrument: Instrument
    per_sleeve: Mapping[str, int]
    net: int


def detect_netting(
        per_sleeve_deltas: Mapping[str, Mapping[Instrument, int]]) -> list:
    """Scan ONE account's per-sleeve requested deltas and return a NettingConflict for every
    instrument that appears in ≥2 sleeves' delta maps (§7.3 "Detect"). Instruments touched by
    a single sleeve are not conflicts. PURE — detection only, no orders."""
    # instrument -> {sleeve_id: delta}
    by_instrument: dict[Instrument, dict[str, int]] = {}
    for sleeve_id, deltas in per_sleeve_deltas.items():
        for inst, delta in deltas.items():
            by_instrument.setdefault(inst, {})[sleeve_id] = delta

    conflicts: list[NettingConflict] = []
    for inst, per_sleeve in by_instrument.items():
        if len(per_sleeve) >= 2:
            conflicts.append(NettingConflict(
                instrument=inst,
                per_sleeve=dict(per_sleeve),
                net=sum(per_sleeve.values()),
            ))
    return conflicts


def net_and_split(conflict: NettingConflict) -> tuple:
    """Resolve a netting conflict via §7.3 option (a) — the chosen DEFAULT: net the delta at
    the ACCOUNT level, place the single net order, then split the resulting fill back across
    the sleeves so the ledger stays correct even though the broker only ever saw one net
    order. (Option (b) — hold the smaller sleeve's leg and alert — is the documented
    alternative; we default to (a) with this audit note per §7.3.)

    Returns `(net_delta_to_place, split_back)`:
      * `net_delta_to_place` — the signed net to send to the broker (`conflict.net`).
      * `split_back` — sleeve_id → FRACTION of the achieved net fill to attribute to that
        sleeve. Fractions = requested_delta / net, so they SUM TO 1.0 and, multiplied by the
        net fill (whole or partial), reconstruct each sleeve's intended delta pro-rata to its
        request. E.g. {S0:+10, S8:-3} → net +7, split_back {S0: 10/7, S8: -3/7}.

    net == 0 (fully offsetting): there is NOTHING to place and NO fill to split, so this
    returns `(0, {})` — a wash. The internal cross (booking each sleeve's own requested
    delta, which is known exactly from `conflict.per_sleeve`) is a separate ledger op the
    caller does directly, since no broker interaction occurs."""
    net = conflict.net
    if net == 0:
        return 0, {}
    split_back = {sleeve_id: delta / net
                  for sleeve_id, delta in conflict.per_sleeve.items()}
    return net, split_back


# =============================================================================
# 5) Reconciliation checksum  (§7.4) — PURE; produces the §12.3 classifications
# =============================================================================
@dataclass(frozen=True)
class InstrumentReconStatus:
    """Per-instrument reconciliation outcome (§7.4). `status` is one of:
      * "MATCH"        — ledger split reconciles to broker truth within POS_TOL.
      * "LEDGER_DRIFT" — the ledger attributes a qty that does NOT reconcile to broker truth
                         (partial fill, rounding, dropped/extra attribution). Hard drift.
      * "ALIEN"        — the broker holds an instrument the ledger attributes ZERO of
                         (corp action / manual trade) — unattributable, surfaced for review,
                         NEVER auto-swept (mirrors rebalance_engine ALIEN)."""
    instrument: Instrument
    ledger_qty: float
    broker_qty: float
    status: str


@dataclass(frozen=True)
class ReconResult:
    """The account-level reconciliation checksum result (§7.4) — the "checksum on every
    reconcile" the future §12.3 latch layer acts on (a DRIFT verdict is what latches an
    account out). `verdict` is one of:
      * "OK"     — every instrument MATCH and cash MATCH.
      * "REVIEW" — some ALIEN holding(s) but no hard drift (needs a human look, not a latch).
      * "DRIFT"  — some LEDGER_DRIFT or CASH_DRIFT (fail-closed: the latch layer pulls the
                   account out of automated trading until a human reconciles)."""
    account_id: str
    per_instrument: list
    ledger_cash: float
    broker_cash: float
    cash_status: str            # "MATCH" | "CASH_DRIFT"
    verdict: str                # "OK" | "DRIFT" | "REVIEW"

    @property
    def drift_instruments(self) -> list:
        return [s for s in self.per_instrument if s.status == "LEDGER_DRIFT"]

    @property
    def alien_instruments(self) -> list:
        return [s for s in self.per_instrument if s.status == "ALIEN"]


def reconcile_account(ledger: SleeveLedger, account: str,
                      broker_positions: Mapping[Instrument, float],
                      broker_total_cash: float, *,
                      pos_tol: float = POS_TOL,
                      cash_tol: float = CASH_TOL) -> ReconResult:
    """§7.4 checksum for one account: does `Σ_sleeves attributed_positions` reconcile to the
    broker's blended holdings, and `Σ_sleeves attributed_cash` to broker TotalCash?

    Over the UNION of ledger-blended and broker instruments, per instrument:
      * |ledger_qty − broker_qty| <= pos_tol            → MATCH
      * else if ledger attributes ~0 (broker holds it)  → ALIEN  (unattributable → review)
      * else                                            → LEDGER_DRIFT (hard drift)
    Cash: |ledger_cash − broker_cash| <= cash_tol → MATCH else CASH_DRIFT.

    Verdict precedence: DRIFT (any LEDGER_DRIFT or CASH_DRIFT) outranks REVIEW (any ALIEN)
    outranks OK. `pos_tol`/`cash_tol` are MECHANICAL float/rounding-slop params (see module
    header) — NOT frozen strategy numbers. PURE — no broker calls; the broker snapshot is
    passed IN by a future live driver."""
    ledger_blended = ledger.blended_positions(account)

    instruments = set(ledger_blended) | set(broker_positions)
    per_instrument: list[InstrumentReconStatus] = []
    for inst in instruments:
        lq = ledger_blended.get(inst, 0.0)
        bq = broker_positions.get(inst, 0.0)
        if abs(lq - bq) <= pos_tol:
            status = "MATCH"
        elif abs(lq) <= pos_tol:
            # Broker holds it, ledger attributes zero → unattributable (§7.4). Never swept.
            status = "ALIEN"
        else:
            status = "LEDGER_DRIFT"
        per_instrument.append(InstrumentReconStatus(
            instrument=inst, ledger_qty=lq, broker_qty=bq, status=status))

    ledger_cash = ledger.blended_cash(account)
    cash_status = ("MATCH" if abs(ledger_cash - broker_total_cash) <= cash_tol
                   else "CASH_DRIFT")

    has_drift = cash_status == "CASH_DRIFT" or any(
        s.status == "LEDGER_DRIFT" for s in per_instrument)
    has_alien = any(s.status == "ALIEN" for s in per_instrument)
    if has_drift:
        verdict = "DRIFT"
    elif has_alien:
        verdict = "REVIEW"
    else:
        verdict = "OK"

    # Deterministic ordering for stable reporting/serialization downstream.
    per_instrument.sort(key=lambda s: s.instrument.key())
    return ReconResult(
        account_id=account,
        per_instrument=per_instrument,
        ledger_cash=ledger_cash,
        broker_cash=broker_total_cash,
        cash_status=cash_status,
        verdict=verdict,
    )
