"""
EDGAR point-in-time fundamentals pipeline — CAN SLIM, Phase 1.

Owned, in-house, purpose-built. Turns SEC's raw XBRL company-facts into a clean
QUARTERLY, POINT-IN-TIME fundamentals table for the advisor's ~800 watch-list names.

Design contract (desk causality discipline — rule #1, no lookahead):
  * Every figure carries its SEC FILING DATE (`filed`).
  * The as-of layer answers "what was known as of date D" — on any historical date,
    only facts actually filed by then are visible.
  * When a period is later restated, an as-of-then query returns the value as ORIGINALLY
    FILED (first filing whose `filed` <= D), never the later restatement. No leakage.

We deliberately DO NOT depend on edgartools/other frameworks: we ingest SEC's bulk
`companyfacts.zip` and parse the raw XBRL JSON ourselves, so the canonical-concept mapping,
the YTD->quarterly differencing, and the as-of logic are all OWNED and readable here.

DATA (never on Drive — corrupts on sync): everything lives under the LOCAL warehouse
  C:/TradingDesk-Local/canslim/edgar/
Only this CODE lives in the Drive repo.

Stages (run with --stage or run all):
  ingest   : download companyfacts.zip (~1.4GB) + CIK<->ticker map into the warehouse
  build    : resolve watch-list tickers -> CIK, parse each company's facts, canonicalize
             concepts, difference YTD->discrete quarters, attach filing dates, write the
             raw point-in-time fact store (parquet).
  table    : from the PIT fact store, emit the clean quarterly fundamentals table with
             derived quarterly & YoY sales growth, EPS growth, ROE, margins (as-first-filed).
  validate : coverage counts, spot-checks, and the concrete as-of / no-lookahead demo.

Usage:
  python edgar_pipeline.py ingest
  python edgar_pipeline.py build
  python edgar_pipeline.py table
  python edgar_pipeline.py validate
  python edgar_pipeline.py all
"""

from __future__ import annotations

import io
import json
import sys
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

# --------------------------------------------------------------------------------------
# Paths & fair-access config
# --------------------------------------------------------------------------------------

# CODE root (Drive). DATA root (local warehouse — never synced).
REPO_ROOT = Path(__file__).resolve().parent            # ...\TradingDesk\canslim
WATCHLIST = REPO_ROOT / "research" / "watchlist_tickers.txt"
RESEARCH_DIR = REPO_ROOT / "research"

WAREHOUSE = Path(r"C:\TradingDesk-Local\canslim\edgar")
COMPANYFACTS_ZIP = WAREHOUSE / "companyfacts.zip"
TICKER_MAP_JSON = WAREHOUSE / "company_tickers.json"

# Parsed artifacts (all in the local warehouse)
PIT_FACTS_PARQUET = WAREHOUSE / "pit_facts.parquet"        # raw point-in-time facts (long)
QUARTERLY_PARQUET = WAREHOUSE / "quarterly_fundamentals.parquet"
QUARTERLY_CSV = WAREHOUSE / "quarterly_fundamentals.csv"
UNRESOLVED_CSV = WAREHOUSE / "unresolved_concepts.csv"     # tags we couldn't canonicalize
COVERAGE_CSV = WAREHOUSE / "phase1_coverage.csv"           # per-ticker resolution status

# SEC fair-access: descriptive User-Agent (or 403) and <=10 req/sec. companyfacts.zip is a
# single bulk grab, so we're well under the cap; the header is the binding requirement.
USER_AGENT = "Surber HC Trading Research andrew@surberhc.com"
HEADERS = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"}

# Canonical SEC bulk path is the /xbrl/ one; the /bulkdata/ path 403s (verified 2026-07-01).
COMPANYFACTS_URL = "https://www.sec.gov/Archives/edgar/daily-index/xbrl/companyfacts.zip"
TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"

# --------------------------------------------------------------------------------------
# Canonical concept mapping — the fiddly core.
# Each canonical field maps to a PRIORITY-ORDERED list of us-gaap tag variants. For a given
# (company, period, filing) we take the first variant that resolves. We log any income/
# balance concept we skipped so the messy tail is measured, not hidden.
# --------------------------------------------------------------------------------------

