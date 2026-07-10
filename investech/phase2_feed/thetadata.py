"""
ThetaData local-Terminal integration for the Phase-2 breadth feed.

ThetaData has NO cloud REST API. Data is served by a local Java *Terminal*
(default http://127.0.0.1:25503) that the user runs and logs into with the
THETADATA_API_KEY. Once the Terminal is logged in it serves data WITHOUT the key
in the request, so this module never places the key in a URL and never prints
it. (env_loader.get_thetadata_key() is used only to confirm the key is
configured so we can emit a clearer "not configured" hint.)

Why use it: the Terminal is a BULK EOD / history source with no free-tier hourly
cap (unlike the rate-limited free Tiingo key), so it is the PREFERRED price
source when DATA_SOURCE == "thetadata" (or auto-detect finds the Terminal up).
It also unlocks a TRUE broad NYSE/NASDAQ universe for real exchange-wide breadth
(advance/decline + new highs/lows), which is what InvesTech's NLC actually uses
-- vs the S&P-500 large-cap subset the Tiingo path is limited to.

Endpoints implemented (ThetaData v3 REST, local Terminal). v2 is deprecated --
the local Terminal now serves v3 only (v2 paths respond with an "upgraded to
API v3" error). Docs: https://http-docs.thetadata.us/
  Status  : GET /v3/system/mdds/status                (health check)
  Symbols : GET /v3/stock/list/symbols                 (all traded stock tickers)
  EOD     : GET /v3/stock/history/eod?symbol=<T>&start_date=YYYYMMDD&end_date=YYYYMMDD

v3 responses are plain CSV (no JSON envelope, no pagination wrapper), default
`format=csv` (also supports json/ndjson/html via an explicit `format` param --
we use csv to keep this stdlib-only with the `csv` module, no third-party JSON
schema to track).

EOD CSV header (confirmed live against the running Terminal):
  created,last_trade,open,high,low,close,volume,count,
  bid_size,bid_exchange,bid,bid_condition,ask_size,ask_exchange,ask,ask_condition
There is no plain 'date' column -- both 'created' and 'last_trade' are ISO
timestamps. We take the bar's date from 'last_trade' (the timestamp of the
final trade that day), since that is the field definitionally tied to the
trading session the bar represents ('created' is just when ThetaData generated
the EOD report, which happens to match empirically but is not what the row
"is"). Verified live: for AAPL over a 250-trading-day sample, 'created' and
'last_trade' agreed on date in every single row (0 mismatches), so this choice
makes no observed difference today, but 'last_trade' is the principled pick.

The v3 EOD endpoint caps each request to a 365-calendar-day window (confirmed
live: a >365-day request returns HTTP 400 "Too many days between start and end
date; max 365 days allowed") and there is no next-page / cursor pagination
mechanism (confirmed: a 364-day AAPL pull returned exactly 251 rows, in line
with ~252 US trading days/year, with no truncation). Callers that need more
than ~1 year of history must chunk the range into <=365-day windows themselves;
this module does not do that automatically (not needed by current callers,
which default to config.TIINGO_LOOKBACK_DAYS, well under 365 days).

Networking is stdlib urllib only (no third-party deps), matching breadth.py.
Every call degrades gracefully: if the Terminal is down the connection is
refused and we surface a clear "terminal_unavailable" signal instead of crashing.
"""

import csv
import io
import datetime as _dt
from urllib import request, parse, error

import config
from env_loader import get_thetadata_key


# Sentinel raised/returned when the local Terminal is not reachable. Callers
# (breadth.py) catch this to fall back to Tiingo rather than aborting the run.
class TerminalUnavailable(Exception):
    """The local ThetaData Terminal could not be reached (likely not running)."""


# --------------------------------------------------------------------------- #
# Low-level HTTP (stdlib only) -- returns raw CSV text or raises
# --------------------------------------------------------------------------- #
def _base():
    return config.THETADATA_BASE_URL.rstrip("/")


