"""
full_market_universe.py — CAN SLIM full-market SELECTION, Phase 1: the UNIVERSE leg.

RE-SCOPED 2026-07-02 per Andrew's locked decisions
--------------------------------------------------
The universe is the **FULL survivorship-free US universe INCLUDING SMALL CAPS** — every
EDGAR-covered company (2010-2026) with clean point-in-time fundamentals, plus delisted /
renamed names recovered via the edgar_resolver. Small caps are the CORE of CAN SLIM, so we
do **NOT** filter to optionable / large-mid-cap. (The optionable large/mid-cap transplant +
options de-risk overlay is a SEPARATE ADDITIVE experiment for later — reported as an add-on,
NEVER used here as a filter or gate.)

This module defines:
  1. the CIK<->ticker<->date timeline that joins survivorship-free prices to the EDGAR
     fundamentals leak-free (point-in-time ticker), and
  2. the point-in-time universe MEMBERSHIP by (CIK, ticker, year), gated ONLY by minimal,
     principled, UNTUNED listing/liquidity criteria (below) — no cap/optionable filter.

FROZEN, MINIMAL, PRINCIPLED LISTING/LIQUIDITY RULE (public, NOT tuned)
---------------------------------------------------------------------
A (CIK, ticker) is a UNIVERSE MEMBER for calendar year Y if, using ONLY data known as-of the
membership date (Y-01-01), ALL of:
    1. HAS clean EDGAR fundamentals whose FILING DATE <= Y-01-01  (it was a live, reporting
       US filer as of the membership date — this is what makes membership point-in-time and
       survivorship-free: a name is a member exactly for the years it was live-and-reporting,
       then drops out when its filings end at delisting).
    2. HAS tradable daily price/volume history ending near Y-01-01 (>= 20 of the trailing
       ~63 trading days present)  (it was actually trading — screens need price bars).
    3. PRICE  >= $1                (a nominal non-defunct floor; NOT a cap or quality gate.
                                    Sub-$1 = a shell / reverse-split candidate a mechanical
                                    breakout screen cannot honestly transact. $1 is the
                                    universal exchange minimum-bid-price rule, not a tuned knob).
These are PUBLIC and chosen for principle (defunct-shell exclusion + tradability), never to
hit a target. There is deliberately NO market-cap floor, NO dollar-volume floor, and NO
optionable filter — small and micro caps stay IN. Changing any of the three is a parameter
change (rule #1) requiring Andrew's blessing.

Exposure/timing note: there is NO external market-timing or cash dial anywhere. Whether the
strategy is in cash or invested must EMERGE downstream from (valid setups passing the screen)
plus (position stops), not from any universe-level signal. This module only defines WHO is
eligible to be scanned each year; it never decides how much to hold.

CANDIDATE SUPERSET to price-source: every CIK with clean EDGAR fundamentals that has a
resolvable trading TICKER (embedded in the fundamentals table by the resolver, or recovered
via the delisted seed). CIKs with fundamentals but NO recoverable symbol are an honest,
counted coverage gap (SEC publishes former NAMES, never former ticker SYMBOLS).

DATA (local warehouse, never on Drive):
    C:/TradingDesk-Local/canslim/edgar/   (fundamentals + identity index — inputs)
    C:/TradingDesk-Local/canslim/prices/  (price/volume warehouse — built by full_market_prices.py)
    C:/TradingDesk-Local/canslim/universe/ (this module's outputs)

Only this CODE lives in the Drive repo.
"""

from __future__ import annotations

import glob
import sys
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent          # ...\TradingDesk\canslim
sys.path.insert(0, str(HERE))
import edgar_resolver as er                      # identity index + delisted seed (reused)

# ---- paths -------------------------------------------------------------------------------
EDGAR = Path(r"C:\TradingDesk-Local\canslim\edgar")
PRICES = Path(r"C:\TradingDesk-Local\canslim\prices")
UNIVERSE = Path(r"C:\TradingDesk-Local\canslim\universe")