# Ordered by preference (most specific / most modern first where it matters).
CANONICAL_CONCEPTS: dict[str, list[str]] = {
    # Revenue — post-ASC606 tag first, then the classic variants, then broad fallbacks.
    "revenue": [
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
        "SalesRevenueGoodsNet",
        "SalesRevenueServicesNet",
        "RevenuesNetOfInterestExpense",
    ],
    # Net income attributable to the company.
    "net_income": [
        "NetIncomeLoss",
        "ProfitLoss",
        "NetIncomeLossAvailableToCommonStockholdersBasic",
    ],
    # Diluted EPS preferred (CAN SLIM convention), basic as fallback.
    "eps_diluted": [
        "EarningsPerShareDiluted",
        "EarningsPerShareBasicAndDiluted",
    ],
    "eps_basic": [
        "EarningsPerShareBasic",
        "EarningsPerShareBasicAndDiluted",
    ],
    # Shares (diluted weighted-average preferred).
    "shares_diluted": [
        "WeightedAverageNumberOfDilutedSharesOutstanding",
        "WeightedAverageNumberOfSharesOutstandingBasicAndDiluted",
    ],
    # Stockholders' equity — instant (balance sheet). Prefer parent-only, then incl. NCI.
    "equity": [
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
    ],
    "assets": ["Assets"],
    "gross_profit": ["GrossProfit"],
    "operating_income": ["OperatingIncomeLoss"],
    "cost_of_revenue": [
        "CostOfRevenue",
        "CostOfGoodsAndServicesSold",
        "CostOfGoodsSold",
    ],
}

# Flow concepts are duration facts (revenue, income...) that need YTD->quarterly differencing.
# Instant concepts (equity, assets, shares snapshot) are balance-sheet points — no differencing.
FLOW_CONCEPTS = {
    "revenue", "net_income", "gross_profit", "operating_income", "cost_of_revenue",
}
INSTANT_CONCEPTS = {"equity", "assets"}
# EPS and share counts are per-period reported values (not summed the same way); handled below.
PER_SHARE_CONCEPTS = {"eps_diluted", "eps_basic"}
SHARE_CONCEPTS = {"shares_diluted"}

# Which raw us-gaap tags we consider "financial statement income/balance" for the purpose of
# measuring the unresolved tail (concepts a filer used that we didn't map). Kept broad but
# focused on the statements we care about.
_ALL_MAPPED_TAGS = {t for variants in CANONICAL_CONCEPTS.values() for t in variants}


# --------------------------------------------------------------------------------------
# Stage 1: INGEST
# --------------------------------------------------------------------------------------

def _download(url: str, dest: Path, *, min_bytes: int = 1) -> None:
    """Stream a URL to disk with the SEC User-Agent. Skips if already present & non-trivial."""
    import requests

    if dest.exists() and dest.stat().st_size >= min_bytes:
        print(f"  [skip] {dest.name} already present ({dest.stat().st_size/1e6:.1f} MB)")
        return
    print(f"  [get ] {url}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    with requests.get(url, headers=HEADERS, stream=True, timeout=120) as r:
        r.raise_for_status()
        total = int(r.headers.get("Content-Length", 0))
        got = 0
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 20):  # 1 MB
                f.write(chunk)
                got += len(chunk)
                if total and got % (50 << 20) < (1 << 20):  # ~every 50 MB
                    print(f"       {got/1e6:7.1f} / {total/1e6:7.1f} MB", flush=True)
    print(f"  [done] {dest.name} {dest.stat().st_size/1e6:.1f} MB in {time.time()-t0:.0f}s")


def ingest() -> None:
    """Download companyfacts.zip + the CIK<->ticker map into the warehouse."""
    print("INGEST -> " + str(WAREHOUSE))
    WAREHOUSE.mkdir(parents=True, exist_ok=True)
    _download(TICKER_MAP_URL, TICKER_MAP_JSON, min_bytes=100_000)
    _download(COMPANYFACTS_URL, COMPANYFACTS_ZIP, min_bytes=500_000_000)
    # Sanity: is it a real zip?
    with zipfile.ZipFile(COMPANYFACTS_ZIP) as zf:
        n = len(zf.namelist())
    print(f"  companyfacts.zip OK — {n:,} company JSON entries")


# --------------------------------------------------------------------------------------
# Ticker -> CIK resolution
# --------------------------------------------------------------------------------------

