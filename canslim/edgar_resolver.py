"""
EDGAR delisted / renamed CIK resolver — CAN SLIM Phase 2 (survivorship recovery).

The current `company_tickers.json` lists only ACTIVE filers. A survivorship-free backtest
needs the delisted and renamed names too (TWTR, XLNX, VMW, SPLK, SGEN, ATVI ...), whose
fundamentals STILL sit on EDGAR under the old CIK. This module builds an
    old-ticker  ->  CIK
resolver from SEC's bulk `submissions.zip`, which carries, per company:
    * `tickers`      — current tickers (may be empty for a delisted shell),
    * `formerNames`  — [{name, from, to}], the company's prior legal names,
    * `name`         — current legal name,
    * `exchanges`, `sicDescription`, and the full filing history.

We do NOT invent ticker history where SEC gives none. Two resolution layers, both sourced:
  1. TICKER layer  — every ticker SEC currently associates with each CIK (submissions +
     company_tickers.json). This is authoritative but only covers still-listed tickers.
  2. NAME layer    — every legal name a CIK ever had (`name` + all `formerNames`), normalized.
     This is how we recover a DELISTED old ticker: the Phase-1 watch list carries the old
     ticker; we map that ticker -> the company's known name (from an external label the
     advisor list already implies, or a hand seed), then match the NAME against SEC's
     name+formerNames index to recover the CIK. For the concrete Phase-1 unresolved tail we
     ship an explicit, sourced old-ticker->name seed (delisted names have no SEC ticker row
     at all, so a ticker can only be recovered via its company name).

Design contract: this module only RESOLVES identities (ticker/name -> CIK). It does not
touch fundamentals or the as-of layer — those stay in edgar_pipeline.py, unchanged.

DATA (local warehouse, never on Drive):
    C:/TradingDesk-Local/canslim/edgar/submissions.zip        (SEC bulk, ~1.55 GB)
    C:/TradingDesk-Local/canslim/edgar/cik_identity.parquet   (built identity index)
"""

from __future__ import annotations

import json
import re
import time
import zipfile
from pathlib import Path

import pandas as pd

WAREHOUSE = Path(r"C:\TradingDesk-Local\canslim\edgar")
SUBMISSIONS_ZIP = WAREHOUSE / "submissions.zip"
TICKER_MAP_JSON = WAREHOUSE / "company_tickers.json"
IDENTITY_PARQUET = WAREHOUSE / "cik_identity.parquet"          # one row per (cik) w/ names+tickers
NAME_INDEX_PARQUET = WAREHOUSE / "cik_name_index.parquet"      # long: (norm_name, cik, kind, from, to)

SUBMISSIONS_URL = "https://www.sec.gov/Archives/edgar/daily-index/bulkdata/submissions.zip"
USER_AGENT = "Surber HC Trading Research andrew@surberhc.com"
HEADERS = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"}


# --------------------------------------------------------------------------------------
# Name normalization — matching legal names is noisy; normalize away the corporate suffixes
# and punctuation so "Twitter, Inc." == "TWITTER INC" == "Twitter Inc.".
# --------------------------------------------------------------------------------------

_SUFFIXES = [
    "incorporated", "inc", "corporation", "corp", "company", "co", "limited", "ltd",
    "llc", "lp", "plc", "holdings", "holding", "group", "the", "sa", "nv", "ag",
    "class a", "class b", "class c", "common stock", "ordinary shares",
]
_SUFFIX_RE = re.compile(r"\b(" + "|".join(re.escape(s) for s in _SUFFIXES) + r")\b")


def normalize_name(name: str) -> str:
    """Uppercase, strip punctuation, drop corporate suffixes, collapse whitespace."""
    if not name:
        return ""
    s = name.lower()
    s = s.replace("&", " and ")
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = _SUFFIX_RE.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s.upper()


# --------------------------------------------------------------------------------------
# Download
# --------------------------------------------------------------------------------------