QUARTERLY_FULL_DIR = EDGAR / "quarterly_fundamentals_full"      # partitioned (shard=*.parquet)
IDENTITY_PARQUET = EDGAR / "cik_identity.parquet"

TIMELINE_CSV = UNIVERSE / "cik_ticker_timeline.csv"          # CIK<->ticker<->date map (the join key)
CANDIDATES_CSV = UNIVERSE / "candidate_tickers.csv"          # what to pull prices for
MEMBERSHIP_CSV = UNIVERSE / "universe_membership.csv"        # point-in-time membership (CIK/ticker x year)
COUNTS_CSV = UNIVERSE / "universe_counts_by_year.csv"        # membership counts by year

# ---- FROZEN minimal listing/liquidity rule (public, NOT tuned) ---------------------------
# Deliberately NO market-cap floor and NO optionable filter — small caps are the CORE.
MIN_PRICE = 1.0                        # $1 exchange minimum-bid floor (defunct-shell exclusion, not a cap gate)
LIQ_WINDOW = 63                        # ~one trading quarter, trailing, for the as-of price read
MIN_TRADING_DAYS = 20                  # >= 20 of the trailing 63 present == it was actually trading
BACKTEST_YEARS = list(range(2010, 2027))  # 2010..2026 inclusive


# ==========================================================================================
# 1. CIK <-> ticker <-> date timeline (the leak-free join key)
# ==========================================================================================

def build_timeline() -> pd.DataFrame:
    """
    Assemble the CIK<->ticker<->date mapping used to join prices to fundamentals leak-free.

    Sources (all sourced, no invented ticker history):
      * FUNDAMENTALS ticker  — the ticker the EDGAR pipeline's resolver already embedded per
        CIK in quarterly_fundamentals_full (the authoritative as-resolved symbol for a name
        with clean facts). This is the primary key set for the full universe.
      * CURRENT tickers      — from the identity index (submissions `tickers` per CIK), unioned
        in so a still-listed name whose fundamentals row lacks a symbol is still covered.
      * DELISTED/renamed OLD tickers — reverse the resolver's sourced DELISTED_TICKER_TO_NAME
        seed (old_ticker -> name -> CIK) so a delisted CIK gets its historical trading symbol.

    Output columns: cik, ticker, source ('fundamentals'|'current'|'delisted_seed'), entity_name.
    A CIK may appear under more than one symbol (a rename): every symbol is emitted so the
    price puller can source whichever the free vendor knows, and the join keys on CIK.
    """
    print("BUILD CIK<->TICKER TIMELINE (full universe)")
    rows: list[dict] = []

    # (a) tickers embedded in the clean fundamentals table (primary, resolver-authoritative)
    for sp in sorted(QUARTERLY_FULL_DIR.glob("shard=*.parquet")):
        q = pd.read_parquet(sp, columns=["cik", "ticker"]).dropna(subset=["cik", "ticker"])
        for cik, tk in q.drop_duplicates().itertuples(index=False):
            tk = str(tk).strip().upper()
            if tk:
                rows.append({"cik": int(cik), "ticker": tk, "source": "fundamentals",
                             "entity_name": ""})

    # (b) current tickers from the identity index (union-in; fills any fundamentals-row gap)
    if IDENTITY_PARQUET.exists():
        idf = pd.read_parquet(IDENTITY_PARQUET)
        for r in idf.itertuples():
            if not getattr(r, "tickers", ""):
                continue
            for t in str(r.tickers).split(";"):
                t = t.strip().upper()
                if t:
                    rows.append({"cik": int(r.cik), "ticker": t, "source": "current",
                                 "entity_name": getattr(r, "current_name", "")})

    # (c) survivorship-recovered old tickers (delisted seed -> CIK, via the name index)
    name_idx = er.load_name_index()
    try:
        facts_ciks = er.companyfacts_ciks()
    except Exception:
        facts_ciks = None
    for old_ticker, name in er.DELISTED_TICKER_TO_NAME.items():
        hits = er.resolve_by_name(name, name_idx)
        if not hits:
            continue
        h = er._rank_name_hits(hits, facts_ciks)[0]
        rows.append({"cik": int(h["cik"]), "ticker": old_ticker.upper(),
                     "source": "delisted_seed", "entity_name": h["raw_name"]})

    df = pd.DataFrame(rows)
    # prefer the 'fundamentals' source row on a (cik,ticker) dup, then current, then seed
    order = {"fundamentals": 0, "current": 1, "delisted_seed": 2}
    df["__o"] = df["source"].map(order).fillna(9)
    df = (df.sort_values("__o").drop_duplicates(subset=["cik", "ticker"], keep="first")
            .drop(columns="__o"))
    UNIVERSE.mkdir(parents=True, exist_ok=True)
    df.to_csv(TIMELINE_CSV, index=False)
    src = df["source"].value_counts().to_dict()
    print(f"  timeline rows: {len(df):,}  {src}")
    print(f"  distinct CIKs {df.cik.nunique():,}, distinct tickers {df.ticker.nunique():,}")
    print(f"  wrote {TIMELINE_CSV}")
    return df


