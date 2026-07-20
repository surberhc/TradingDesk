"""
Fetcher functions -- one per metric.

Contract: every fetcher returns a dict with the same shape:
    {
        "metric":      <str, the metric key from config.METRIC_KEYS>,
        "value":       <float | None>,
        "as_of_date":  <str "YYYY-MM-DD" | None>,
        "source":      <str, human-readable source>,
        "status":      "ok" | "needs_api_key" | "error: <msg>",
    }

Each fetcher is wrapped in try/except so a single failure (network down,
format change, missing key) never aborts the whole run -- it just records an
"error: ..." status for that one metric.

Networking uses stdlib urllib so the project runs with zero third-party deps.
"""

import io
import os
import re
import csv as _csv
import json
import datetime as _dt
from urllib import request, parse, error

import config


# --------------------------------------------------------------------------- #
# Small HTTP helper (stdlib only)
# --------------------------------------------------------------------------- #
def _http_get(url, timeout=config.HTTP_TIMEOUT):
    """GET a URL and return decoded text. Raises on network/HTTP error."""
    req = request.Request(url, headers={"User-Agent": config.USER_AGENT})
    with request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    # Best-effort decode; holdings files are usually utf-8 / latin-1.
    for enc in ("utf-8", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _http_get_bytes(url, timeout=config.HTTP_TIMEOUT):
    req = request.Request(url, headers={"User-Agent": config.USER_AGENT})
    with request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _today():
    return _dt.date.today().isoformat()


def _result(metric, value=None, as_of_date=None, source="", status="ok"):
    return {
        "metric": metric,
        "value": value,
        "as_of_date": as_of_date,
        "source": source,
        "status": status,
    }


# --------------------------------------------------------------------------- #
# FRED helper
# --------------------------------------------------------------------------- #
def _fred_latest(series_id, api_key, want_recent=True):
    """
    Return (value: float, date: str) for the most recent non-missing
    observation of a FRED series. Raises on error.
    """
    params = {
        "series_id": series_id,
        "api_key": api_key,
        "file_type": "json",
        "sort_order": "desc",   # newest first
        "limit": "12",          # enough to skip any trailing "." missing values
    }
    url = config.FRED_BASE_URL + "?" + parse.urlencode(params)
    text = _http_get(url)
    data = json.loads(text)
    for obs in data.get("observations", []):
        if obs.get("value") not in (".", "", None):
            return float(obs["value"]), obs["date"]
    raise ValueError(f"no valid observations for {series_id}")


# --------------------------------------------------------------------------- #
# 1. S&P 500 trailing P/E  (multpl.com scrape)
# --------------------------------------------------------------------------- #
def _parse_multpl_current(html):
    """
    Extract the current value from a multpl.com page.

    multpl markup is, roughly:
        <div id="current"> ... </b>\n 31.98 \n ... </div>
    and the <meta name="description"> reliably says "...is 31.98,...".
    We try the #current block first, then the meta description as a fallback.
    """
    # Primary: number right after the bold "Current ...:" label inside #current.
    m = re.search(
        r'id="current".*?</b>\s*([0-9]+(?:\.[0-9]+)?)',
        html, re.DOTALL,
    )
    if m:
        return float(m.group(1))
    # Fallback: meta description "... is 31.98, ...".
    m = re.search(
        r'name="description"\s+content="[^"]*?is\s+([0-9]+(?:\.[0-9]+)?)',
        html,
    )
    if m:
        return float(m.group(1))
    raise ValueError("could not locate current value in page")


def fetch_sp500_trailing_pe():
    metric = "sp500_trailing_pe"
    source = "multpl.com (S&P 500 P/E ratio)"
    try:
        html = _http_get(config.MULTPL_SP500_PE_URL)
        value = _parse_multpl_current(html)
        return _result(metric, value, _today(), source, "ok")
    except error.URLError as e:
        return _result(metric, None, None, source, f"error: network ({e.reason})")
    except Exception as e:
        return _result(metric, None, None, source, f"error: {e}")


# --------------------------------------------------------------------------- #
# 2. CAPE / Shiller P/E
#    Primary: multpl.com scrape (reliable, no .xls dependency).
#    Optional upgrade: Shiller ie_data.xls (needs xlrd -- see README/TODO).
# --------------------------------------------------------------------------- #
def fetch_cape_shiller_pe():
    metric = "cape_shiller_pe"
    source = "multpl.com (Shiller PE / CAPE)"
    try:
        html = _http_get(config.MULTPL_CAPE_URL)
        value = _parse_multpl_current(html)
        return _result(metric, value, _today(), source, "ok")
    except error.URLError as e:
        return _result(metric, None, None, source, f"error: network ({e.reason})")
    except Exception as e:
        return _result(metric, None, None, source, f"error: {e}")


# --------------------------------------------------------------------------- #
# 3. Buffett Indicator = total corporate equity market value / GDP   (FRED)
#    Quarterly. NCBEILQ027S is in $MILLIONS; GDP is in $BILLIONS, so the
#    numerator is divided by 1000 to match units before forming the ratio.
# --------------------------------------------------------------------------- #
def fetch_buffett_indicator():
    metric = "buffett_indicator"
    source = (
        f"FRED ({config.FRED_BUFFETT_NUM_SERIES} / {config.FRED_GDP_SERIES}, "
        f"quarterly)"
    )
    api_key = os.environ.get("FRED_API_KEY")
    if not api_key:
        return _result(metric, None, None, source, "needs_api_key")
    try:
        equities_mm, eq_date = _fred_latest(config.FRED_BUFFETT_NUM_SERIES, api_key)
        gdp_b, _g_date = _fred_latest(config.FRED_GDP_SERIES, api_key)
        # NCBEILQ027S is $MILLIONS -> /1000 to $BILLIONS to match GDP ($B).
        value = round(100.0 * (equities_mm / 1000.0) / gdp_b, 1)
        as_of = eq_date  # latest numerator (equities) observation date
        return _result(metric, value, as_of, source, "ok")
    except error.URLError as e:
        return _result(metric, None, None, source, f"error: network ({e.reason})")
    except Exception as e:
        return _result(metric, None, None, source, f"error: {e}")


# --------------------------------------------------------------------------- #
# 4. Household equity allocation (% of household financial assets)   (FRED)
# --------------------------------------------------------------------------- #
def fetch_hh_equity_allocation():
    metric = "hh_equity_allocation_pct"
    source = (
        f"FRED Z.1 ({config.FRED_HH_EQUITIES_SERIES} / "
        f"{config.FRED_HH_FIN_ASSETS_SERIES}, quarterly; public proxy)"
    )
    api_key = os.environ.get("FRED_API_KEY")
    if not api_key:
        return _result(metric, None, None, source, "needs_api_key")
    try:
        # Both series are $MILLIONS, quarterly, so no unit conversion is needed.
        # NOTE: public proxy -- this will NOT exactly equal InvesTech's
        # proprietary ~46% figure because their denominator differs.
        equities, e_date = _fred_latest(config.FRED_HH_EQUITIES_SERIES, api_key)
        assets, _a_date = _fred_latest(config.FRED_HH_FIN_ASSETS_SERIES, api_key)
        value = round(100.0 * equities / assets, 1)
        as_of = e_date  # latest numerator observation date
        return _result(metric, value, as_of, source, "ok")
    except error.URLError as e:
        return _result(metric, None, None, source, f"error: network ({e.reason})")
    except Exception as e:
        return _result(metric, None, None, source, f"error: {e}")


# --------------------------------------------------------------------------- #
# 5. Top-10 concentration -- Gorilla Index proxy  (IVV holdings CSV)
# --------------------------------------------------------------------------- #
def _top10_from_spy_xlsx():
    """
    Primary path: State Street SPY holdings .xlsx via openpyxl.
    Returns (top10_pct: float, as_of_date: str). Raises if openpyxl is
    unavailable or the layout is unrecognized.
    """
    try:
        import openpyxl
    except ImportError:
        raise RuntimeError("openpyxl not installed (needed for SPY .xlsx)")

    raw = _http_get_bytes(config.SPY_HOLDINGS_XLSX_URL)
    wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    ws = wb.active

    as_of = _today()
    weight_col = None
    weights = []
    for row in ws.iter_rows(values_only=True):
        cells = [("" if c is None else str(c)).strip() for c in row]
        # Capture the "As of <date>" line near the top.
        if cells and cells[0].lower().startswith("holdings") and len(cells) > 1:
            m = re.search(r"As of\s+(.+)", cells[1])
            if m:
                as_of = m.group(1).strip()
        # Locate the header row, then read weights below it.
        if weight_col is None:
            for j, c in enumerate(cells):
                if c.lower() == "weight":
                    weight_col = j
                    break
            continue
        if len(cells) <= weight_col:
            continue
        raw_w = cells[weight_col].replace("%", "").replace(",", "")
        try:
            weights.append(float(raw_w))
        except ValueError:
            continue
    wb.close()

    if not weights:
        raise ValueError("no numeric weights parsed from SPY xlsx")
    weights.sort(reverse=True)
    return round(sum(weights[:10]), 2), as_of


def fetch_top10_concentration():
    metric = "top10_concentration_pct"
    source = "State Street SPY daily holdings .xlsx (top-10 weight sum)"
    # Primary: SPY .xlsx (openpyxl). Falls back to IVV CSV on failure.
    try:
        top10, as_of = _top10_from_spy_xlsx()
        return _result(metric, top10, as_of, source, "ok")
    except error.URLError as e:
        return _result(metric, None, None, source, f"error: network ({e.reason})")
    except Exception as spy_err:
        # Fall back to iShares IVV CSV below; remember why SPY failed.
        source = "iShares IVV daily holdings CSV (fallback; SPY failed)"

    try:
        text = _http_get(config.IVV_HOLDINGS_CSV_URL)
        if "<html" in text[:2000].lower() or "<!doctype" in text[:2000].lower():
            raise ValueError("iShares returned an HTML page, not the CSV")
        # iShares CSVs have a preamble of metadata lines before the real header.
        # Find the header row that contains a weight column.
        lines = text.splitlines()
        header_idx = None
        for i, line in enumerate(lines):
            low = line.lower()
            if ("weight" in low) and ("ticker" in low or "name" in low):
                header_idx = i
                break
        if header_idx is None:
            raise ValueError("could not find holdings header row")

        reader = _csv.reader(lines[header_idx:])
        header = next(reader)
        # Identify the weight column.
        weight_col = None
        for j, col in enumerate(header):
            if "weight" in col.lower():
                weight_col = j
                break
        if weight_col is None:
            raise ValueError("no weight column in header")

        weights = []
        for row in reader:
            if len(row) <= weight_col:
                continue
            raw = row[weight_col].strip().replace("%", "").replace(",", "")
            if not raw:
                continue
            try:
                weights.append(float(raw))
            except ValueError:
                continue  # skip non-numeric (footer/cash rows)

        if not weights:
            raise ValueError("no numeric weights parsed")

        weights.sort(reverse=True)
        top10 = round(sum(weights[:10]), 2)
        return _result(metric, top10, _today(), source, "ok")
    except error.URLError as e:
        return _result(metric, None, None, source, f"error: network ({e.reason})")
    except Exception as e:
        return _result(metric, None, None, source, f"error: {e}")


# --------------------------------------------------------------------------- #
# Registry -- main.py iterates this in METRIC_KEYS order.
# --------------------------------------------------------------------------- #
FETCHERS = {
    "sp500_trailing_pe": fetch_sp500_trailing_pe,
    "cape_shiller_pe": fetch_cape_shiller_pe,
    "buffett_indicator": fetch_buffett_indicator,
    "hh_equity_allocation_pct": fetch_hh_equity_allocation,
    "top10_concentration_pct": fetch_top10_concentration,
}