def download_submissions() -> None:
    import requests
    if SUBMISSIONS_ZIP.exists() and SUBMISSIONS_ZIP.stat().st_size > 1_000_000_000:
        print(f"  [skip] submissions.zip present ({SUBMISSIONS_ZIP.stat().st_size/1e9:.2f} GB)")
        return
    print(f"  [get ] {SUBMISSIONS_URL}")
    WAREHOUSE.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    with requests.get(SUBMISSIONS_URL, headers=HEADERS, stream=True, timeout=180) as r:
        r.raise_for_status()
        total = int(r.headers.get("Content-Length", 0)); got = 0
        with open(SUBMISSIONS_ZIP, "wb") as f:
            for c in r.iter_content(1 << 20):
                f.write(c); got += len(c)
                if total and got % (200 << 20) < (1 << 20):
                    print(f"       {got/1e6:.0f}/{total/1e6:.0f} MB", flush=True)
    print(f"  [done] {got/1e6:.0f} MB in {time.time()-t0:.0f}s")


# --------------------------------------------------------------------------------------
# Build the identity index from submissions.zip
# --------------------------------------------------------------------------------------

def _iter_primary_submission_jsons(zf: zipfile.ZipFile):
    """
    submissions.zip holds one CIK##########.json per company (the primary/current file) plus
    paginated CIK##########-submissions-NNN.json overflow files that carry ONLY older filing
    pages (no identity fields). We want only the primary files — they have name/tickers/
    formerNames. Filter to the exact 'CIK<digits>.json' pattern.
    """
    prim = re.compile(r"^CIK\d{10}\.json$")
    for n in zf.namelist():
        if prim.match(n):
            yield n


def build_identity_index() -> None:
    """
    Parse every primary submissions JSON into:
      * identity rows: cik, current_name, current_tickers (list), exchanges, former_names (list)
      * a long NAME index: (norm_name, cik, kind in {current,former}, from, to)
    Written to the warehouse as parquet.
    """
    print("BUILD IDENTITY INDEX <- submissions.zip")
    if not SUBMISSIONS_ZIP.exists():
        raise SystemExit("submissions.zip missing — run download first")

    id_rows: list[dict] = []
    name_rows: list[dict] = []
    n = 0
    with zipfile.ZipFile(SUBMISSIONS_ZIP) as zf:
        for name in _iter_primary_submission_jsons(zf):
            try:
                with zf.open(name) as fh:
                    d = json.load(fh)
            except (KeyError, json.JSONDecodeError):
                continue
            cik = d.get("cik")
            if cik is None:
                # some files store cik only in the filename
                try:
                    cik = int(name[3:13])
                except ValueError:
                    continue
            cik = int(cik)
            cur_name = d.get("name") or ""
            tickers = d.get("tickers") or []
            exchanges = d.get("exchanges") or []
            formers = d.get("formerNames") or []
            id_rows.append({
                "cik": cik,
                "current_name": cur_name,
                "tickers": ";".join(str(t).upper() for t in tickers),
                "exchanges": ";".join(str(e) for e in exchanges),
                "n_former_names": len(formers),
                "sic": d.get("sicDescription", ""),
            })
            # NAME index — current
            nn = normalize_name(cur_name)
            if nn:
                name_rows.append({"norm_name": nn, "cik": cik, "kind": "current",
                                  "raw_name": cur_name, "from": "", "to": ""})
            # NAME index — every former name
            for fn in formers:
                raw = fn.get("name", "")
                nn = normalize_name(raw)
                if nn:
                    name_rows.append({"norm_name": nn, "cik": cik, "kind": "former",
                                      "raw_name": raw, "from": fn.get("from", ""),
                                      "to": fn.get("to", "")})
            n += 1
            if n % 5000 == 0:
                print(f"    parsed {n} companies...", flush=True)

    id_df = pd.DataFrame(id_rows)
    name_df = pd.DataFrame(name_rows)
    id_df.to_parquet(IDENTITY_PARQUET, index=False)
    name_df.to_parquet(NAME_INDEX_PARQUET, index=False)
    n_former = int((id_df["n_former_names"] > 0).sum())
    print(f"  companies: {len(id_df):,}; with >=1 former name: {n_former:,}")
    print(f"  name-index rows: {len(name_df):,} "
          f"(current {int((name_df['kind']=='current').sum()):,}, "
          f"former {int((name_df['kind']=='former').sum()):,})")
    print(f"  wrote {IDENTITY_PARQUET.name} and {NAME_INDEX_PARQUET.name}")


