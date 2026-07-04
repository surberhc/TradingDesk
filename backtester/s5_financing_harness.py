r"""
s5_financing_harness.py -- the FOUNDATION for S5 financing research: a shared, honest-fill
SPXW *multi-DTE* options backtest harness.

PAPER / research only. OFFLINE. Windows. STRICTLY READ-ONLY on the options warehouse.
numpy / pandas + duckdb (for the parquet reads only). ASCII-only console output.

================================================================================
WHAT THIS IS (and why it is load-bearing)
================================================================================
Every downstream S5 financing "structure sweep" (put credit spreads, condors, calendars,
diagonals at arbitrary DTE) will IMPORT this module and call its primitives. So the
priorities are CORRECTNESS and HONEST FILLS, not speed.

It GENERALIZES `s5_harvest_engine.py` (which does honest-fill 0DTE iron-condor selling on
the 1-min feed with daily marks) to:
  * arbitrary target DTE  -> nearest available SPXW expiration,
  * arbitrary target delta -> nearest strike on the STORED greek,
  * arbitrary multi-leg STRUCTURES declared as DATA (legs + a management rule),
  * EOD (end-of-day) chain marks from the options warehouse (NOT the 1-min feed).

The conventions reused VERBATIM from s5_harvest_engine.py:
  * HONEST fills: SELL at the BID, BUY at the ASK -- never the mid.
  * $0.65 / leg commission, charged on ENTRY legs and on any ITM cash-settled leg at expiry.
  * Cash-settled European index-option mechanics (SPXW): a leg settles at intrinsic against
    the settlement underlying; OTM legs expire worthless (no trade, no commission).

================================================================================
SCOPE -- EOD MULTI-DTE ONLY; 0DTE IS EXCLUDED BY DESIGN (not a bug, not a gate)
================================================================================
This harness reads the 16:00 EOD warehouse chain, where the 7/14/30/45-DTE structures S5
finances with are liquid and two-sided (~90% of contracts quoted). It is NOT a 0DTE engine
and is NOT validated against 0DTE-on-EOD reproduction:
  * A 0DTE option on the 16:00 EOD chain is AT SETTLEMENT -- its two-sided quote is dead
    (~90% unfillable). The separate intraday engine s5_harvest_engine.py sells 0DTE at 14:00
    on the 1-minute feed, which is a fundamentally different (earlier, liquid) data point.
    Trying to reproduce that 14:00 fill from the 16:00 EOD chain is ill-posed, and refusing
    to fill it is the CORRECT honest behavior here -- not a defect to tune away.
  * 0DTE is refuted as an S5 FINANCING leg anyway (see memory: DDOI gamma / harvest work),
    so this harness deliberately serves only the multi-DTE structures that survived.
  * The code still SUPPORTS a same-day (0DTE) settlement path (see run_trade) so a unit test
    can exercise the mechanics on a synthetic chain, but no real-warehouse validation gate
    asserts 0DTE reproduction. Validation is via mechanical INVARIANTS (defined-risk bounds,
    honest fills, no-lookahead), NOT by matching the 1-min engine's 0DTE P&L.

================================================================================
HARD DATA CONSTRAINT -- the dead window (enforced in the loader, not by convention)
================================================================================
The warehouse's two-sided bid/ask quotes are DEAD outside two clean windows:
    clean window A : 2018-01-02 -> 2020-08-12
    clean window B : 2022-01-03 -> 2026-07-02
The window 2020-08-13 -> 2021-12-31 has ~0-6% two-sided quotes and is NOT fillable. The
loader HARD-EXCLUDES it: `load_chain` on an excluded day raises DeadWindowError, and the
fill model refuses to fill on any day whose quotes are not two-sided. This is a guard, not
a naming convention -- a downstream sweep physically cannot fill a trade in the dead window.

================================================================================
STRUCTURE-AS-DATA (the engine hard-codes NO single structure's logic)
================================================================================
A structure is DECLARED as a `Structure`:
    Structure(
        name="45d_put_credit_spread",
        legs=[Leg(right="PUT", target_delta=0.15, action="sell", dte=45),
              Leg(right="PUT",  strike_offset=-10, action="buy",  dte=45)],
        management=Management(mode="target_or_time", profit_target=0.50, time_exit_dte=21),
    )
and the engine executes it. Adding a new structure is a data change, never an engine edit.

Leg selection is either:
  * target_delta -> nearest stored |delta| strike on that right at the chosen expiration, OR
  * strike_offset -> a fixed point offset from a REFERENCE leg's strike (for defined-risk
    wings: e.g. the long wing is `short_strike - 10`), OR
  * target_moneyness -> nearest strike to underlying*(1+m) (for outright OTM legs).
All legs in a structure share one target DTE unless a leg overrides it (calendars/diagonals).
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import pandas as pd

try:
    import duckdb
except Exception:  # pragma: no cover - duckdb is expected to be installed in the venv
    duckdb = None


# --------------------------------------------------------------------------- #
# FROZEN cost / mechanics constants -- inherited VERBATIM from s5_harvest_engine.
# --------------------------------------------------------------------------- #
COMMISSION_PER_LEG = 0.65        # $/contract/leg; standard retail SPX; a STATED cost.
CONTRACT_MULTIPLIER = 100.0      # SPX index option multiplier.
ROOT = "SPXW"                    # SPX-root is monthly-only; SPXW only for short tenors.

WAREHOUSE = Path(r"C:\TradingDesk-Local\warehouse\raw\options")

# --------------------------------------------------------------------------- #
# Clean fillable windows (INCLUSIVE). The gap between A and B is the DEAD window.
# --------------------------------------------------------------------------- #
CLEAN_WINDOW_A = (_dt.date(2018, 1, 2), _dt.date(2020, 8, 12))
CLEAN_WINDOW_B = (_dt.date(2022, 1, 3), _dt.date(2026, 7, 2))
DEAD_WINDOW = (_dt.date(2020, 8, 13), _dt.date(2021, 12, 31))  # NEVER fillable.

# A day counts as having usable two-sided quotes only if at least this fraction of its
# contracts have bid>0 AND ask>0. Clean days sit ~90%; dead days sit ~0-21%. 0.50 is a
# safe separator (a hard guard, not tuned to any P&L).
MIN_TWO_SIDED_FRAC = 0.50


# Diagnostic tally of WHY entries were rejected during a walk/backtest (observability for
# fix #5's min-credit floor and general drop-rate reporting). Reset with reset_entry_rejects().
ENTRY_REJECTS: dict[str, int] = {"min_credit_floor": 0, "unfillable_or_unselectable": 0}

# Diagnostic tally of trades that could NEVER honestly settle because their expiry falls past
# the last available clean day (entries in the final ~45 DTE of each clean window: near the
# 2020-08-13 dead-window boundary and near end-of-data). These are UNRESOLVED, not real P&L:
# run_trade returns None for them (never books a stale mark) and increments this counter so
# coverage stays transparent. Reset with reset_entry_rejects().
TRUNCATED_DROPPED: int = 0


def reset_entry_rejects() -> None:
    """Zero the entry-rejection + truncated-drop tallies before a fresh backtest so counts are
    per-run."""
    global TRUNCATED_DROPPED
    for k in ENTRY_REJECTS:
        ENTRY_REJECTS[k] = 0
    TRUNCATED_DROPPED = 0


class DeadWindowError(ValueError):
    """Raised when a chain load / fill is attempted on a day in the excluded dead window."""


class NotFillableError(ValueError):
    """Raised when an honest fill is attempted but the leg lacks a two-sided quote."""


class MinCreditFloorError(ValueError):
    """Raised when a net-credit entry's credit does not clear the commission floor -- a bad
    deep-OTM fill artifact, not a real trade. A hard sanity floor (fix #5), not a tunable."""


def is_clean_day(d: _dt.date) -> bool:
    """True iff `d` falls inside clean window A or B (i.e. NOT the dead window / not outside)."""
    return (CLEAN_WINDOW_A[0] <= d <= CLEAN_WINDOW_A[1]) or \
           (CLEAN_WINDOW_B[0] <= d <= CLEAN_WINDOW_B[1])


def is_dead_day(d: _dt.date) -> bool:
    """True iff `d` falls inside the excluded, un-fillable window (2020-08-13..2021-12-31)."""
    return DEAD_WINDOW[0] <= d <= DEAD_WINDOW[1]


# --------------------------------------------------------------------------- #
# EOD chain loader -- clean-window-aware, cached.
# --------------------------------------------------------------------------- #
_CHAIN_CACHE: dict[_dt.date, pd.DataFrame] = {}

_CHAIN_COLS = [
    "date", "symbol", "expiration", "strike", "right",
    "close", "bid", "ask", "bid_size", "ask_size", "volume", "open_interest",
    "implied_vol", "delta", "gamma", "theta", "vega", "underlying_price",
]


def _parquet_path(d: _dt.date, symbol: str = ROOT) -> Path:
    return WAREHOUSE / symbol / f"{d.strftime('%Y%m%d')}.parquet"


def load_chain(d: _dt.date, symbol: str = ROOT, use_cache: bool = True) -> pd.DataFrame:
    """Load the full SPXW EOD chain for trading day `d`.

    Returns a tidy DataFrame (one row per contract) with normalized dtypes:
      date (date), expiration (date), strike (float), right ('PUT'/'CALL'),
      close/bid/ask (float), bid_size/ask_size/volume/open_interest (float),
      implied_vol/delta/gamma/theta/vega/underlying_price (float),
      dte (int, calendar days to expiry), two_sided (bool, bid>0 & ask>0).

    HARD dead-window guard: raises DeadWindowError if `d` is in the excluded window.
    Raises FileNotFoundError if there is no parquet for that day.
    """
    if is_dead_day(d):
        raise DeadWindowError(
            f"{d} is in the excluded dead window {DEAD_WINDOW[0]}..{DEAD_WINDOW[1]} "
            f"-- two-sided quotes are dead here; never fill a trade on this day."
        )
    if use_cache and d in _CHAIN_CACHE:
        return _CHAIN_CACHE[d]

    path = _parquet_path(d, symbol)
    if not path.is_file():
        raise FileNotFoundError(f"no {symbol} chain parquet for {d}: {path}")

    if duckdb is not None:
        con = duckdb.connect()
        try:
            # quote identifiers -- `right` (and others) are duckdb reserved keywords
            cols = ", ".join(f'"{c}"' for c in _CHAIN_COLS)
            df = con.execute(
                f"SELECT {cols} FROM read_parquet(?)", [str(path)]
            ).fetch_df()
        except duckdb.Error as e:
            # ~70 market-holiday days on disk are EMPTY placeholder files that duckdb
            # cannot parse (InvalidInputException). Treat an unreadable placeholder as a
            # MISSING day -- raise the SAME missing-day path a caller already handles for
            # FileNotFoundError. We only swallow the empty/unreadable-placeholder case;
            # any other duckdb failure is re-raised so real corruption never hides.
            raise FileNotFoundError(
                f"{symbol} chain parquet for {d} is an empty/unreadable placeholder "
                f"(treated as a missing/non-trading day): {path} [{type(e).__name__}]"
            ) from e
        finally:
            con.close()
    else:  # pragma: no cover - fallback path
        try:
            df = pd.read_parquet(path, columns=_CHAIN_COLS)
        except Exception as e:
            raise FileNotFoundError(
                f"{symbol} chain parquet for {d} is unreadable (missing/non-trading day): "
                f"{path} [{type(e).__name__}]"
            ) from e

    # A zero-row parquet (some placeholders parse but hold no contracts) is also a
    # non-trading day -- surface it on the missing-day path, not as a downstream crash.
    if len(df) == 0:
        raise FileNotFoundError(
            f"{symbol} chain parquet for {d} has zero rows (missing/non-trading day): {path}"
        )

    df = _normalize_chain(df)
    if use_cache:
        _CHAIN_CACHE[d] = df
    return df


def _normalize_chain(df: pd.DataFrame) -> pd.DataFrame:
    """Coerce raw warehouse rows into the tidy, typed chain the harness expects."""
    df = df.copy()
    # `date` is 'YYYYMMDD', `expiration` is 'YYYY-MM-DD'.
    df["date"] = pd.to_datetime(df["date"].astype(str), format="%Y%m%d").dt.date
    df["expiration"] = pd.to_datetime(df["expiration"].astype(str)).dt.date
    df["right"] = df["right"].astype(str).str.upper()
    for c in ("strike", "close", "bid", "ask", "implied_vol",
              "delta", "gamma", "theta", "vega", "underlying_price",
              "bid_size", "ask_size", "volume", "open_interest"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    # calendar days to expiry (expiry mechanics are calendar; DTE targets are calendar too)
    trade_d = df["date"].iloc[0]
    df["dte"] = df["expiration"].apply(lambda e: (e - trade_d).days)
    df["two_sided"] = (df["bid"] > 0) & (df["ask"] > 0)
    return df.reset_index(drop=True)


def day_two_sided_fraction(chain: pd.DataFrame) -> float:
    """Fraction of contracts on the day with a genuine two-sided quote (bid>0 & ask>0)."""
    if len(chain) == 0:
        return 0.0
    return float(chain["two_sided"].mean())


def day_is_fillable(chain: pd.DataFrame) -> bool:
    """A day is fillable iff enough of its chain is two-sided (dead-window backstop by data,
    not just by date -- a sparse clean day still gets rejected)."""
    return day_two_sided_fraction(chain) >= MIN_TWO_SIDED_FRAC


def _parquet_has_rows(path: Path) -> bool:
    """Cheap validity probe: True iff `path` is a readable parquet with >0 rows.

    ~84 market-holiday days on disk are empty 600-byte placeholder files that duckdb cannot
    parse. This reads only the parquet FOOTER metadata (num_rows), so it is fast enough to
    run across the whole warehouse (~2s for 2200 files) and never loads a chain into memory.
    A file that fails to parse, or reports zero rows, is a non-trading placeholder and is
    treated as MISSING. Only the empty/unreadable case is skipped -- a genuine read error on
    a non-placeholder file would still surface later in load_chain."""
    if duckdb is None:  # pragma: no cover - fallback: assume present, load_chain re-checks
        return True
    con = duckdb.connect()
    try:
        n = con.execute(
            "SELECT num_rows FROM parquet_file_metadata(?)", [str(path)]
        ).fetchone()[0]
        return bool(n) and n > 0
    except duckdb.Error:
        return False   # empty/unreadable placeholder -> treat as a missing day
    finally:
        con.close()


def available_days(symbol: str = ROOT, clean_only: bool = True) -> list[_dt.date]:
    """List trading days on disk for `symbol`. With clean_only=True (default), returns ONLY
    days in clean windows A/B -- the dead window is excluded up front.

    Empty/unreadable market-holiday placeholder files (zero usable rows) are EXCLUDED: they
    are not trading days and must not enter an entry/walk set (else the trade walk aborts on
    a file that cannot be read)."""
    folder = WAREHOUSE / symbol
    if not folder.is_dir():
        return []
    out = []
    for p in sorted(folder.glob("*.parquet")):
        try:
            d = _dt.datetime.strptime(p.stem, "%Y%m%d").date()
        except ValueError:
            continue
        if clean_only and not is_clean_day(d):
            continue
        if not _parquet_has_rows(p):
            continue   # holiday/empty placeholder -> not a usable trading day
        out.append(d)
    return out


# --------------------------------------------------------------------------- #
# Contract SELECTION helpers.
# --------------------------------------------------------------------------- #
def nearest_expiration(chain: pd.DataFrame, target_dte: int,
                       min_dte: int = 0) -> Optional[_dt.date]:
    """The available expiration whose DTE is nearest to `target_dte`.

    `min_dte` filters out expirations too close (default 0 keeps 0DTE). Ties -> the LONGER
    expiration (conservative: never accidentally pick a shorter, riskier tenor)."""
    exps = (chain[chain["dte"] >= min_dte][["expiration", "dte"]]
            .drop_duplicates()
            .sort_values("dte"))
    if exps.empty:
        return None
    exps = exps.assign(err=(exps["dte"] - target_dte).abs())
    best_err = exps["err"].min()
    # tie-break toward the longer (larger dte) expiration
    cand = exps[exps["err"] == best_err].sort_values("dte", ascending=False)
    return cand.iloc[0]["expiration"]


def select_by_delta(chain: pd.DataFrame, expiration: _dt.date, right: str,
                    target_abs_delta: float) -> Optional[float]:
    """Strike on `right` at `expiration` whose stored |delta| is nearest `target_abs_delta`.

    Uses the warehouse's own stored greek (no re-pricing). Returns the strike (float) or
    None if no quotable candidate exists."""
    side = chain[(chain["expiration"] == expiration)
                 & (chain["right"] == right.upper())
                 & (chain["delta"].notna())].copy()
    if side.empty:
        return None
    side["d_err"] = (side["delta"].abs() - target_abs_delta).abs()
    return float(side.sort_values(["d_err", "strike"]).iloc[0]["strike"])


def select_by_moneyness(chain: pd.DataFrame, expiration: _dt.date, right: str,
                        moneyness: float) -> Optional[float]:
    """Strike nearest underlying*(1+moneyness) on `right` at `expiration`. `moneyness` is a
    signed fraction (e.g. -0.20 = 20% OTM put strike; +0.05 = 5% OTM call strike)."""
    side = chain[(chain["expiration"] == expiration)
                 & (chain["right"] == right.upper())].copy()
    if side.empty:
        return None
    und = float(side["underlying_price"].dropna().iloc[0])
    target_k = und * (1.0 + moneyness)
    side["k_err"] = (side["strike"] - target_k).abs()
    return float(side.sort_values(["k_err", "strike"]).iloc[0]["strike"])


def snap_to_available_strike(chain: pd.DataFrame, expiration: _dt.date, right: str,
                             target_strike: float) -> Optional[float]:
    """Nearest ACTUALLY-AVAILABLE strike to `target_strike` on `right` at `expiration`.

    The warehouse grid is 5-wide near the money but coarsens to 10/25/50/100-wide further
    OTM. A defined-risk wing declared as `short - width` therefore frequently lands OFF the
    stored grid; before fix #4 that silently dropped the whole entry (contract_row -> None ->
    ValueError). Snapping the wing to the nearest existing strike keeps the entry alive; the
    REALIZED width (|short - snapped_wing|) is recorded on the position so downstream P&L
    uses the true defined-risk width, not the nominal one. Returns None only if the right/
    expiration side is entirely absent."""
    side = chain[(chain["expiration"] == expiration)
                 & (chain["right"] == right.upper())].copy()
    if side.empty:
        return None
    side["k_err"] = (side["strike"] - target_strike).abs()
    return float(side.sort_values(["k_err", "strike"]).iloc[0]["strike"])


def contract_row(chain: pd.DataFrame, expiration: _dt.date, strike: float,
                 right: str) -> Optional[pd.Series]:
    """Return the single contract row for (expiration, strike, right), or None."""
    m = chain[(chain["expiration"] == expiration)
              & (np.isclose(chain["strike"], strike))
              & (chain["right"] == right.upper())]
    if m.empty:
        return None
    return m.iloc[0]


# --------------------------------------------------------------------------- #
# HONEST fill model (sell at bid, buy at ask; commission on entry legs).
# --------------------------------------------------------------------------- #
def fill_price(row: pd.Series, action: str) -> float:
    """The HONEST fill price for a single leg (in option points, per share of the index):
      sell -> the BID (you receive the bid),
      buy  -> the ASK (you pay the ask).
    Raises NotFillableError if the leg lacks a genuine two-sided quote."""
    action = action.lower()
    bid = float(row["bid"])
    ask = float(row["ask"])
    if not (np.isfinite(bid) and np.isfinite(ask) and bid > 0 and ask > 0):
        raise NotFillableError(
            f"leg not two-sided (bid={bid}, ask={ask}) -- cannot honestly fill"
        )
    if action == "sell":
        return bid
    if action == "buy":
        return ask
    raise ValueError(f"action must be 'buy' or 'sell', got {action!r}")


def leg_entry_cashflow(row: pd.Series, action: str, n_contracts: int = 1
                       ) -> tuple[float, float]:
    """Cashflow ($) and commission ($) of ENTERING one leg at honest fills.

    Returns (cash, commission). `cash` is signed in DOLLARS from the account's view:
      sell -> +bid*mult*n (credit received), buy -> -ask*mult*n (debit paid).
    Commission is always a positive $ cost. NOTE the commission is returned SEPARATELY so
    callers can report credit vs. cost distinctly; net entry = cash - commission."""
    px = fill_price(row, action)
    sign = +1.0 if action.lower() == "sell" else -1.0
    cash = sign * px * CONTRACT_MULTIPLIER * n_contracts
    commission = COMMISSION_PER_LEG * n_contracts
    return cash, commission


def leg_expiry_intrinsic(strike: float, right: str, settle_underlying: float) -> float:
    """Intrinsic value (option points, >= 0) of a single leg at cash settlement against
    `settle_underlying`. CALL = max(S-K, 0); PUT = max(K-S, 0)."""
    if right.upper() == "CALL":
        return max(settle_underlying - strike, 0.0)
    return max(strike - settle_underlying, 0.0)


def leg_expiry_cashflow(strike: float, right: str, action: str, settle_underlying: float,
                        n_contracts: int = 1) -> tuple[float, float]:
    """Cashflow ($) and commission ($) of a single leg CASH-SETTLING at expiry.

    A long option ITM RECEIVES intrinsic; a short option ITM PAYS intrinsic. OTM legs
    expire worthless (0 cash, 0 commission -- no trade). Commission ($0.65/leg) is charged
    ONLY on legs that settle ITM (a settlement event), matching s5_harvest_engine."""
    intrinsic = leg_expiry_intrinsic(strike, right, settle_underlying)
    if intrinsic <= 0.0:
        return 0.0, 0.0            # OTM: worthless, no trade, no commission
    sign = +1.0 if action.lower() == "buy" else -1.0   # long receives, short pays
    cash = sign * intrinsic * CONTRACT_MULTIPLIER * n_contracts
    commission = COMMISSION_PER_LEG * n_contracts
    return cash, commission


# --------------------------------------------------------------------------- #
# STRUCTURE-AS-DATA declarations.
# --------------------------------------------------------------------------- #
@dataclass
class Leg:
    """One option leg of a structure, DECLARED (not selected yet).

    Exactly one selection mode must be set:
      * target_delta  : nearest stored |delta| strike (the usual short-strike selector),
      * target_moneyness : nearest strike to underlying*(1+m) (outright OTM legs),
      * strike_offset : a fixed point offset from a REFERENCE leg's chosen strike -- used
                        for defined-risk wings (e.g. long wing = short_strike - width). The
                        reference is `ref_leg` (index into the structure's legs list).
    `dte` overrides the structure-level DTE for this leg (calendars/diagonals). If None the
    structure DTE is used.
    """
    right: str                        # 'PUT' or 'CALL'
    action: str                       # 'sell' or 'buy'
    target_delta: Optional[float] = None
    target_moneyness: Optional[float] = None
    strike_offset: Optional[float] = None
    ref_leg: Optional[int] = None     # required iff strike_offset is set
    dte: Optional[int] = None         # per-leg DTE override (calendars/diagonals)
    n_contracts: int = 1

    def __post_init__(self):
        modes = sum(x is not None for x in
                    (self.target_delta, self.target_moneyness, self.strike_offset))
        if modes != 1:
            raise ValueError(
                "each Leg needs EXACTLY one of target_delta / target_moneyness / "
                f"strike_offset (got {modes})"
            )
        if (self.strike_offset is not None) and (self.ref_leg is None):
            raise ValueError("strike_offset legs require ref_leg (the reference leg index)")
        self.right = self.right.upper()
        self.action = self.action.lower()


@dataclass
class Management:
    """The exit rule for a structure. `mode` is one of:
      'hold'          : hold to expiry (cash-settle every leg at intrinsic).
      'profit_target' : close early when open profit >= profit_target * entry_credit.
      'time_exit'     : close when calendar DTE falls to <= time_exit_dte.
      'target_or_time': whichever of the two fires FIRST.
    `stop_mult`, if set (any mode), ALSO closes early when the open LOSS reaches
      stop_mult * entry_credit (an N x-credit stop-loss). A loss stop can co-exist with a
      profit target / time exit; whichever binds first wins.

    Profit / loss are measured on the structure's marked value vs. the entry credit, using
    honest CLOSE marks (buy back what you sold at ask, sell what you bought at bid).
    """
    mode: str = "hold"
    profit_target: Optional[float] = None   # fraction of entry credit (e.g. 0.50)
    time_exit_dte: Optional[int] = None      # close at/under this many calendar DTE
    stop_mult: Optional[float] = None        # N x-credit loss stop (e.g. 2.0)

    def __post_init__(self):
        valid = {"hold", "profit_target", "time_exit", "target_or_time"}
        if self.mode not in valid:
            raise ValueError(f"management.mode must be one of {valid}, got {self.mode!r}")
        if self.mode in ("profit_target", "target_or_time") and self.profit_target is None:
            raise ValueError(f"mode {self.mode} requires profit_target")
        if self.mode in ("time_exit", "target_or_time") and self.time_exit_dte is None:
            raise ValueError(f"mode {self.mode} requires time_exit_dte")


@dataclass
class Structure:
    """A tradeable structure DECLARED as data: legs + a target DTE + a management rule."""
    name: str
    legs: list[Leg]
    dte: int                         # structure-level target DTE (legs may override)
    management: Management = field(default_factory=Management)


# --------------------------------------------------------------------------- #
# A resolved (selected) position -- concrete strikes/expirations chosen on entry day.
# --------------------------------------------------------------------------- #
@dataclass
class ResolvedLeg:
    right: str
    action: str
    strike: float
    expiration: _dt.date
    n_contracts: int
    entry_fill: float               # option-points fill price (bid if sold, ask if bought)
    entry_delta: float
    entry_iv: float


@dataclass
class Position:
    name: str
    entry_date: _dt.date
    legs: list[ResolvedLeg]
    entry_credit: float             # net $ credit at entry (sum of signed leg fills, NOT
                                    #   yet net of commission); >0 = net credit taken in
    entry_commission: float         # $ commission paid to enter all legs
    entry_underlying: float
    last_expiration: _dt.date       # the latest expiry across legs (when the book is flat)
    # REALIZED (snapped) width per strike_offset leg: {leg_index -> |ref_strike - snapped|}.
    # A wing declared as short-10 may snap to short-25 on a coarse OTM grid; this records the
    # width actually taken on so downstream defined-risk math uses the TRUE width. Empty for
    # structures with no offset legs.
    realized_widths: dict = field(default_factory=dict)
    # nominal (declared) offset magnitude per offset leg, for reporting snap slippage.
    nominal_widths: dict = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# ENTRY: resolve a Structure into a Position on a given trading day.
# --------------------------------------------------------------------------- #
def open_position(structure: Structure, d: _dt.date,
                  chain: Optional[pd.DataFrame] = None) -> Position:
    """Resolve `structure`'s declared legs into concrete contracts on day `d` and take the
    HONEST entry fills. Raises DeadWindowError (via load_chain) on a dead day, and
    NotFillableError / ValueError if any leg cannot be selected or honestly filled.
    """
    if chain is None:
        chain = load_chain(d)

    # Choose each leg's expiration first (structure DTE, or a per-leg override).
    resolved: list[ResolvedLeg] = []
    chosen_strikes: list[Optional[float]] = [None] * len(structure.legs)

    # Two passes: delta/moneyness legs first (they are self-contained), then offset legs
    # (which reference an already-chosen strike). This lets a wing key off its short leg.
    order = sorted(range(len(structure.legs)),
                   key=lambda i: structure.legs[i].strike_offset is not None)

    realized_widths: dict = {}
    nominal_widths: dict = {}

    for i in order:
        leg = structure.legs[i]
        dte = leg.dte if leg.dte is not None else structure.dte
        exp = nearest_expiration(chain, dte)
        if exp is None:
            raise ValueError(f"no expiration near DTE={dte} on {d} for leg {i}")

        if leg.target_delta is not None:
            k = select_by_delta(chain, exp, leg.right, leg.target_delta)
        elif leg.target_moneyness is not None:
            k = select_by_moneyness(chain, exp, leg.right, leg.target_moneyness)
        else:  # strike_offset -- a defined-risk wing keyed off a reference leg.
            ref = chosen_strikes[leg.ref_leg]
            if ref is None:
                raise ValueError(f"leg {i} references leg {leg.ref_leg} which is unresolved")
            nominal_k = ref + leg.strike_offset
            # FIX #4: SNAP the wing to the nearest available strike (the OTM grid coarsens,
            # so the nominal strike is often off-grid). Record the REALIZED width so P&L uses
            # the true defined-risk width, never silently drop the entry.
            k = snap_to_available_strike(chain, exp, leg.right, nominal_k)
            if k is not None:
                realized_widths[i] = abs(ref - k)
                nominal_widths[i] = abs(leg.strike_offset)
        if k is None:
            raise ValueError(f"could not select a strike for leg {i} ({leg.right}) on {d}")
        chosen_strikes[i] = k

        row = contract_row(chain, exp, k, leg.right)
        if row is None:
            raise ValueError(f"selected strike {k} {leg.right} {exp} absent from chain on {d}")
        px = fill_price(row, leg.action)   # raises NotFillableError if not two-sided
        resolved.append((i, ResolvedLeg(
            right=leg.right, action=leg.action, strike=float(k), expiration=exp,
            n_contracts=leg.n_contracts, entry_fill=px,
            entry_delta=float(row["delta"]) if np.isfinite(row["delta"]) else float("nan"),
            entry_iv=float(row["implied_vol"]) if np.isfinite(row["implied_vol"]) else float("nan"),
        )))

    # restore original leg order
    resolved.sort(key=lambda t: t[0])
    legs = [rl for _, rl in resolved]

    entry_credit = 0.0
    entry_commission = 0.0
    for rl in legs:
        sign = +1.0 if rl.action == "sell" else -1.0
        entry_credit += sign * rl.entry_fill * CONTRACT_MULTIPLIER * rl.n_contracts
        entry_commission += COMMISSION_PER_LEG * rl.n_contracts

    # FIX #5: MIN-CREDIT SANITY FLOOR (a hard sanity floor, NOT a tunable knob). You cannot
    # honestly sell a credit structure for a net credit that does not even clear the
    # commission cost of entering it -- a non-positive or sub-commission net credit is a bad
    # fill artifact from wide, deep-OTM two-sided quotes, not a real trade. We reject such an
    # entry (raise) and let the caller count it. The floor is fixed at the entry commission
    # cost; there is no free parameter to tune. (Only enforced for net-CREDIT structures --
    # a declared net-debit structure legitimately pays a debit.)
    if entry_credit > 0 and entry_credit <= entry_commission:
        raise MinCreditFloorError(
            f"entry net credit ${entry_credit:.2f} <= commission floor "
            f"${entry_commission:.2f} on {d} -- bad deep-OTM fill artifact, entry rejected"
        )

    und = float(chain["underlying_price"].dropna().iloc[0])
    last_exp = max(rl.expiration for rl in legs)
    return Position(
        name=structure.name, entry_date=d, legs=legs,
        entry_credit=entry_credit, entry_commission=entry_commission,
        entry_underlying=und, last_expiration=last_exp,
        realized_widths=realized_widths, nominal_widths=nominal_widths,
    )


# --------------------------------------------------------------------------- #
# MARK: honest close value of an open position on a later day.
# --------------------------------------------------------------------------- #
def close_fill_price(row: pd.Series, action: str) -> float:
    """Honest price to CLOSE a leg that was opened with `action`:
      opened by sell -> close by BUYING back at the ASK,
      opened by buy  -> close by SELLING at the BID.
    Raises NotFillableError if not two-sided."""
    closing_action = "buy" if action.lower() == "sell" else "sell"
    return fill_price(row, closing_action)


def mark_position(pos: Position, d: _dt.date,
                  chain: Optional[pd.DataFrame] = None
                  ) -> tuple[float, float]:
    """Honest cost to CLOSE `pos` on day `d`, as (close_debit, close_commission) in $.

    `close_debit` is signed $ from the account view: to flatten, you BUY back shorts (pay
    ask) and SELL longs (receive bid). Positive close_debit = it costs you $ to flatten;
    negative = you receive $ to flatten. Realized trade P&L if closed here (net of ALL
    commissions) = entry_credit - entry_commission + close_debit - close_commission.

    Raises NotFillableError if ANY leg is not two-sided on `d` (can't honestly close).
    """
    if chain is None:
        chain = load_chain(d)
    close_cash = 0.0
    close_commission = 0.0
    for rl in pos.legs:
        row = contract_row(chain, rl.expiration, rl.strike, rl.right)
        if row is None:
            raise NotFillableError(
                f"leg {rl.right} {rl.strike} {rl.expiration} absent on {d} -- cannot mark"
            )
        px = close_fill_price(row, rl.action)   # raises NotFillableError if not two-sided
        # closing a short = buy (cash out); closing a long = sell (cash in)
        sign = -1.0 if rl.action == "sell" else +1.0
        close_cash += sign * px * CONTRACT_MULTIPLIER * rl.n_contracts
        close_commission += COMMISSION_PER_LEG * rl.n_contracts
    return close_cash, close_commission


def settle_position(pos: Position, settle_underlying: float) -> tuple[float, float]:
    """Cash-settle EVERY leg of `pos` at expiry against `settle_underlying`.
    Returns (settle_cash, settle_commission) in $. settle_cash is signed from the account
    view (long ITM receives +, short ITM pays -). Commission charged only on ITM legs."""
    settle_cash = 0.0
    settle_commission = 0.0
    for rl in pos.legs:
        cash, comm = leg_expiry_cashflow(rl.strike, rl.right, rl.action,
                                         settle_underlying, rl.n_contracts)
        settle_cash += cash
        settle_commission += comm
    return settle_cash, settle_commission


# --------------------------------------------------------------------------- #
# The MANAGEMENT + P&L engine: walk one position day-by-day to its exit.
# --------------------------------------------------------------------------- #
@dataclass
class TradeResult:
    name: str
    entry_date: _dt.date
    exit_date: _dt.date
    exit_reason: str                 # 'settle' | 'profit_target' | 'time_exit' | 'stop'
                                     #   (window-tail trades that can never settle are NOT
                                     #    emitted -- run_trade returns None for them)
    entry_credit: float              # $ (gross, pre-commission)
    net_pnl: float                   # $ realized, NET of ALL commissions
    total_commission: float          # $ total commissions (entry + exit/settle)
    entry_underlying: float
    exit_underlying: float
    hold_days: int
    # daily mark path: list of (date, mark_pnl_$) where mark_pnl is the OPEN P&L that day
    marks: list = field(default_factory=list)


def _open_pnl(pos: Position, close_cash: float, close_commission: float) -> float:
    """Open (unrealized) P&L in $ if the position were flattened at these honest marks,
    net of entry AND the hypothetical exit commission."""
    return (pos.entry_credit - pos.entry_commission) + close_cash - close_commission


def run_trade(structure: Structure, entry_date: _dt.date,
              chain_days: list[_dt.date],
              settle_underlying_fn: Optional[Callable[[_dt.date, Position], float]] = None,
              chain_loader: Callable[[_dt.date], pd.DataFrame] = load_chain,
              ) -> Optional[TradeResult]:
    """Open `structure` on `entry_date`, then walk each subsequent trading day in
    `chain_days` (which must be the sorted clean days from entry through >= expiry) applying
    the management rule, until an exit fires or the position expires.

    `settle_underlying_fn(expiry_date, pos)` supplies the settlement underlying at expiry;
    default uses the underlying_price recorded in the expiry-day chain (or, if that day is
    missing/not clean, the last available clean day's underlying at/after expiry is NOT
    used -- we require the expiry day itself to be clean to settle honestly).

    Returns a TradeResult, or None if entry could not be taken (dead day / not fillable /
    unselectable) -- downstream can skip such days.
    """
    try:
        entry_chain = chain_loader(entry_date)
    except (DeadWindowError, FileNotFoundError):
        return None
    if not day_is_fillable(entry_chain):
        return None
    try:
        pos = open_position(structure, entry_date, entry_chain)
    except MinCreditFloorError:
        # bad deep-OTM fill artifact -- count it (reported by backtest_structure) and skip.
        ENTRY_REJECTS["min_credit_floor"] += 1
        return None
    except (NotFillableError, ValueError, DeadWindowError):
        ENTRY_REJECTS["unfillable_or_unselectable"] += 1
        return None

    mgmt = structure.management
    marks: list[tuple[_dt.date, float]] = []

    def _settle_result(d: _dt.date, chain: pd.DataFrame) -> TradeResult:
        """Cash-settle every leg at expiry against `chain`'s underlying (or the injected
        settle fn) on day `d`, and build the terminal 'settle' TradeResult."""
        if settle_underlying_fn is not None:
            settle_und = settle_underlying_fn(d, pos)
        else:
            settle_und = float(chain["underlying_price"].dropna().iloc[0])
        settle_cash, settle_comm = settle_position(pos, settle_und)
        net_pnl = (pos.entry_credit - pos.entry_commission) + settle_cash - settle_comm
        total_comm = pos.entry_commission + settle_comm
        marks.append((d, net_pnl))
        return TradeResult(
            name=pos.name, entry_date=entry_date, exit_date=d,
            exit_reason="settle", entry_credit=pos.entry_credit,
            net_pnl=net_pnl, total_commission=total_comm,
            entry_underlying=pos.entry_underlying, exit_underlying=settle_und,
            hold_days=(d - entry_date).days, marks=marks,
        )

    # SAME-DAY (0DTE) SETTLEMENT: if the position expires ON the entry day, it must settle
    # that same day -- there is no "future" day to walk to. (Convention: on a 0DTE trade we
    # DECIDE the entry and take the honest fill from the entry-day chain, then cash-settle
    # against that same chain's EOD underlying. Both use ONLY entry-day data, so there is no
    # look-ahead: the settlement spot is the same EOD mark that priced the fills.) Before
    # fix #2 the walk list `[d for d in chain_days if entry_date < d <= last_expiration]`
    # was EMPTY for 0DTE, so every 0DTE trade returned None and was silently dropped.
    if pos.last_expiration <= entry_date:
        return _settle_result(entry_date, entry_chain)

    # days strictly after entry, up to and including the last expiration
    future = [d for d in chain_days if entry_date < d <= pos.last_expiration]

    for d in future:
        # is this day the expiry? (all legs share last_expiration here, or a calendar's
        # final leg -- we treat expiry as the day == last_expiration)
        is_expiry = (d == pos.last_expiration)
        try:
            chain = chain_loader(d)
        except (DeadWindowError, FileNotFoundError):
            # can't mark/settle honestly on this day; skip marking, keep holding.
            continue
        if not day_is_fillable(chain):
            continue

        # DTE of the (governing) expiration as of day d
        gov_dte = (pos.last_expiration - d).days

        if not is_expiry:
            # mark the open position honestly
            try:
                close_cash, close_comm = mark_position(pos, d, chain)
            except NotFillableError:
                continue   # a leg lost its two-sided quote today; hold, try tomorrow
            open_pnl = _open_pnl(pos, close_cash, close_comm)
            marks.append((d, open_pnl))

            exit_reason = _check_management(mgmt, pos, open_pnl, gov_dte)
            if exit_reason is not None:
                net_pnl = open_pnl
                total_comm = pos.entry_commission + close_comm
                exit_und = float(chain["underlying_price"].dropna().iloc[0])
                return TradeResult(
                    name=pos.name, entry_date=entry_date, exit_date=d,
                    exit_reason=exit_reason, entry_credit=pos.entry_credit,
                    net_pnl=net_pnl, total_commission=total_comm,
                    entry_underlying=pos.entry_underlying, exit_underlying=exit_und,
                    hold_days=(d - entry_date).days, marks=marks,
                )
        else:
            # EXPIRY: cash-settle every leg at intrinsic against this day's chain.
            return _settle_result(d, chain)

    # Fell off the end of chain_days without reaching expiry: the position's expiry lies
    # PAST the last available clean day, so it can NEVER honestly settle -- the data does not
    # exist there. This happens for entries in the final ~45 DTE of each clean window (near
    # the 2020-08-13 dead-window boundary and near end-of-data). Booking the last stale mark
    # as realized P&L would emit non-real P&L into the trade frame, so we DO NOT do that:
    # the trade is UNRESOLVED. Count it (TRUNCATED_DROPPED, surfaced by backtest_structure)
    # for coverage transparency and return None so it is excluded from the tidy output.
    global TRUNCATED_DROPPED
    TRUNCATED_DROPPED += 1
    return None


def _check_management(mgmt: Management, pos: Position, open_pnl: float,
                      gov_dte: int) -> Optional[str]:
    """Return an exit reason string if the management rule binds on this day's open P&L /
    DTE, else None. Loss stop (if any) is checked in every mode.

    Profit target: open_pnl >= profit_target * |entry_credit| (credit structures only --
    for a NET-DEBIT structure entry_credit is negative and the target is meaningless, so we
    guard on entry_credit > 0). Time exit: gov_dte <= time_exit_dte. Stop: open_pnl <=
    -stop_mult * |entry_credit|.
    """
    credit = pos.entry_credit
    # loss stop first (risk control binds before profit-taking on the same day if both hit)
    if mgmt.stop_mult is not None and credit > 0:
        if open_pnl <= -mgmt.stop_mult * credit:
            return "stop"

    if mgmt.mode == "hold":
        return None

    hit_target = (mgmt.profit_target is not None and credit > 0
                  and open_pnl >= mgmt.profit_target * credit)
    hit_time = (mgmt.time_exit_dte is not None and gov_dte <= mgmt.time_exit_dte)

    if mgmt.mode == "profit_target":
        return "profit_target" if hit_target else None
    if mgmt.mode == "time_exit":
        return "time_exit" if hit_time else None
    if mgmt.mode == "target_or_time":
        if hit_target:
            return "profit_target"
        if hit_time:
            return "time_exit"
        return None
    return None


# --------------------------------------------------------------------------- #
# BACKTEST DRIVER: run a structure entered every clean day, collect tidy P&L.
# --------------------------------------------------------------------------- #
def backtest_structure(structure: Structure,
                       start: Optional[_dt.date] = None,
                       end: Optional[_dt.date] = None,
                       entry_days: Optional[list[_dt.date]] = None,
                       max_days: Optional[int] = None,
                       verbose: bool = False,
                       progress_every: int = 50) -> pd.DataFrame:
    """Enter `structure` on every clean trading day in [start, end] (or on `entry_days`),
    run each trade to its exit, and return a tidy per-TRADE DataFrame:
      columns: entry_date, exit_date, exit_reason, hold_days, entry_credit, net_pnl,
               total_commission, entry_underlying, exit_underlying, name.

    Only clean days are considered for entry; dead days are physically excluded by the
    loader. A day where entry can't be taken (not fillable / unselectable) is skipped.

    UNRESOLVED window-tail trades (expiry past the last clean day -- they can never honestly
    settle) are EXCLUDED from the frame and counted; the count is surfaced on the returned
    DataFrame's `.attrs["truncated_dropped"]` (and the entry-reject tallies on
    `.attrs["entry_rejects"]`) so coverage is transparent without polluting the tidy columns.
    """
    reset_entry_rejects()   # per-run tallies (entry rejects + truncated drops)
    all_days = available_days(clean_only=True)
    if start is not None:
        all_days = [d for d in all_days if d >= start]
    if end is not None:
        all_days = [d for d in all_days if d <= end]
    if entry_days is not None:
        entry_set = set(entry_days)
        entries = [d for d in all_days if d in entry_set]
    else:
        entries = list(all_days)
    if max_days is not None:
        entries = entries[:max_days]

    # chain_days used to walk each trade: ALL clean days on disk (so a trade can settle even
    # if its entry set is a subset). Restrict to a reasonable horizon around the entries.
    walk_days = all_days  # already clean-only, sorted

    rows = []
    n = len(entries)
    for i, d in enumerate(entries, 1):
        res = run_trade(structure, d, walk_days)
        if res is not None:
            rows.append({
                "name": res.name,
                "entry_date": res.entry_date,
                "exit_date": res.exit_date,
                "exit_reason": res.exit_reason,
                "hold_days": res.hold_days,
                "entry_credit": res.entry_credit,
                "net_pnl": res.net_pnl,
                "total_commission": res.total_commission,
                "entry_underlying": res.entry_underlying,
                "exit_underlying": res.exit_underlying,
            })
        if verbose and (i % progress_every == 0 or i == n):
            done = len(rows)
            print(f"[{i}/{n}] {d} traded={done}", flush=True)

    out = pd.DataFrame(rows)
    # surface coverage tallies without polluting the tidy per-trade columns
    out.attrs["truncated_dropped"] = TRUNCATED_DROPPED
    out.attrs["entry_rejects"] = dict(ENTRY_REJECTS)
    return out


def per_day_pnl(trades: pd.DataFrame) -> pd.DataFrame:
    """Collapse a per-TRADE frame into a per-DAY realized-P&L frame keyed on entry_date
    (one sell/entry per day here). Downstream annualizers key off this: a per-day net-$ P&L
    series they can express as a % of a stated core notional and feed the placebo/OOS/DSR/
    regime evaluators. Columns: date, net_pnl, entry_credit, exit_reason."""
    if trades.empty:
        return pd.DataFrame(columns=["date", "net_pnl", "entry_credit", "exit_reason"])
    g = trades.rename(columns={"entry_date": "date"})
    return (g.groupby("date", as_index=False)
             .agg(net_pnl=("net_pnl", "sum"),
                  entry_credit=("entry_credit", "sum"),
                  exit_reason=("exit_reason", "first"))
             .sort_values("date")
             .reset_index(drop=True))


# --------------------------------------------------------------------------- #
# Convenience structure builders (declared-as-data; NOT frozen strategy config).
# --------------------------------------------------------------------------- #
def iron_condor(dte: int, short_delta: float, wing: float,
                management: Optional[Management] = None,
                name: Optional[str] = None) -> Structure:
    """A symmetric iron condor: short put + long put wing (below) and short call + long
    call wing (above), each `wing` points wide, short strikes at |delta| = short_delta."""
    legs = [
        Leg(right="PUT", action="sell", target_delta=short_delta),     # 0: short put
        Leg(right="PUT", action="buy", strike_offset=-wing, ref_leg=0),  # 1: put wing
        Leg(right="CALL", action="sell", target_delta=short_delta),    # 2: short call
        Leg(right="CALL", action="buy", strike_offset=+wing, ref_leg=2),  # 3: call wing
    ]
    return Structure(name=name or f"{dte}d_iron_condor_{short_delta}d_{wing}w",
                     legs=legs, dte=dte,
                     management=management or Management(mode="hold"))


def put_credit_spread(dte: int, short_delta: float, wing: float,
                      management: Optional[Management] = None,
                      name: Optional[str] = None) -> Structure:
    """A put credit spread: sell a put at |delta|=short_delta, buy the `wing`-wide put below."""
    legs = [
        Leg(right="PUT", action="sell", target_delta=short_delta),      # 0: short put
        Leg(right="PUT", action="buy", strike_offset=-wing, ref_leg=0),  # 1: long put wing
    ]
    return Structure(name=name or f"{dte}d_put_credit_spread_{short_delta}d_{wing}w",
                     legs=legs, dte=dte,
                     management=management or Management(mode="hold"))


def clear_cache() -> None:
    """Drop the in-memory chain cache (free memory between big sweeps)."""
    _CHAIN_CACHE.clear()