# Manual rename map for the hard tail (old watch-list ticker -> current ticker in SEC map).
# From the coverage research; only the ones that are pure renames of live filers.
RENAME_MAP = {
    "AAXN": "AXON", "BLL": "BALL", "CDAY": "DAY", "ELY": "MODG", "GPS": "GAP",
    "GSX": "GOTU", "JCOM": "ZD", "JEC": "J", "PKI": "RVTY", "SQ": "XYZ",
    "FB": "META", "XLNX": None, "TWTR": None,  # None = delisted, resolve by name below
    "ATVI": None, "BRKS": "AZTA", "ABMD": None, "CTXS": None,
}


def load_watchlist() -> list[str]:
    tickers = []
    for line in WATCHLIST.read_text().splitlines():
        t = line.strip().upper()
        if t and not t.startswith("#"):
            tickers.append(t)
    # unique, preserve order
    seen = set()
    out = []
    for t in tickers:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def load_ticker_cik_map() -> dict[str, int]:
    """ticker (upper) -> CIK int, from SEC's current company_tickers.json."""
    raw = json.loads(TICKER_MAP_JSON.read_text())
    m = {}
    for row in raw.values():
        m[str(row["ticker"]).upper()] = int(row["cik_str"])
    return m


def resolve_ciks(tickers: list[str], tmap: dict[str, int]) -> tuple[dict[str, int], list[dict]]:
    """
    Resolve each watch-list ticker to a CIK. Returns (resolved {ticker->cik}, coverage_rows).
    coverage_rows records status + reason for EVERY ticker (resolved or not) for honest counts.
    """
    resolved: dict[str, int] = {}
    rows: list[dict] = []
    ETFS = {"ARKK", "IBIT", "IWM", "RSP", "SIL", "SMH", "TQQQ", "UFO", "XLV"}
    for t in tickers:
        if t in ETFS:
            rows.append({"ticker": t, "cik": None, "status": "skip_etf",
                         "reason": "ETF/index — no company fundamentals"})
            continue
        cik = tmap.get(t)
        if cik is not None:
            resolved[t] = cik
            rows.append({"ticker": t, "cik": cik, "status": "direct", "reason": ""})
            continue
        # try rename map
        if t in RENAME_MAP:
            alt = RENAME_MAP[t]
            if alt and alt in tmap:
                resolved[t] = tmap[alt]
                rows.append({"ticker": t, "cik": tmap[alt], "status": "renamed",
                             "reason": f"renamed -> {alt}"})
                continue
            rows.append({"ticker": t, "cik": None, "status": "unresolved",
                         "reason": "delisted/renamed, not in current SEC map"})
            continue
        rows.append({"ticker": t, "cik": None, "status": "unresolved",
                     "reason": "not in current SEC map (renamed/delisted/foreign)"})
    return resolved, rows


# --------------------------------------------------------------------------------------
# Stage 2: BUILD — parse each company's facts into a point-in-time fact store
# --------------------------------------------------------------------------------------

@dataclass
class Fact:
    ticker: str
    cik: int
    concept: str          # canonical field name
    tag: str              # the actual us-gaap tag that resolved it
    period_end: str       # 'end' (YYYY-MM-DD)
    period_start: str | None  # 'start' for duration facts, None for instants
    fy: int | None
    fp: str | None        # 'Q1'..'Q4','FY'
    form: str             # 10-Q / 10-K
    filed: str            # FILING DATE — the point-in-time key
    accn: str
    value: float
    qtrs: int             # 0 instant, 1 quarter, 2/3 partial YTD, 4 annual (derived)
    priority: int         # tag's rank within its concept's variant list (0 = most preferred)


def _cik_json_from_zip(zf: zipfile.ZipFile, cik: int) -> dict | None:
    """companyfacts.zip stores one file per company: CIK##########.json (10-digit)."""
    name = f"CIK{cik:010d}.json"
    try:
        with zf.open(name) as fh:
            return json.load(fh)
    except KeyError:
        return None


def _months_between(start: str, end: str) -> int:
    s = pd.Timestamp(start)
    e = pd.Timestamp(end)
    return round((e - s).days / 30.44)


