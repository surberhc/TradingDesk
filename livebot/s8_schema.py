"""S8 live-pilot data-capture — pure schema definitions (Phase 0).

No I/O, no IBKR, no network. This module defines the analysis-grade record shapes
for the S8 zero-transmit live pilot:

  - make_trade_id(...)         stable unique key per (would-be) trade
  - LegGrab                    a full snapshot of one option leg at one instant
  - TradeRecord (+ nested
    EntryInfo / ExitInfo /
    Provenance)                the durable entry+exit summary, one per trade
  - TICK_COLUMNS               columns for the per-trade full-tick parquet table
  - MARKET_COLUMNS             columns for the intraday market-context parquet table

TradeRecord.to_dict()/from_dict() round-trip through plain dicts (nested dataclasses
become nested dicts) for append-only JSONL storage.

Observation layer only: nothing here picks entries or exits. It records what the
frozen S8 strategy already decided (rule #1 stays clean).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


# --------------------------------------------------------------------------- #
# Stable trade key
# --------------------------------------------------------------------------- #

def make_trade_id(
    date: str,
    template: str,
    slot: str,
    short_strike: Any,
    long_strike: Any,
) -> str:
    """Build a stable, unique key for a (would-be) trade.

    Deterministic in its inputs: the same trade always yields the same id, so an
    updated exit record collides with (and thus overwrites, latest-wins) the
    entry-only record for the same trade.

    Example
    -------
    >>> make_trade_id("20260717", "Puts-80-$4", "12:35", 7480, 7445)
    '20260717:Puts-80-$4:12:35:7480:7445'
    """
    parts = [
        str(date),
        str(template),
        str(slot),
        _fmt_strike(short_strike),
        _fmt_strike(long_strike),
    ]
    return ":".join(parts)


def _fmt_strike(strike: Any) -> str:
    """Render a strike compactly: whole numbers drop the trailing ``.0``."""
    if isinstance(strike, float) and strike.is_integer():
        return str(int(strike))
    return str(strike)


# --------------------------------------------------------------------------- #
# Leg snapshot
# --------------------------------------------------------------------------- #

@dataclass
class LegGrab:
    """A full data grab for one option leg at one instant.

    ``complete`` flags whether the model greeks had populated at grab time (they
    arrive after a short settle-delay); an incomplete grab records what is present
    and is flagged rather than silently writing nulls.
    """

    right: Optional[str] = None            # "P" / "C"
    strike: Optional[float] = None
    bid: Optional[float] = None
    ask: Optional[float] = None
    last: Optional[float] = None
    bid_size: Optional[float] = None
    ask_size: Optional[float] = None
    volume: Optional[float] = None
    open_interest: Optional[float] = None
    delta: Optional[float] = None
    gamma: Optional[float] = None
    vega: Optional[float] = None
    theta: Optional[float] = None
    iv: Optional[float] = None
    underlying_spot: Optional[float] = None
    grab_ts: Optional[str] = None
    complete: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> Optional["LegGrab"]:
        if d is None:
            return None
        known = {k: d.get(k) for k in cls.__dataclass_fields__}
        return cls(**known)


# --------------------------------------------------------------------------- #
# Trade record (entry + exit summary)
# --------------------------------------------------------------------------- #

@dataclass
class EntryInfo:
    entry_ts: Optional[str] = None
    entry_spot: Optional[float] = None
    entry_vix: Optional[float] = None
    entry_realized_vol: Optional[float] = None
    short_strike: Optional[float] = None
    long_strike: Optional[float] = None
    width: Optional[float] = None
    realized_credit: Optional[float] = None
    stop_multiple: Optional[float] = None
    stop_price: Optional[float] = None
    short_leg: Optional[LegGrab] = None
    long_leg: Optional[LegGrab] = None
    greeks_complete: bool = False

    def to_dict(self) -> Dict[str, Any]:
        d = dict(self.__dict__)
        d["short_leg"] = self.short_leg.to_dict() if self.short_leg else None
        d["long_leg"] = self.long_leg.to_dict() if self.long_leg else None
        return d

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> Optional["EntryInfo"]:
        if d is None:
            return None
        known = {k: d.get(k) for k in cls.__dataclass_fields__}
        known["short_leg"] = LegGrab.from_dict(d.get("short_leg"))
        known["long_leg"] = LegGrab.from_dict(d.get("long_leg"))
        return cls(**known)


@dataclass
class ExitInfo:
    exit_ts: Optional[str] = None
    exit_reason: Optional[str] = None      # "stop_hit" / "expiry" / "eod" / None
    exit_spot: Optional[float] = None
    short_leg_exit: Optional[LegGrab] = None
    long_leg_exit: Optional[LegGrab] = None
    spread_value_at_exit: Optional[float] = None
    pnl: Optional[float] = None
    max_adverse_excursion: Optional[float] = None
    duration_secs: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        d = dict(self.__dict__)
        d["short_leg_exit"] = self.short_leg_exit.to_dict() if self.short_leg_exit else None
        d["long_leg_exit"] = self.long_leg_exit.to_dict() if self.long_leg_exit else None
        return d

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> Optional["ExitInfo"]:
        if d is None:
            return None
        known = {k: d.get(k) for k in cls.__dataclass_fields__}
        known["short_leg_exit"] = LegGrab.from_dict(d.get("short_leg_exit"))
        known["long_leg_exit"] = LegGrab.from_dict(d.get("long_leg_exit"))
        return cls(**known)


@dataclass
class Provenance:
    paperbot_version: Optional[str] = None
    pilot_mode: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> "Provenance":
        if d is None:
            return cls()
        known = {k: d.get(k) for k in cls.__dataclass_fields__}
        return cls(**known)


@dataclass
class TradeRecord:
    """One durable, analysis-grade record per (would-be) trade — entry + exit."""

    trade_id: str
    date: Optional[str] = None
    account: Optional[str] = None
    template: Optional[str] = None
    slot: Optional[str] = None
    side: Optional[str] = None              # "PUT" / "CALL"
    expiration: Optional[str] = None
    qty: Optional[int] = None
    status: str = "open"                     # "open" / "closed"
    entry: EntryInfo = field(default_factory=EntryInfo)
    exit: Optional[ExitInfo] = None
    provenance: Provenance = field(default_factory=Provenance)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "trade_id": self.trade_id,
            "date": self.date,
            "account": self.account,
            "template": self.template,
            "slot": self.slot,
            "side": self.side,
            "expiration": self.expiration,
            "qty": self.qty,
            "status": self.status,
            "entry": self.entry.to_dict() if self.entry else None,
            "exit": self.exit.to_dict() if self.exit else None,
            "provenance": self.provenance.to_dict() if self.provenance else None,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "TradeRecord":
        return cls(
            trade_id=d["trade_id"],
            date=d.get("date"),
            account=d.get("account"),
            template=d.get("template"),
            slot=d.get("slot"),
            side=d.get("side"),
            expiration=d.get("expiration"),
            qty=d.get("qty"),
            status=d.get("status", "open"),
            entry=EntryInfo.from_dict(d.get("entry")) or EntryInfo(),
            exit=ExitInfo.from_dict(d.get("exit")),
            provenance=Provenance.from_dict(d.get("provenance")),
        )


# --------------------------------------------------------------------------- #
# Time-series parquet table schemas
# --------------------------------------------------------------------------- #

# Per-trade full-tick leg time-series (quotes + greeks), date-partitioned parquet.
TICK_COLUMNS = [
    "trade_id",
    "ts",
    "leg",              # "short" / "long"
    "right",
    "strike",
    "bid",
    "ask",
    "last",
    "bid_size",
    "ask_size",
    "volume",
    "open_interest",
    "delta",
    "gamma",
    "vega",
    "theta",
    "iv",
    "underlying_spot",
]

# Intraday ATM-band chain / underlying / VIX context, date-partitioned parquet.
MARKET_COLUMNS = [
    "ts",
    "expiration",
    "strike",
    "right",
    "bid",
    "ask",
    "last",
    "bid_size",
    "ask_size",
    "volume",
    "open_interest",
    "delta",
    "gamma",
    "vega",
    "theta",
    "iv",
    "underlying_spot",
    "vix",
]