# ==========================================================================================
# 2. Candidate ticker set (what to pull prices for) — the FULL universe superset
# ==========================================================================================

def build_candidates() -> pd.DataFrame:
    """
    The candidate SUPERSET to price-source: every CIK with clean EDGAR fundamentals AND a
    resolvable trading ticker (fundamentals-embedded, current, or delisted-recovered).

    Honestly counts CIKs with fundamentals but NO recoverable ticker (the survivorship gap
    SEC's data cannot close for free — former ticker SYMBOLS are never published, only NAMES).
    """
    print("BUILD CANDIDATE TICKER SET (full universe)")
    fund_ciks = _fundamental_ciks()
    tl = pd.read_csv(TIMELINE_CSV) if TIMELINE_CSV.exists() else build_timeline()

    have_ticker = tl[tl["cik"].isin(fund_ciks)].copy()
    cand = have_ticker.drop_duplicates(subset=["ticker"]).copy()  # one price pull per symbol

    n_fund = len(fund_ciks)
    n_fund_with_ticker = have_ticker["cik"].nunique()
    n_gap = n_fund - n_fund_with_ticker

    cand = cand.sort_values("ticker")
    cand.to_csv(CANDIDATES_CSV, index=False)
    print(f"  CIKs with clean fundamentals      : {n_fund:,}")
    print(f"  ... with a resolvable ticker      : {n_fund_with_ticker:,}")
    print(f"  ... NO recoverable ticker (gap)   : {n_gap:,} "
          f"(delisted shells; SEC gives no former symbols)")
    print(f"  candidate SYMBOLS to price-pull   : {len(cand):,}")
    print(f"  wrote {CANDIDATES_CSV}")
    return cand


def _fundamental_ciks() -> set[int]:
    """CIKs that produced >=1 clean quarterly fundamentals row (the full universe pool)."""
    ciks: set[int] = set()
    for sp in sorted(QUARTERLY_FULL_DIR.glob("shard=*.parquet")):
        q = pd.read_parquet(sp, columns=["cik"])
        ciks.update(int(c) for c in q["cik"].dropna().unique())
    return ciks


# ==========================================================================================
# 3. Point-in-time universe membership (minimal listing/liquidity — NO cap/optionable filter)
# ==========================================================================================

def _load_first_filed_by_cik() -> dict[int, pd.Timestamp]:
    """
    Per CIK, the sorted list of (filed_date) is expensive; we only need, for each year, the
    earliest filing date. Precompute per-CIK the MIN filed date so membership rule #1
    ('had a filing filed <= Y-01-01') is a cheap comparison. Returns cik -> earliest filed.
    """
    firsts: dict[int, pd.Timestamp] = {}
    for sp in sorted(QUARTERLY_FULL_DIR.glob("shard=*.parquet")):
        q = pd.read_parquet(sp, columns=["cik", "filed"])
        q["filed"] = pd.to_datetime(q["filed"], errors="coerce")
        g = q.dropna(subset=["filed"]).groupby("cik")["filed"].min()
        for cik, f in g.items():
            cik = int(cik)
            if cik not in firsts or f < firsts[cik]:
                firsts[cik] = f
    return firsts