def _extract_company_facts(ticker: str, cik: int, doc: dict) -> tuple[list[Fact], set[str]]:
    """
    Pull every (concept, period, filing) fact for the canonical tags from one company's JSON.
    Returns (facts, used_tags). used_tags = the raw us-gaap tags this filer actually used
    (for measuring the unresolved tail).
    """
    facts: list[Fact] = []
    gaap = (doc.get("facts") or {}).get("us-gaap") or {}
    used_tags = set(gaap.keys())

    for concept, variants in CANONICAL_CONCEPTS.items():
        is_flow = concept in FLOW_CONCEPTS
        for rank, tag in enumerate(variants):
            if tag not in gaap:
                continue
            units = gaap[tag].get("units") or {}
            for unit, entries in units.items():
                # revenue/income in USD; EPS in USD/shares; shares are a count.
                for e in entries:
                    val = e.get("val")
                    end = e.get("end")
                    filed = e.get("filed")
                    form = e.get("form", "")
                    if val is None or end is None or filed is None:
                        continue
                    # Only take 10-K/10-Q filings for statement figures.
                    if form not in ("10-K", "10-Q", "10-K/A", "10-Q/A"):
                        continue
                    start = e.get("start")
                    if is_flow and start is None:
                        continue  # flow facts must have a duration
                    # Derive qtrs bucket from the duration (instants have no start).
                    if start is None:
                        qtrs = 0
                    else:
                        m = _months_between(start, end)
                        qtrs = {3: 1, 6: 2, 9: 3, 12: 4}.get(m, -1)
                        if qtrs == -1:
                            continue  # odd (52/53-wk noise, transition periods) — skip
                    facts.append(Fact(
                        ticker=ticker, cik=cik, concept=concept, tag=tag,
                        period_end=end, period_start=start,
                        fy=e.get("fy"), fp=e.get("fp"), form=form,
                        filed=filed, accn=e.get("accn", ""), value=float(val),
                        qtrs=qtrs, priority=rank,
                    ))
            # NOTE: we do NOT break after the first variant. A single filer often switches
            # revenue tags across eras (e.g. SalesRevenueNet pre-2018 -> RevenueFrom-
            # ContractWithCustomer... post-ASC606). We keep ALL variants and resolve the
            # winner PER PERIOD by `priority` downstream (_as_first_filed), which stitches the
            # eras together without double-counting a single period.
    return facts, used_tags


def build() -> None:
    """Parse watch-list companies -> raw point-in-time fact store (parquet)."""
    print("BUILD")
    tickers = load_watchlist()
    tmap = load_ticker_cik_map()
    resolved, coverage_rows = resolve_ciks(tickers, tmap)
    print(f"  watch-list: {len(tickers)} tickers; resolved to CIK: {len(resolved)}")

    all_facts: list[Fact] = []
    # measure the unresolved tail: raw statement tags a filer used that we didn't map.
    tail_counter: dict[str, int] = {}
    parsed_ok = 0
    empty = []

    with zipfile.ZipFile(COMPANYFACTS_ZIP) as zf:
        zip_names = set(zf.namelist())
        for i, (ticker, cik) in enumerate(sorted(resolved.items())):
            name = f"CIK{cik:010d}.json"
            if name not in zip_names:
                # CIK exists in map but no facts file (rare) — note it.
                for row in coverage_rows:
                    if row["ticker"] == ticker:
                        row["status"] = "no_facts_file"
                        row["reason"] = "CIK in map but absent from companyfacts.zip"
                continue
            doc = _cik_json_from_zip(zf, cik)
            if doc is None:
                continue
            facts, used_tags = _extract_company_facts(ticker, cik, doc)
            if not facts:
                empty.append(ticker)
                for row in coverage_rows:
                    if row["ticker"] == ticker:
                        row["status"] = "no_canonical_facts"
                        row["reason"] = "no mapped concept resolved (custom tags / sparse)"
            else:
                parsed_ok += 1
                all_facts.extend(facts)
            # tally revenue-ish tags used but unmapped, to expose the messy tail
            for tg in used_tags:
                if (("Revenue" in tg or "Sales" in tg or "IncomeLoss" in tg
                     or "EarningsPerShare" in tg) and tg not in _ALL_MAPPED_TAGS):
                    tail_counter[tg] = tail_counter.get(tg, 0) + 1
            if (i + 1) % 100 == 0:
                print(f"    parsed {i+1}/{len(resolved)} companies...", flush=True)

    print(f"  companies with >=1 canonical fact: {parsed_ok}; empty: {len(empty)}")
    print(f"  total raw facts: {len(all_facts):,}")

    # Write the point-in-time fact store (long form — every filing of every period).
    df = pd.DataFrame([f.__dict__ for f in all_facts])
    df.to_parquet(PIT_FACTS_PARQUET, index=False)
    print(f"  wrote {PIT_FACTS_PARQUET}")

    # Coverage + unresolved-tail logs.
    pd.DataFrame(coverage_rows).to_csv(COVERAGE_CSV, index=False)
    tail = (pd.DataFrame(sorted(tail_counter.items(), key=lambda kv: -kv[1]),
                         columns=["unmapped_tag", "num_companies"])
            if tail_counter else pd.DataFrame(columns=["unmapped_tag", "num_companies"]))
    tail.to_csv(UNRESOLVED_CSV, index=False)
    print(f"  wrote {COVERAGE_CSV} and {UNRESOLVED_CSV}")