# --------------------------------------------------------------------------------------
# Resolvers
# --------------------------------------------------------------------------------------

def load_ticker_to_cik() -> dict[str, int]:
    """
    Build ticker -> CIK from BOTH sources:
      * submissions identity index (`tickers` per CIK — the fullest current set),
      * company_tickers.json (the classic map).
    Union; on collision the identity index wins (it is the primary submissions record).
    """
    tmap: dict[str, int] = {}
    if TICKER_MAP_JSON.exists():
        raw = json.loads(TICKER_MAP_JSON.read_text())
        for row in raw.values():
            tmap[str(row["ticker"]).upper()] = int(row["cik_str"])
    if IDENTITY_PARQUET.exists():
        idf = pd.read_parquet(IDENTITY_PARQUET)
        for _, r in idf.iterrows():
            if not r["tickers"]:
                continue
            for t in str(r["tickers"]).split(";"):
                if t:
                    tmap[t] = int(r["cik"])
    return tmap


def load_name_index() -> pd.DataFrame:
    return pd.read_parquet(NAME_INDEX_PARQUET)


def resolve_by_name(name: str, name_idx: pd.DataFrame) -> list[dict]:
    """
    Resolve a company NAME (current or former) -> CIK candidates. Returns a list of
    {cik, kind, raw_name, from, to}. Empty if none. Multiple hits are possible (same
    normalized name reused by different shells) — caller decides; we surface them all.
    """
    nn = normalize_name(name)
    if not nn:
        return []
    hits = name_idx[name_idx["norm_name"] == nn]
    return hits.to_dict("records")


def companyfacts_ciks() -> set[int]:
    """The set of CIKs that actually have a us-gaap companyfacts JSON (the recoverable ones)."""
    import zipfile
    zpath = WAREHOUSE / "companyfacts.zip"
    out: set[int] = set()
    with zipfile.ZipFile(zpath) as zf:
        for n in zf.namelist():
            if n.startswith("CIK") and n.endswith(".json"):
                try:
                    out.add(int(n[3:-5]))
                except ValueError:
                    pass
    return out


def _rank_name_hits(hits: list[dict], facts_ciks: set[int] | None) -> list[dict]:
    """
    Rank name-match candidates so the RIGHT CIK wins when a normalized name is ambiguous
    (e.g. 'DISH Network LLC' vs 'DISH Network CORP', both normalize identically):
      1. has a companyfacts file (a CIK with no fundamentals is useless to a backtest),
      2. former-name hit over current (we're usually tracking a rename),
      3. lower CIK (older = the original operating filer, not a later shell).
    """
    def key(h):
        c = int(h["cik"])
        has_facts = 0 if (facts_ciks and c in facts_ciks) else 1
        is_former = 0 if h["kind"] == "former" else 1
        return (has_facts, is_former, c)
    return sorted(hits, key=key)