def _load_prices(ticker: str) -> pd.DataFrame | None:
    """Load one ticker's daily price/volume parquet from the warehouse, or None if absent."""
    p = PRICES / f"{ticker}.parquet"
    if not p.exists():
        return None
    df = pd.read_parquet(p)
    if df.empty:
        return None
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values("date")


def build_membership() -> pd.DataFrame:
    """
    Apply the FROZEN minimal listing/liquidity rule to define membership by (CIK, ticker, year).

    For each candidate ticker and each backtest year Y:
      as_of = Y-01-01
      MEMBER iff:
        (1) earliest EDGAR filing for this CIK was filed <= as_of  (live/reporting by then),
        (2) >= MIN_TRADING_DAYS of the trailing LIQ_WINDOW trading days present ending <= as_of,
        (3) median close over that window >= MIN_PRICE.
      NO market-cap, NO dollar-volume, NO optionable gate.

    Requires the price warehouse to be populated (full_market_prices.py). Names with no price
    file yet are skipped and COUNTED as pending (resumable: rerun as more prices land).
    """
    print("BUILD POINT-IN-TIME UNIVERSE MEMBERSHIP (full universe, no cap/optionable filter)")
    if not CANDIDATES_CSV.exists():
        build_candidates()
    cand = pd.read_csv(CANDIDATES_CSV)
    first_filed = _load_first_filed_by_cik()

    rows: list[dict] = []
    n_priced = 0
    n_missing_price = 0
    for r in cand.itertuples():
        ticker = str(r.ticker)
        cik = int(r.cik)
        px = _load_prices(ticker)
        if px is None:
            n_missing_price += 1
            continue
        n_priced += 1
        px = px.set_index("date")
        cik_first = first_filed.get(cik)
        for year in BACKTEST_YEARS:
            as_of = pd.Timestamp(year=year, month=1, day=1)
            if cik_first is None or cik_first > as_of:
                continue  # rule (1): not yet a reporting filer as of the membership date
            window = px[px.index <= as_of].tail(LIQ_WINDOW)
            if len(window) < MIN_TRADING_DAYS:
                continue  # rule (2): not actually trading with enough history
            price = float(window["close"].median())
            if price < MIN_PRICE:
                continue  # rule (3): defunct-shell floor
            dvol = (float((window["close"] * window["volume"]).median())
                    if "volume" in window.columns else 0.0)
            rows.append({"cik": cik, "ticker": ticker, "year": year,
                         "price": round(price, 2), "avg_dollar_vol": int(dvol)})

    mem = pd.DataFrame(rows)
    UNIVERSE.mkdir(parents=True, exist_ok=True)
    mem.to_csv(MEMBERSHIP_CSV, index=False)

    if mem.empty:
        counts = pd.DataFrame(columns=["year", "n_members"])
    else:
        counts = (mem.groupby("year")
                  .agg(n_members=("cik", "nunique"),
                       median_price=("price", "median"),
                       median_dvol=("avg_dollar_vol", "median")).reset_index())
    counts.to_csv(COUNTS_CSV, index=False)

    print(f"  candidate symbols            : {len(cand):,}")
    print(f"  ... with prices on disk      : {n_priced:,}")
    print(f"  ... awaiting price pull      : {n_missing_price:,} (resumable — rerun later)")
    print(f"  membership rows (CIK x year) : {len(mem):,}")
    if not counts.empty:
        print("\n  MEMBERSHIP BY YEAR:")
        print(counts.to_string(index=False))
    print(f"\n  wrote {MEMBERSHIP_CSV} and {COUNTS_CSV}")
    return mem


# ==========================================================================================
# CLI
# ==========================================================================================

def main() -> None:
    stage = sys.argv[1] if len(sys.argv) > 1 else "all"
    if stage in ("timeline", "all"):
        build_timeline()
    if stage in ("candidates", "all"):
        build_candidates()
    if stage in ("membership", "all"):
        build_membership()


if __name__ == "__main__":
    main()