# --------------------------------------------------------------------------------------
# YTD -> discrete quarter differencing + the as-of layer
# --------------------------------------------------------------------------------------

def _as_first_filed(facts: pd.DataFrame) -> pd.DataFrame:
    """
    Collapse to one row per (ticker, concept, period_end, qtrs) using two rules, in order:
      1. TAG PRIORITY per period — when a filer used multiple tag variants for the same
         period (e.g. it re-disclosed a period under both SalesRevenueNet and the newer
         RevenueFromContractWithCustomer... tag), keep the highest-priority variant
         (`priority` ascending). This stitches multi-era tag switches into one clean series.
      2. AS-FIRST-FILED — among the chosen variant's filings, take the earliest `filed`.
         Restatements filed later stay in the raw store but never overwrite the original,
         preserving point-in-time integrity. (Full as-of query lives in asof_quarterly().)
    """
    facts = facts.sort_values(["priority", "filed"])
    keys = ["ticker", "concept", "period_end", "qtrs"]
    return facts.groupby(keys, as_index=False).first()


def _difference_ytd_to_quarters(flow: pd.DataFrame) -> pd.DataFrame:
    """
    Recover discrete quarters from cumulative YTD flow figures.
      Q1 = qtrs==1 as-is.
      Q(k) discrete = YTD(k) - YTD(k-1), matched within the same fiscal year.
    We identify the fiscal year by grouping on (ticker, concept, fy). Q4 = FY(12mo) - 9mo YTD.
    Every derived quarter inherits the FILING DATE of the YTD figure it was derived FROM
    (the later of the two filings when subtracting), so point-in-time integrity holds:
    the discrete quarter wasn't "known" until the YTD that reveals it was filed.
    """
    out_rows = []
    flow = flow.copy()
    flow["end_ts"] = pd.to_datetime(flow["period_end"])
    for (ticker, concept, fy), g in flow.groupby(["ticker", "concept", "fy"], dropna=True):
        g = g.sort_values(["qtrs", "end_ts"]).drop_duplicates(subset=["qtrs"], keep="first")
        by_qtrs = {int(r.qtrs): r for r in g.itertuples()}
        # Build cumulative ladder present for this fiscal year.
        prev_ytd_val = 0.0
        prev_qtrs = 0
        for k in (1, 2, 3, 4):
            if k not in by_qtrs:
                # gap — reset ladder so we don't subtract across a hole
                if k - 1 in by_qtrs:
                    prev_ytd_val = by_qtrs[k - 1].value
                    prev_qtrs = k - 1
                continue
            r = by_qtrs[k]
            if k == 1:
                disc = r.value
                filed = r.filed
            else:
                if prev_qtrs == k - 1:
                    disc = r.value - prev_ytd_val
                    # discrete quarter is only known once the YTD(k) was filed
                    filed = r.filed
                else:
                    # missing an intermediate YTD — can't cleanly difference; skip
                    prev_ytd_val = r.value
                    prev_qtrs = k
                    continue
            out_rows.append({
                "ticker": ticker, "concept": concept, "fy": int(fy),
                "fq": k,  # fiscal quarter number
                "period_end": r.period_end, "value": disc, "filed": filed,
                "form": r.form, "accn": r.accn,
            })
            prev_ytd_val = r.value
            prev_qtrs = k
    return pd.DataFrame(out_rows)


