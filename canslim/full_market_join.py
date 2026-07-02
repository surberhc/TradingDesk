"""
full_market_join.py — CAN SLIM full-market SELECTION, Phase 1: the leak-free JOIN leg.

Joins the survivorship-free daily PRICE/VOLUME warehouse (full_market_prices.py) to the
point-in-time EDGAR FUNDAMENTALS (edgar_pipeline / quarterly_fundamentals_full) via
CIK <-> ticker <-> date, with NO look-ahead. This is the single access layer the full-market
selection backtest will call to ask, for a symbol on a decision date D:
    * the price/volume history up to and including D (for RS + base detection), and
    * the fundamentals that were ALREADY FILED as of D (EPS/sales growth, ROE, margins).

TWO LEAK-FREE INVARIANTS (both enforced here, both covered by tests)
-------------------------------------------------------------------
  1. PRICE never uses a bar dated after D. `prices_asof(sym, D)` returns rows with date <= D.
  2. FUNDAMENTALS never use a filing filed after D. `fundamentals_asof(cik, D)` returns only
     rows whose `filed` date <= D, then takes the latest period among those — so a later
     RESTATEMENT of an old period is invisible until the date it was actually filed. Keying on
     `filed` (not `period_end`) is the whole point: it reproduces what was knowable on D.

THE CIK<->TICKER<->DATE JOIN
----------------------------
The universe timeline (cik_ticker_timeline.csv) maps every CIK to its trading symbol(s),
including survivorship-recovered old symbols for renamed/delisted names. Prices are stored per
SYMBOL; fundamentals are keyed by CIK. The join keys on CIK: for a member (CIK, ticker, year),
we load that ticker's price file and that CIK's fundamentals, so a rename is handled by pricing
the historical symbol while reading the one CIK's continuous fundamentals. Point-in-time ticker:
membership already pins which symbol is valid in which year (full_market_universe.build_membership),
so we never price a symbol under a year it did not trade.

DATA (local warehouse, never on Drive):
    C:/TradingDesk-Local/canslim/prices/<SYMBOL>.parquet
    C:/TradingDesk-Local/canslim/edgar/quarterly_fundamentals_full/shard=*.parquet
    C:/TradingDesk-Local/canslim/universe/cik_ticker_timeline.csv
    C:/TradingDesk-Local/canslim/universe/universe_membership.csv

Only this CODE lives in the Drive repo.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import pandas as pd

EDGAR = Path(r"C:\TradingDesk-Local\canslim\edgar")
PRICES = Path(r"C:\TradingDesk-Local\canslim\prices")
UNIVERSE = Path(r"C:\TradingDesk-Local\canslim\universe")

QUARTERLY_FULL_DIR = EDGAR / "quarterly_fundamentals_full"
TIMELINE_CSV = UNIVERSE / "cik_ticker_timeline.csv"
MEMBERSHIP_CSV = UNIVERSE / "universe_membership.csv"

FULL_SHARDS = 20   # fundamentals are sharded by cik % 20 (mirror of edgar_pipeline)

# growth/quality columns the CAN SLIM screen consumes (carried through untouched)
FUND_COLS = [
    "cik", "ticker", "fy", "fq", "period_end", "filed",
    "eps_diluted", "eps_growth_yoy", "sales_growth_yoy", "sales_growth_qoq",
    "roe_ttm_annualized", "net_margin", "operating_margin", "gross_margin",
    "revenue", "net_income",
]


# ------------------------------------------------------------------------------------------
# Price side (leak-free by date)
# ------------------------------------------------------------------------------------------

def load_prices(symbol: str) -> pd.DataFrame | None:
    """Full daily price/volume history for a symbol (date-sorted), or None if not pulled yet."""
    p = PRICES / f"{symbol.upper()}.parquet"
    if not p.exists():
        return None
    df = pd.read_parquet(p)
    if df.empty:
        return None
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values("date").reset_index(drop=True)


def prices_asof(symbol: str, as_of) -> pd.DataFrame | None:
    """Price/volume history for `symbol` with date <= as_of (leak-free). None if unavailable."""
    df = load_prices(symbol)
    if df is None:
        return None
    as_of = pd.Timestamp(as_of)
    out = df[df["date"] <= as_of]
    return out if not out.empty else None


# ------------------------------------------------------------------------------------------
# Fundamentals side (leak-free by FILING date)
# ------------------------------------------------------------------------------------------

@lru_cache(maxsize=FULL_SHARDS)
def _fund_shard(shard: int) -> pd.DataFrame:
    sp = QUARTERLY_FULL_DIR / f"shard={shard}.parquet"
    cols = [c for c in FUND_COLS]
    d = pd.read_parquet(sp, columns=cols)
    d["filed"] = pd.to_datetime(d["filed"], errors="coerce")
    d["period_end"] = pd.to_datetime(d["period_end"], errors="coerce")
    return d


def load_fundamentals(cik: int) -> pd.DataFrame:
    """All clean quarterly fundamentals rows for one CIK (unsorted view of the shard subset)."""
    d = _fund_shard(int(cik) % FULL_SHARDS)
    return d[d["cik"] == int(cik)].copy()


def fundamentals_asof(cik: int, as_of) -> pd.DataFrame:
    """
    Point-in-time fundamentals for `cik` as-of `as_of`: only rows whose FILING date <= as_of,
    sorted by period_end then filed (chronological knowledge order). The LAST row is the most
    recent reported quarter that was public by as_of. Empty frame if nothing was filed yet.

    A later restatement of an older period is excluded until its own `filed` date, so the
    screen sees exactly what was knowable on `as_of` — no restatement look-ahead.
    """
    d = load_fundamentals(cik)
    as_of = pd.Timestamp(as_of)
    d = d[d["filed"].notna() & (d["filed"] <= as_of)]
    if d.empty:
        return d
    return d.sort_values(["period_end", "filed"]).reset_index(drop=True)


def latest_fundamentals_asof(cik: int, as_of) -> pd.Series | None:
    """The single most-recent as-of-known quarterly fundamentals row for `cik`, or None."""
    d = fundamentals_asof(cik, as_of)
    return None if d.empty else d.iloc[-1]


# ------------------------------------------------------------------------------------------
# Membership / timeline access
# ------------------------------------------------------------------------------------------

def load_membership() -> pd.DataFrame:
    if not MEMBERSHIP_CSV.exists():
        raise SystemExit(f"missing {MEMBERSHIP_CSV} — run full_market_universe.py membership")
    return pd.read_csv(MEMBERSHIP_CSV)


def load_timeline() -> pd.DataFrame:
    if not TIMELINE_CSV.exists():
        raise SystemExit(f"missing {TIMELINE_CSV} — run full_market_universe.py timeline")
    return pd.read_csv(TIMELINE_CSV)


def members_for_year(year: int) -> pd.DataFrame:
    """(cik, ticker, ...) rows that are universe members for a given backtest year."""
    m = load_membership()
    return m[m["year"] == int(year)].copy()


# ------------------------------------------------------------------------------------------
# The one leak-free accessor the backtest calls
# ------------------------------------------------------------------------------------------

def joined_asof(cik: int, symbol: str, as_of) -> dict | None:
    """
    The single leak-free snapshot for (cik, symbol) on decision date `as_of`:
        {
          'prices'    : DataFrame of bars with date <= as_of,
          'fund_row'  : latest fundamentals Series filed <= as_of (or None),
          'fund_hist' : DataFrame of all fundamentals filed <= as_of (for growth streaks),
        }
    Returns None if there is no price history as-of the date (can't screen a name with no bars).
    Both legs are strictly point-in-time; nothing dated/filed after `as_of` is ever returned.
    """
    px = prices_asof(symbol, as_of)
    if px is None:
        return None
    hist = fundamentals_asof(cik, as_of)
    return {"prices": px, "fund_row": (hist.iloc[-1] if not hist.empty else None),
            "fund_hist": hist}


# ------------------------------------------------------------------------------------------
# Coverage / self-check
# ------------------------------------------------------------------------------------------

def coverage() -> None:
    """Report join coverage: members with both price files present and fundamentals present."""
    m = load_membership() if MEMBERSHIP_CSV.exists() else pd.DataFrame()
    tl = load_timeline()
    priced = {p.stem.upper() for p in PRICES.glob("*.parquet")}
    n_sym = tl["ticker"].str.upper().nunique()
    n_priced = len({t for t in tl["ticker"].str.upper().unique() if t in priced})
    print("FULL-MARKET JOIN COVERAGE")
    print(f"  timeline symbols          : {n_sym:,}")
    print(f"  ... with price file        : {n_priced:,} ({100*n_priced/max(1,n_sym):.1f}%)")
    if not m.empty:
        yr = (m.groupby("year")["cik"].nunique()
              .rename("members").reset_index())
        print("  membership by year (needs prices on disk to populate):")
        print(yr.to_string(index=False))
    else:
        print("  membership not built yet (run full_market_universe.py membership once prices land)")


if __name__ == "__main__":
    coverage()