# Sourced old-ticker -> company name seed for the Phase-1 delisted/renamed unresolved tail.
# Delisted tickers have NO row in SEC's ticker map, so a ticker can only be recovered via the
# company's legal name matched against submissions name+formerNames. Each entry is a real,
# checkable acquisition/rename — not synthesized fundamentals, just an identity label.
DELISTED_TICKER_TO_NAME: dict[str, str] = {
    # --- Phase-1 watch-list unresolved tail (all verified to hit the name index) ---
    "TWTR": "Twitter, Inc.", "XLNX": "XILINX INC", "VMW": "VMware, Inc.",
    "SPLK": "Splunk Inc.", "SGEN": "Seagen Inc.", "ATVI": "Activision Blizzard, Inc.",
    "ABMD": "ABIOMED, INC.", "CTXS": "Citrix Systems, Inc.",
    "DNKN": "Dunkin' Brands Group, Inc.",
    "HZNP": "Horizon Therapeutics Public Limited Company",
    "MXIM": "Maxim Integrated Products, Inc.",
    "AMED": "Amedisys Inc", "ARCH": "Arch Coal, Inc.", "ARNA": "Arena Pharmaceuticals Inc",
    "ATGE": "Adtalem Global Education Inc.", "ATUS": "Altice USA, Inc.",
    "AVLR": "Avalara, Inc.", "AXNX": "Axonics, Inc.", "AZPN": "Aspen Technology, Inc.",
    "BPMC": "Blueprint Medicines Corp", "CCMP": "CMC Materials, Inc.",
    "CDAY": "Ceridian HCM Holding Inc.", "CFLT": "Confluent, Inc.",
    "CIVI": "Civitas Resources, Inc.", "CLDR": "Cloudera, Inc.",
    "COOP": "Mr. Cooper Group Inc.", "COUP": "Coupa Software Inc",
    "CPE": "Callon Petroleum Co", "CTLT": "Catalent, Inc.", "CYBR": "CyberArk Software Ltd.",
    "DENN": "Denny's Corp", "DISH": "DISH Network CORP", "ELY": "Callaway Golf Co",
    "ENV": "Envestnet, Inc.", "ERJ": "Embraer S.A.", "EVBG": "Everbridge, Inc.",
    "FTCH": "Farfetch Limited", "GBT": "Global Blood Therapeutics, Inc.",
    "GLDD": "Great Lakes Dredge & Dock CORP", "GMS": "GMS Inc.", "HEAR": "Turtle Beach Corp",
    "HI": "Hillenbrand, Inc.", "HMSY": "HMS Holdings Corp", "HSC": "Harsco Corp",
    "HSII": "Heidrick & Struggles International Inc", "IIVI": "II-VI Inc",
    "INFO": "IHS Markit Ltd.", "INXN": "InterXion Holding N.V.", "IPHI": "Inphi Corp",
    "IRBT": "iRobot Corp", "KL": "Kirkland Lake Gold Ltd.", "LTHM": "Livent Corp.",
    "LVGO": "Livongo Health, Inc.", "MDCO": "MEDICINES CO /DE", "MDLA": "Medallia, Inc.",
    "MDRX": "Allscripts Healthcare Solutions, Inc.", "MIME": "Mimecast Ltd",
    "MODN": "Model N, Inc.", "MRUS": "Merus N.V.", "NEWR": "New Relic, Inc.",
    "NVEE": "NV5 Global, Inc.", "NVTA": "Invitae Corp", "ONEM": "1Life Healthcare, Inc.",
    "PDCO": "Patterson Companies, Inc.", "PFPT": "Proofpoint Inc",
    "PING": "Ping Identity Holding Corp.", "PLAN": "Anaplan, Inc.", "PRFT": "Perficient, Inc.",
    "PSTG": "Pure Storage, Inc.", "QTNA": "Quantenna Communications, Inc.",
    "RARX": "Ultragenyx Pharmaceutical Inc.", "RCII": "Rent-A-Center, Inc.",
    "REVG": "REV Group, Inc.", "RP": "RealPage, Inc.", "SEAS": "SeaWorld Entertainment, Inc.",
    "SMAR": "Smartsheet Inc", "SQSP": "Squarespace, Inc.", "STL": "Sterling Bancorp",
    "SUM": "Summit Materials, Inc.", "SWAV": "ShockWave Medical, Inc.",
    "SWIR": "Sierra Wireless Inc", "TPTX": "Turning Point Therapeutics, Inc.",
    "TPX": "Tempur Sealy International, Inc.", "UBNT": "Ubiquiti Networks, Inc.",
    "VRTV": "Veritiv Corp", "WIRE": "Encore Wire Corp",
    "WWE": "World Wrestling Entertainment, Inc.", "X": "United States Steel Corp",
    "ZI": "ZoomInfo Technologies Inc.",
    # --- Other well-known S&P-era renames/acquisitions (checkable M&A facts) ---
    "ALXN": "Alexion Pharmaceuticals, Inc.", "CERN": "CERNER Corp",
    "CXO": "Concho Resources Inc.",
    "WLTW": "Willis Towers Watson Public Limited Company", "FLIR": "FLIR Systems, Inc.",
    "MYL": "Mylan N.V.", "CELG": "Celgene Corp", "RTN": "Raytheon Company",
    "AGN": "Allergan plc", "STI": "SUNTRUST BANKS INC", "COL": "Rockwell Collins, Inc.",
    "ANDV": "Andeavor", "PCLN": "Priceline Group Inc.", "TIF": "Tiffany & Co.",
    "NBL": "Noble Energy Inc", "ETFC": "E TRADE FINANCIAL CORP",
    "WCG": "WellCare Health Plans, Inc.", "CBS": "CBS Corp", "VIAB": "Viacom Inc.",
    "APC": "ANADARKO PETROLEUM CORP", "RHT": "RED HAT INC",
    "ESRX": "EXPRESS SCRIPTS HOLDING CO", "TSS": "TOTAL SYSTEM SERVICES INC",
    "LLL": "L3 Technologies, Inc.", "DPS": "Dr Pepper Snapple Group, Inc.",
    "WYN": "Wyndham Worldwide Corp", "TWX": "TIME WARNER INC.", "MON": "MONSANTO CO",
    "SCG": "SCANA Corp",
}