def table() -> None:
    """
    From the raw PIT fact store, emit the clean AS-FIRST-FILED quarterly fundamentals table
    with derived quarterly & YoY growth, ROE, and margins.
    """
    print("TABLE")
    facts = pd.read_parquet(PIT_FACTS_PARQUET)

    # As-first-filed originals (point-in-time canonical).
    orig = _as_first_filed(facts)

    # --- Flows -> discrete quarters ---
    flows = orig[orig["concept"].isin(FLOW_CONCEPTS)].copy()
    disc = _difference_ytd_to_quarters(flows)  # ticker, concept, fy, fq, period_end, value, filed...

    # Pivot flows to wide: one row per (ticker, fy, fq) with each flow concept as a column.
    if not disc.empty:
        flow_wide = disc.pivot_table(
            index=["ticker", "fy", "fq", "period_end"],
            columns="concept", values="value", aggfunc="first").reset_index()
        # filing date per (ticker,fy,fq) = the latest filed among its flow components
        filed_map = (disc.groupby(["ticker", "fy", "fq"])["filed"].max().reset_index()
                     .rename(columns={"filed": "filed"}))
        flow_wide = flow_wide.merge(filed_map, on=["ticker", "fy", "fq"], how="left")
    else:
        flow_wide = pd.DataFrame()

    # --- EPS: prefer as-reported discrete-quarter EPS if the filer gives a 3-month EPS;
    #     otherwise leave null (we do NOT synthesize EPS by dividing, to stay as-reported). ---
    eps = orig[(orig["concept"] == "eps_diluted") & (orig["qtrs"] == 1)].copy()
    eps_q = eps.rename(columns={"value": "eps_diluted"})[
        ["ticker", "fy", "fp", "period_end", "eps_diluted", "filed"]]
    eps_q["fq"] = eps_q["fp"].map({"Q1": 1, "Q2": 2, "Q3": 3, "Q4": 4})
    # for annual filings fp='FY' -> that's the 4-quarter EPS, not a discrete Q; drop here.
    eps_q = eps_q.dropna(subset=["fq"])
    eps_q["fq"] = eps_q["fq"].astype(int)

    # --- Instants (equity) — snapshot at period end, as-first-filed. ---
    inst = orig[orig["concept"].isin(INSTANT_CONCEPTS) & (orig["qtrs"] == 0)].copy()
    inst_wide = inst.pivot_table(index=["ticker", "period_end"],
                                 columns="concept", values="value",
                                 aggfunc="first").reset_index()

    if flow_wide.empty:
        print("  no flow data — aborting table build")
        return

    tbl = flow_wide.copy()
    # attach EPS by (ticker, fy, fq)
    if not eps_q.empty:
        tbl = tbl.merge(eps_q[["ticker", "fy", "fq", "eps_diluted"]],
                        on=["ticker", "fy", "fq"], how="left")
    # attach equity by period_end (balance sheet at quarter close)
    if not inst_wide.empty:
        tbl = tbl.merge(inst_wide, on=["ticker", "period_end"], how="left")

    tbl["period_end_ts"] = pd.to_datetime(tbl["period_end"])
    tbl = tbl.sort_values(["ticker", "period_end_ts"]).reset_index(drop=True)

    # --- Derived metrics ---
    def col(name):
        return tbl[name] if name in tbl.columns else pd.Series([pd.NA] * len(tbl))

    tbl["revenue"] = col("revenue")
    tbl["net_income"] = col("net_income")
    tbl["gross_profit"] = col("gross_profit")
    tbl["operating_income"] = col("operating_income")

    # margins
    tbl["gross_margin"] = pd.to_numeric(tbl["gross_profit"], errors="coerce") / \
        pd.to_numeric(tbl["revenue"], errors="coerce")
    tbl["net_margin"] = pd.to_numeric(tbl["net_income"], errors="coerce") / \
        pd.to_numeric(tbl["revenue"], errors="coerce")
    tbl["operating_margin"] = pd.to_numeric(tbl["operating_income"], errors="coerce") / \
        pd.to_numeric(tbl["revenue"], errors="coerce")

    # ROE (quarterly net income annualized crudely x4 over equity) — flag: point-in-time equity
    if "equity" in tbl.columns:
        tbl["roe_q"] = pd.to_numeric(tbl["net_income"], errors="coerce") / \
            pd.to_numeric(tbl["equity"], errors="coerce")
        tbl["roe_ttm_annualized"] = tbl["roe_q"] * 4
    else:
        tbl["roe_q"] = pd.NA
        tbl["roe_ttm_annualized"] = pd.NA

    # YoY growth: match same fiscal quarter one year prior (fy-1, same fq).
    tbl = _add_yoy(tbl, "revenue", "sales_growth_yoy")
    tbl = _add_yoy(tbl, "eps_diluted", "eps_growth_yoy")

    # sequential quarterly growth
    tbl["sales_growth_qoq"] = tbl.groupby("ticker")["revenue"].pct_change(fill_method=None)

    cols = ["ticker", "fy", "fq", "period_end", "filed", "revenue", "net_income",
            "eps_diluted", "gross_profit", "operating_income", "equity",
            "gross_margin", "operating_margin", "net_margin", "roe_q", "roe_ttm_annualized",
            "sales_growth_yoy", "sales_growth_qoq", "eps_growth_yoy"]
    cols = [c for c in cols if c in tbl.columns]
    out = tbl[cols].copy()
    out.to_parquet(QUARTERLY_PARQUET, index=False)
    out.to_csv(QUARTERLY_CSV, index=False)
    print(f"  wrote {QUARTERLY_PARQUET} ({len(out):,} rows, "
          f"{out['ticker'].nunique()} tickers)")
    print(f"  wrote {QUARTERLY_CSV}")