def _get_csv(path, params=None, timeout=None):
    """
    GET {base}{path}?{params} and return the raw response text (CSV by default).

    Raises TerminalUnavailable on connection refused / timeout / DNS (i.e. the
    Terminal is down). Other HTTP/parse errors raise the underlying exception so
    real bugs are not silently swallowed.
    """
    timeout = timeout if timeout is not None else config.HTTP_TIMEOUT
    url = _base() + path
    if params:
        url = url + "?" + parse.urlencode(params)
    req = request.Request(url, headers={"User-Agent": config.USER_AGENT})
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except error.HTTPError as e:
        # Terminal answered with an HTTP error code (e.g. 472 no-data, 400 bad
        # request). Read the body so callers can inspect; re-raise.
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        e.thetadata_body = body  # attach for diagnostics
        raise
    except (error.URLError, ConnectionError, OSError, TimeoutError) as e:
        # ConnectionRefusedError (Terminal down) lands here, as do socket
        # timeouts and "actively refused" OS errors on Windows.
        raise TerminalUnavailable(
            f"ThetaData Terminal not reachable at {_base()} ({e})"
        )
    return raw.decode("utf-8", errors="replace")


# --------------------------------------------------------------------------- #
# Health check
# --------------------------------------------------------------------------- #
def is_terminal_up(timeout=None):
    """
    Quick health probe of the local Terminal. Returns True iff GET
    /v3/system/mdds/status responds at all (short timeout). Never raises --
    a down Terminal (connection refused) cleanly returns False.

    Note: this endpoint returns HTTP 404 even when the Terminal is healthy
    (confirmed live against v3) -- any response (including an HTTPError) still
    means the Terminal answered the socket, so it counts as "up". Only a
    connection failure (Terminal not running) counts as "down".
    """
    timeout = timeout if timeout is not None else getattr(
        config, "THETADATA_HEALTH_TIMEOUT", 3
    )
    url = _base() + "/system/mdds/status"
    req = request.Request(url, headers={"User-Agent": config.USER_AGENT})
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            resp.read()
        return True
    except error.HTTPError:
        # An HTTP error code still means the Terminal answered the socket.
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# Stock EOD history
# --------------------------------------------------------------------------- #
def _fmt_date(d):
    """Accept 'YYYY-MM-DD', date/datetime, or int YYYYMMDD -> int YYYYMMDD."""
    if isinstance(d, int):
        return d
    if isinstance(d, (_dt.date, _dt.datetime)):
        return int(d.strftime("%Y%m%d"))
    s = str(d).strip()
    if "-" in s:  # ISO 'YYYY-MM-DD'
        return int(s.replace("-", "")[:8])
    return int(s[:8])


def _date_from_iso_timestamp(v):
    """Take the 'YYYY-MM-DD' date portion off an ISO timestamp like
    '2026-01-06T17:15:23.636'."""
    return str(v).strip()[:10]


def get_eod_history(ticker, start_date, end_date, timeout=None):
    """
    Return ascending [{"date","close","high","low"}, ...] for one stock symbol
    via GET /v3/stock/history/eod (CSV response).

    Reads close/high/low from the CSV header by NAME (robust if ThetaData
    reorders columns) and takes the bar's date from the 'last_trade' column's
    date portion (see module docstring for why). Returns [] if the Terminal
    reports no data for the symbol/range. Raises TerminalUnavailable if the
    Terminal is down so callers can fall back to Tiingo.

    Note: ThetaData stock EOD is split-adjusted but NOT dividend-adjusted, unlike
    the Tiingo path which uses adjClose. For breadth (MA crossovers, 52w hi/lo,
    daily up/down) this is acceptable; see README "ThetaData vs Tiingo".

    Note: the v3 endpoint caps a single request to a 365-calendar-day window
    (HTTP 400 above that) and has no pagination -- callers needing more history
    must chunk the range themselves. Not needed by current callers.
    """
    symbol = str(ticker).strip().upper().replace("-", ".")  # Theta uses BRK.B
    params = {
        "symbol": symbol,
        "start_date": _fmt_date(start_date),
        "end_date": _fmt_date(end_date),
        "format": "csv",
    }

    path = "/stock/history/eod"
    try:
        text = _get_csv(path, params=params, timeout=timeout)
    except error.HTTPError as e:
        # 472/474/476 == "no data for the contract/date range" -> empty, not
        # fatal. (Kept from the v2 port in case v3 reuses these codes.)
        if getattr(e, "code", None) in (472, 474, 476):
            return []
        raise

    out = []
    reader = csv.DictReader(io.StringIO(text))
    for row in reader:
        try:
            last_trade = row.get("last_trade")
            d_iso = _date_from_iso_timestamp(last_trade) if last_trade else None
            c = float(row["close"]) if row.get("close") not in (None, "") else None
            if not d_iso or c is None:
                continue
            h = float(row["high"]) if row.get("high") not in (None, "") else c
            lo = float(row["low"]) if row.get("low") not in (None, "") else c
        except (TypeError, ValueError, KeyError):
            continue
        out.append({"date": d_iso, "close": c, "high": h, "low": lo})

    out.sort(key=lambda r: r["date"])
    return out