def resolve_watchlist_tail(unresolved_tickers: list[str],
                           facts_ciks: set[int] | None = None) -> pd.DataFrame:
    """
    Given Phase-1's unresolved tickers, try to recover a CIK via:
      1. the (now fuller) ticker map (submissions tickers may cover a name Phase 1 missed),
      2. the sourced delisted-ticker->name seed matched against the name index, ranked so a
         facts-bearing CIK wins over a same-name shell (see _rank_name_hits).
    `facts_ciks` (set of CIKs with a companyfacts file) disambiguates; auto-loaded if None.
    Returns a coverage frame: ticker, cik, method, matched_name, kind, has_facts.
    """
    tmap = load_ticker_to_cik()
    name_idx = load_name_index()
    if facts_ciks is None:
        facts_ciks = companyfacts_ciks()
    rows: list[dict] = []
    for t in unresolved_tickers:
        t = t.upper()
        # 1) direct via fuller ticker map
        if t in tmap:
            cik = int(tmap[t])
            rows.append({"ticker": t, "cik": cik, "method": "ticker_map_fuller",
                         "matched_name": "", "kind": "", "has_facts": cik in facts_ciks})
            continue
        # 2) delisted-name seed -> name index (facts-aware ranking)
        nm = DELISTED_TICKER_TO_NAME.get(t)
        if nm:
            hits = resolve_by_name(nm, name_idx)
            if hits:
                h = _rank_name_hits(hits, facts_ciks)[0]
                cik = int(h["cik"])
                rows.append({"ticker": t, "cik": cik, "method": "name_seed",
                             "matched_name": h["raw_name"], "kind": h["kind"],
                             "has_facts": cik in facts_ciks})
                continue
        rows.append({"ticker": t, "cik": None, "method": "unrecovered",
                     "matched_name": "", "kind": "", "has_facts": False})
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------

def main() -> None:
    import sys
    stage = sys.argv[1] if len(sys.argv) > 1 else "all"
    if stage in ("download", "all"):
        download_submissions()
    if stage in ("index", "all"):
        build_identity_index()


if __name__ == "__main__":
    main()