def _add_yoy(tbl: pd.DataFrame, value_col: str, out_col: str) -> pd.DataFrame:
    if value_col not in tbl.columns:
        tbl[out_col] = pd.NA
        return tbl
    prior = tbl[["ticker", "fy", "fq", value_col]].copy()
    prior["fy"] = prior["fy"] + 1
    prior = prior.rename(columns={value_col: value_col + "_prior"})
    tbl = tbl.merge(prior, on=["ticker", "fy", "fq"], how="left")
    cur = pd.to_numeric(tbl[value_col], errors="coerce")
    pri = pd.to_numeric(tbl[value_col + "_prior"], errors="coerce")
    tbl[out_col] = (cur - pri) / pri.abs()
    tbl = tbl.drop(columns=[value_col + "_prior"])
    return tbl


# --------------------------------------------------------------------------------------
# THE AS-OF LAYER — point-in-time query. This is the whole point.
# --------------------------------------------------------------------------------------

def asof_quarterly(facts: pd.DataFrame, ticker: str, concept: str, as_of: str) -> pd.DataFrame:
    """
    Return, for one company+concept, the quarterly facts VISIBLE as of date `as_of`:
      * only facts whose FILING DATE (`filed`) <= as_of,
      * and for each period, the value AS-FIRST-FILED among those visible (earliest filed).

    Consequence (no lookahead): a period restated later returns its ORIGINAL value for an
    as-of-then query, because the restatement's `filed` is after `as_of` and is excluded;
    even if the restatement were before as_of, we take the earliest-filed of the visible set,
    which is still the original. Nothing filed after `as_of` can ever influence the answer.
    """
    as_of_ts = pd.Timestamp(as_of)
    sub = facts[(facts["ticker"] == ticker) & (facts["concept"] == concept)].copy()
    sub = sub[pd.to_datetime(sub["filed"]) <= as_of_ts]
    if sub.empty:
        return sub
    # Within the visible-as-of set: highest-priority tag per period, then earliest filed.
    sort_cols = ["priority", "filed"] if "priority" in sub.columns else ["filed"]
    sub = sub.sort_values(sort_cols)
    out = sub.groupby(["period_end", "qtrs"], as_index=False).first()
    return out.sort_values("period_end")


# --------------------------------------------------------------------------------------
# Stage 4: VALIDATE
# --------------------------------------------------------------------------------------