def get_eod_history_tuples(ticker, start_date=None, end_date=None, timeout=None):
    """
    Convenience adapter to the breadth.py cache shape: ascending
    [(date, close, high, low), ...]. Defaults the window to
    config.TIINGO_LOOKBACK_DAYS back from today (same history depth the rest of
    the feed expects: enough for a 200-day MA + 52-week hi/lo). Returns None on
    empty so callers treat it like a Tiingo miss; raises TerminalUnavailable if
    the Terminal is down.
    """
    if end_date is None:
        end_date = _dt.date.today()
    if start_date is None:
        lookback = getattr(config, "TIINGO_LOOKBACK_DAYS", 420)
        start_date = _dt.date.today() - _dt.timedelta(days=lookback)
    bars = get_eod_history(ticker, start_date, end_date, timeout=timeout)
    if not bars:
        return None
    return [(b["date"], b["close"], b["high"], b["low"]) for b in bars]


# --------------------------------------------------------------------------- #
# Universe (broad stock roots)  --  configurable source
# --------------------------------------------------------------------------- #
def list_stock_roots(timeout=None):
    """
    Return the full list of traded stock tickers from the local Terminal via
    GET /v3/stock/list/symbols (CSV response, single 'symbol' column).
    Raises TerminalUnavailable if the Terminal is down.

    Note: this is ThetaData's full historical+current universe (~26k symbols),
    much broader/noisier than "NYSE/NASDAQ common stock" -- it includes
    delisted/unit/warrant tickers etc. We replicate the prior behavior of
    returning the deduped list as-is; filtering it down is a separate concern.
    """
    text = _get_csv("/stock/list/symbols", params={"format": "csv"}, timeout=timeout)
    reader = csv.DictReader(io.StringIO(text))
    out = []
    seen = set()
    for row in reader:
        t = str(row.get("symbol", "")).strip().upper()
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def get_universe(timeout=None):
    """
    Return (tickers, source) for the broad ThetaData stock-roots universe.

    TRADEOFF (documented in README "S&P-500 vs broad universe"):
      * S&P 500 (the existing Tiingo path): 500 large-caps. Stable, licensed,
        reproducible -- but NOT what InvesTech's Negative Leadership Composite
        uses. The NLC is built on FULL NYSE/NASDAQ exchange breadth (every listed
        issue), where small-caps and secondary issues drive the new-low /
        downside-leadership signal the NLC is famous for.
      * Broad ThetaData roots: thousands of NYSE/NASDAQ issues -> a far closer
        approximation of true exchange breadth. Heavier (more EOD calls), and
        includes ETFs/ADRs/illiquid names unless filtered.

    config.THETADATA_UNIVERSE_LIMIT caps the broad list (None/0 = all). Raises
    TerminalUnavailable if the Terminal is down (caller falls back to the S&P-500
    list).
    """
    roots = list_stock_roots(timeout=timeout)
    source = "ThetaData broad stock symbols (/v3/stock/list/symbols)"
    limit = getattr(config, "THETADATA_UNIVERSE_LIMIT", None)
    if limit and limit > 0 and limit < len(roots):
        roots = roots[:limit]
        source += f" | LIMITED to first {limit} of broad universe"
    return roots, source