def validate() -> None:
    print("VALIDATE")
    cov = pd.read_csv(COVERAGE_CSV)
    facts = pd.read_parquet(PIT_FACTS_PARQUET)
    quarterly = pd.read_parquet(QUARTERLY_PARQUET)

    n_total = len(cov)
    n_etf = (cov["status"] == "skip_etf").sum()
    n_resolved_cik = cov["cik"].notna().sum()
    n_with_facts = quarterly["ticker"].nunique()
    print(f"\n== COVERAGE ==")
    print(f"  watch-list tickers        : {n_total}")
    print(f"  ETFs/indices (excluded)   : {n_etf}")
    print(f"  resolved to a CIK         : {n_resolved_cik}")
    print(f"  produced quarterly table  : {n_with_facts}")
    print(f"\n  status breakdown:")
    print(cov["status"].value_counts().to_string())

    # --- Spot-checks ---
    print(f"\n== SPOT-CHECKS (discrete-quarter revenue & EPS, as-first-filed) ==")
    for tk in ["AAPL", "AAON", "ADMA"]:
        sub = quarterly[quarterly["ticker"] == tk].sort_values(["fy", "fq"]).tail(6)
        if sub.empty:
            print(f"  {tk}: (no rows)")
            continue
        print(f"\n  {tk} — last 6 discrete quarters:")
        show = sub[["fy", "fq", "period_end", "revenue", "eps_diluted",
                    "sales_growth_yoy", "eps_growth_yoy"]]
        with pd.option_context("display.width", 160, "display.max_columns", 20):
            print(show.to_string(index=False))

    # --- As-of / no-lookahead demonstration ---
    print(f"\n== AS-OF / NO-LOOKAHEAD DEMO ==")
    _demo_asof(facts)


def _demo_asof(facts: pd.DataFrame) -> None:
    """
    Concrete proof: pick a name, show that an as-of query returns only pre-filing data, and
    that a restated period returns the ORIGINAL as-first-filed value for an as-of-then query.
    """
    # Pick a name with a known restatement pattern: find a (ticker, period_end, qtrs) that was
    # filed more than once with DIFFERENT values (a genuine restatement in our store).
    rev = facts[facts["concept"] == "revenue"].copy()
    grp = rev.groupby(["ticker", "period_end", "qtrs"])
    restated = []
    for key, g in grp:
        if g["value"].nunique() > 1 and len(g) > 1:
            g2 = g.sort_values("filed")
            restated.append((key, g2))
    if restated:
        (tk, pend, q), g2 = restated[0]
        first = g2.iloc[0]
        later = g2.iloc[-1]
        print(f"  Restatement found: {tk} revenue period_end={pend} (qtrs={q})")
        print(f"    first  filed {first['filed']}: value={first['value']:,.0f}")
        print(f"    later  filed {later['filed']}: value={later['value']:,.0f}")
        # as-of the day AFTER the first filing but BEFORE the restatement:
        between = (pd.Timestamp(first["filed"]) + pd.Timedelta(days=1)).date().isoformat()
        got = asof_quarterly(facts, tk, "revenue", between)
        row = got[got["period_end"] == pend]
        if not row.empty:
            v = row.iloc[0]["value"]
            print(f"    as-of {between}: query returns {v:,.0f} "
                  f"({'ORIGINAL — correct, no lookahead' if v == first['value'] else 'MISMATCH'})")
        # as-of BEFORE the first filing: period must be INVISIBLE
        before = (pd.Timestamp(first["filed"]) - pd.Timedelta(days=1)).date().isoformat()
        got_b = asof_quarterly(facts, tk, "revenue", before)
        vis = pend in set(got_b["period_end"]) if not got_b.empty else False
        print(f"    as-of {before} (before it was filed): period visible? {vis} "
              f"({'correct — invisible' if not vis else 'LEAK'})")
    else:
        print("  (no multi-value restatement of revenue found in this universe slice)")

    # Independent no-lookahead check for a large cap: pick AAPL, an as-of mid-2022, and
    # confirm nothing with filed>as_of appears.
    tk = "AAPL"
    as_of = "2022-06-30"
    got = asof_quarterly(facts, tk, "revenue", as_of)
    if not got.empty:
        max_filed = pd.to_datetime(got["filed"]).max().date().isoformat()
        latest_period = got["period_end"].max()
        print(f"\n  {tk} revenue as-of {as_of}: {len(got)} periods visible, "
              f"latest filing {max_filed} (<= {as_of} ? "
              f"{'YES' if max_filed <= as_of else 'NO — LEAK'}); "
              f"latest period_end visible = {latest_period}")


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------

def main() -> None:
    stage = sys.argv[1] if len(sys.argv) > 1 else "all"
    if stage in ("ingest", "all"):
        ingest()
    if stage in ("build", "all"):
        build()
    if stage in ("table", "all"):
        table()
    if stage in ("validate", "all"):
        validate()


if __name__ == "__main__":
    main()
