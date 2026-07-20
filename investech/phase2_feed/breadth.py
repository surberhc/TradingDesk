"""
Breadth computation module for the Phase-2 "Leadership Proxy" feed.

Responsibilities
----------------
1. Resolve the S&P 500 universe (static CSV first, then Wikipedia fallback).
2. Pull daily EOD prices per constituent from Tiingo (licensed desk key).
3. Compute daily breadth metrics across the universe:
     - % above 50-day / 200-day moving average
     - 52-week new highs / new lows (count + net + net %)
     - daily advances / declines, net, and a cumulative A/D line
4. Fold those into a transparent 0..100 "Leadership Proxy" composite that
   APPROXIMATES (does not replicate) InvesTech's proprietary Negative
   Leadership Composite behavior, and emit a documented regime label.

Networking is stdlib `urllib` only (no third-party deps required). Every
external step is guarded so one failure never aborts the run. The Tiingo key is
read via env_loader and is NEVER printed.
"""

import os
import csv
import json
import time
import datetime as _dt
from urllib import request, parse

import config
from env_loader import get_tiingo_key, get_thetadata_key
import thetadata


# --------------------------------------------------------------------------- #
# Data-source selection (ThetaData local Terminal vs Tiingo)
# --------------------------------------------------------------------------- #
# Cached once per run so we do not re-probe the Terminal for every ticker.
_TERMINAL_UP = None


def _terminal_up():
    """Probe the local ThetaData Terminal once per process; cache the result."""
    global _TERMINAL_UP
    if _TERMINAL_UP is None:
        try:
            _TERMINAL_UP = thetadata.is_terminal_up()
        except Exception:
            _TERMINAL_UP = False
    return _TERMINAL_UP


def _use_thetadata_prices():
    """
    Decide whether ThetaData is the PREFERRED price source for this run.
      DATA_SOURCE == "thetadata" -> yes if the Terminal is up (else Tiingo).
      DATA_SOURCE == "auto"      -> yes iff the Terminal is up.
      DATA_SOURCE == "tiingo"    -> never (original behavior).
    A down Terminal always degrades to Tiingo, so the run never aborts.
    """
    src = getattr(config, "DATA_SOURCE", "auto")
    if src == "tiingo":
        return False
    if src in ("thetadata", "auto"):
        return _terminal_up()
    return False


# --------------------------------------------------------------------------- #
# HTTP helpers (stdlib only)
# --------------------------------------------------------------------------- #
def _http_get_text(url, timeout=config.HTTP_TIMEOUT, headers=None):
    h = {"User-Agent": config.USER_AGENT}
    if headers:
        h.update(headers)
    req = request.Request(url, headers=h)
    with request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    for enc in ("utf-8", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


# --------------------------------------------------------------------------- #
# Universe resolution
# --------------------------------------------------------------------------- #
def load_universe():
    """
    Return (tickers, source, subset, full_count).

    UNIVERSE_SOURCE == "thetadata" (and the Terminal up) -> the BROAD
    NYSE/NASDAQ stock-roots list from the local Terminal (closest to the full
    exchange universe InvesTech's NLC uses). If the Terminal is down it falls
    back to the S&P-500 list below.

    UNIVERSE_SOURCE == "sp500" (default) -> the committed static CSV
    (reproducible), then the public Wikipedia table as a fallback.

    config.UNIVERSE_LIMIT (S&P-500 path) / config.THETADATA_UNIVERSE_LIMIT
    (broad path) cap the list for scaffold/test runs.
    """
    if getattr(config, "UNIVERSE_SOURCE", "sp500") == "thetadata":
        try:
            if thetadata.is_terminal_up():
                td_tickers, td_source = thetadata.get_universe()
                if td_tickers:
                    full_count = len(td_tickers)
                    subset = False
                    # THETADATA_UNIVERSE_LIMIT is applied inside get_universe;
                    # flag a subset if it trimmed the broad list.
                    if (config.THETADATA_UNIVERSE_LIMIT
                            and len(td_tickers) <= config.THETADATA_UNIVERSE_LIMIT):
                        subset = True
                    return td_tickers, td_source, subset, full_count
        except Exception:
            pass  # any Terminal issue -> degrade to S&P-500 below
        # fall through to S&P-500 when the Terminal is down / returns nothing

    tickers, source = _universe_from_csv()
    if not tickers:
        tickers, source = _universe_from_wikipedia()

    # Tiingo uses '-' for share classes (e.g. BRK-B); Wikipedia uses 'BRK.B'.
    tickers = [t.strip().upper().replace(".", "-") for t in tickers if t.strip()]
    seen = set()
    uni = []
    for t in tickers:
        if t not in seen:
            seen.add(t)
            uni.append(t)

    full_count = len(uni)
    subset = False
    if config.UNIVERSE_LIMIT and config.UNIVERSE_LIMIT < full_count:
        uni = uni[: config.UNIVERSE_LIMIT]
        subset = True
        source += (
            f" | SCAFFOLD SUBSET: first {len(uni)} of {full_count} constituents"
        )
    return uni, source, subset, full_count


def _universe_from_csv():
    path = config.UNIVERSE_CSV_PATH
    if not os.path.exists(path):
        return [], ""
    try:
        with open(path, "r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            col = config.UNIVERSE_CSV_SYMBOL_COL
            if reader.fieldnames and col not in reader.fieldnames:
                for fn in reader.fieldnames:  # forgiving about header casing
                    if fn.strip().lower() == col.lower():
                        col = fn
                        break
            tickers = [row[col] for row in reader if row.get(col)]
        if tickers:
            return tickers, f"static CSV ({path})"
    except Exception:
        pass
    return [], ""


def _universe_from_wikipedia():
    """Scrape the first column of tickers from the Wikipedia S&P 500 table."""
    import re
    try:
        html = _http_get_text(config.WIKIPEDIA_SP500_URL)
    except Exception:
        return [], "Wikipedia (unreachable)"
    m = re.search(r'id="constituents".*?</table>', html, re.DOTALL)
    block = m.group(0) if m else html
    rows = re.findall(r"<tr>(.*?)</tr>", block, re.DOTALL)
    tickers = []
    for r in rows:
        cells = re.findall(r"<td[^>]*>(.*?)</td>", r, re.DOTALL)
        if not cells:
            continue
        first = re.sub(r"<[^>]+>", "", cells[0]).strip().replace("&amp;", "&")
        if first and re.match(r"^[A-Za-z.\-]{1,6}$", first):
            tickers.append(first)
    if tickers:
        return tickers, f"Wikipedia ({config.WIKIPEDIA_SP500_URL})"
    return [], "Wikipedia (no rows parsed)"


# --------------------------------------------------------------------------- #
# Tiingo EOD pull
# --------------------------------------------------------------------------- #
def fetch_tiingo_prices(ticker, api_key, lookback_days=None):
    """
    Return a list of (date_str, close, high, low) ascending for one ticker, or
    None on error. Uses adjusted close/high/low (adjClose/adjHigh/adjLow) so
    MA/high-low logic is split/dividend consistent. Never raises -- returns None
    on failure (including HTTP 429 rate-limit responses).
    """
    lookback_days = lookback_days or config.TIINGO_LOOKBACK_DAYS
    start = (_dt.date.today() - _dt.timedelta(days=lookback_days)).isoformat()
    params = {
        "startDate": start,
        "format": "json",
        "resampleFreq": "daily",
        "token": api_key,  # alternatively: header Authorization: Token <key>
    }
    url = config.TIINGO_BASE_URL.format(ticker=parse.quote(ticker))
    url = url + "?" + parse.urlencode(params)
    try:
        text = _http_get_text(url)
        data = json.loads(text)
    except Exception:
        return None
    if not isinstance(data, list) or not data:
        return None
    out = []
    for bar in data:
        d = bar.get("date", "")[:10]
        c = bar.get("adjClose")
        if c is None:
            c = bar.get("close")
        h = bar.get("adjHigh", bar.get("high"))
        lo = bar.get("adjLow", bar.get("low"))
        if d and c is not None:
            try:
                cf = float(c)
                hf = float(h) if h is not None else cf
                lf = float(lo) if lo is not None else cf
                out.append((d, cf, hf, lf))
            except (TypeError, ValueError):
                continue
    out.sort(key=lambda x: x[0])
    return out or None


# --------------------------------------------------------------------------- #
# EOD price cache (data/cache/<TICKER>.csv : date,close,high,low)
# --------------------------------------------------------------------------- #
def _cache_path(ticker):
    safe = ticker.replace(os.sep, "_").replace("/", "_")
    return os.path.join(config.CACHE_DIR, f"{safe}.csv")


def _expected_last_trading_day(today=None):
    """
    Most recent date for which an EOD bar should exist. EOD is published after
    the close, so 'today' itself is not guaranteed; we treat the latest weekday
    that is strictly before today as the freshness target (holidays may make a
    cache slightly older -- handled by accepting a small tolerance in fresh()).
    """
    today = today or _dt.date.today()
    d = today - _dt.timedelta(days=1)
    while d.weekday() >= 5:  # Sat=5, Sun=6
        d -= _dt.timedelta(days=1)
    return d


def _read_cache(ticker):
    """Return ascending [(date,close,high,low), ...] from cache, or None."""
    path = _cache_path(ticker)
    if not os.path.exists(path):
        return None
    out = []
    try:
        with open(path, "r", newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                d = (row.get("date") or "")[:10]
                if not d:
                    continue
                try:
                    c = float(row["close"])
                    h = float(row.get("high") or c)
                    lo = float(row.get("low") or c)
                except (TypeError, ValueError, KeyError):
                    continue
                out.append((d, c, h, lo))
    except Exception:
        return None
    out.sort(key=lambda x: x[0])
    return out or None


def _write_cache(ticker, series):
    """Persist a (date,close,high,low) series to data/cache/<TICKER>.csv."""
    try:
        os.makedirs(config.CACHE_DIR, exist_ok=True)
        path = _cache_path(ticker)
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["date", "close", "high", "low"])
            for d, c, h, lo in series:
                w.writerow([d, c, h, lo])
    except Exception:
        pass  # cache write failure must never abort the run


def _cache_is_fresh(series, today=None):
    """Fresh if the cache's newest bar is >= the expected last trading day."""
    if not series:
        return False
    target = _expected_last_trading_day(today)
    return series[-1][0] >= target.isoformat()


def _fetch_thetadata(ticker):
    """
    Fetch a (date,close,high,low) series from the local ThetaData Terminal, or
    None on empty/no-data. Returns None (not raise) if the Terminal is down so
    get_series can fall through to Tiingo. Same tuple shape as Tiingo, so the
    cache is written identically.
    """
    try:
        return thetadata.get_eod_history_tuples(ticker)
    except thetadata.TerminalUnavailable:
        return None
    except Exception:
        return None


def get_series(ticker, api_key, force_refresh=False):
    """
    Return (series, origin) where origin is 'cache', 'fetch', 'fetch_theta', or
    'miss'. Reuses a fresh cache file; otherwise fetches from the PREFERRED
    source and writes through to the same cache. On fetch failure but a usable
    (possibly stale) cache exists, returns the cache so a single failed ticker
    still contributes.

    Source preference (see config.DATA_SOURCE): when ThetaData is preferred and
    its Terminal is up, ThetaData is tried first; Tiingo is the fallback. The
    cache layer (data/cache/<TICKER>.csv) is used identically regardless of
    source -- ThetaData-fetched series cache the same way.
    """
    cached = _read_cache(ticker) if config.CACHE_ENABLED else None
    if (not force_refresh and cached is not None
            and _cache_is_fresh(cached)):
        return cached, "cache"

    prefer_theta = _use_thetadata_prices()

    # 1) Preferred source first.
    if prefer_theta:
        fetched = _fetch_thetadata(ticker)
        if fetched is not None:
            if config.CACHE_ENABLED:
                _write_cache(ticker, fetched)
            return fetched, "fetch_theta"

    # 2) Tiingo (either the primary source, or the fallback when Theta missed).
    if api_key:
        fetched = fetch_tiingo_prices(ticker, api_key)
        if fetched is not None:
            if config.CACHE_ENABLED:
                _write_cache(ticker, fetched)
            return fetched, "fetch"

    # 3) Both fetch paths failed (e.g. Tiingo 429, Theta no-data, Terminal down).
    #    Fall back to any cache we have, even if stale.
    if cached is not None:
        return cached, "cache"
    return None, "miss"


# --------------------------------------------------------------------------- #
# Per-ticker breadth signals
# --------------------------------------------------------------------------- #
def _sma(values, window):
    if len(values) < window:
        return None
    return sum(values[-window:]) / window


def per_ticker_signals(series):
    """
    Given an ascending [(date, close, high, low), ...] series, return per-name
    signals, or None if there is not enough history for the long MA. 52-week
    new-high/new-low logic uses CLOSE (unchanged from the original close-only
    behavior); high/low are cached for future use but not folded in here.
    """
    closes = [row[1] for row in series]
    if len(closes) < config.MA_LONG:
        return None  # not enough history for a 200-day MA

    last = closes[-1]
    prev = closes[-2] if len(closes) >= 2 else last

    sma50 = _sma(closes, config.MA_SHORT)
    sma200 = _sma(closes, config.MA_LONG)

    hl_window = (closes[-config.HIGH_LOW_WINDOW:]
                 if len(closes) >= config.HIGH_LOW_WINDOW else closes)
    win_high = max(hl_window)
    win_low = min(hl_window)

    return {
        "above_50": (sma50 is not None and last > sma50),
        "above_200": (sma200 is not None and last > sma200),
        "new_high_52w": last >= win_high,
        "new_low_52w": last <= win_low,
        "advanced": last > prev,
        "declined": last < prev,
    }


# --------------------------------------------------------------------------- #
# Composite "Leadership Proxy"  (transparent APPROXIMATION of NLC behavior)
# --------------------------------------------------------------------------- #
def leadership_proxy(metrics, exchange_score=None):
    """
    Blend breadth metrics into a single 0..100 "breadth health" score.

    APPROXIMATION ONLY -- mimics the *direction* of InvesTech's Negative
    Leadership Composite (Selling Vacuum vs Distribution), not its proprietary
    internals. Higher = broad participation + absent downside leadership
    ("Selling-Vacuum-like"); lower = downside leadership accelerating
    ("Distribution-like").

    Each S&P-500 component is normalized to 0..100, then weighted by
    PROXY_WEIGHTS:
      pct_above_50dma     -> already 0..100 (participation, short trend)
      pct_above_200dma    -> already 0..100 (participation, long trend)
      net_highs_lows_pct  -> in [-100, +100]; mapped via (x+100)/2
      ad_pct              -> (adv - dec)/universe in [-100,+100]; (x+100)/2

    `exchange_score` (0..100), when provided, is an ADDITIONAL true-exchange
    breadth sub-score. It is blended with PROVISIONAL weight
    config.EXCHANGE_BREADTH_WEIGHT; the S&P-500 weights are scaled pro rata to
    make room. When it is None (the current default -- no live source), the
    score is IDENTICAL to the original S&P-500-only blend, so the existing path
    is unchanged. The provisional weight will be set in the calibration step.
    """
    def to_score(key):
        v = metrics.get(key)
        if v is None:
            return None
        if key in ("pct_above_50dma", "pct_above_200dma"):
            return max(0.0, min(100.0, v))
        if key in ("net_highs_lows_pct", "ad_pct"):
            return max(0.0, min(100.0, (v + 100.0) / 2.0))
        return None

    # Optionally reserve a slice of the blend for the exchange-breadth sub-score.
    sp_scale = 1.0
    ex_w = 0.0
    if exchange_score is not None:
        ex_w = max(0.0, min(1.0, config.EXCHANGE_BREADTH_WEIGHT))
        sp_scale = 1.0 - ex_w

    num = 0.0
    wsum = 0.0
    for key, w in config.PROXY_WEIGHTS.items():
        s = to_score(key)
        if s is None:
            continue
        num += (w * sp_scale) * s
        wsum += (w * sp_scale)
    if exchange_score is not None:
        num += ex_w * max(0.0, min(100.0, exchange_score))
        wsum += ex_w
    if wsum == 0:
        return None
    return round(num / wsum, 1)


def classify_regime(score):
    if score is None:
        return None
    if score >= config.REGIME_BULL_MIN:
        return config.REGIME_LABELS["bull"]
    if score <= config.REGIME_BEAR_MAX:
        return config.REGIME_LABELS["bear"]
    return config.REGIME_LABELS["neutral"]


# --------------------------------------------------------------------------- #
# True NYSE/NASDAQ exchange breadth  (WIRED STUB -- see README)
# --------------------------------------------------------------------------- #
def fetch_exchange_breadth():
    """
    Fetch TRUE full-exchange breadth (NYSE + NASDAQ advance/decline and
    new-high/new-low) and return a 0..100 sub-score.

    Returns a dict:
        {"status": "ok"|"unavailable"|"disabled",
         "score": float|None,        # 0..100, only when status == "ok"
         "source": str,              # which source produced/would produce it
         "reason": str}              # human-readable detail

    WIRED SOURCE: the local ThetaData Terminal. When the Terminal is up AND a
    broad stock-roots universe is available, this computes REAL exchange-wide
    breadth bottom-up: for each issue in the broad universe it reads the EOD
    series (cached the same way as the rest of the feed) and tallies
    advances/declines and 52-week new-highs/new-lows, then maps
        ad_ratio = (adv - dec) / (adv + dec)          in [-1, +1]
        hl_ratio = (nh  - nl ) / (nh  + nl )          in [-1, +1]
    into a 0..100 sub-score: 100 * mean((ad_ratio+1)/2, (hl_ratio+1)/2).

    This is "bottom-up" exchange breadth from issue-level EOD (not a vendor
    $NYAD/$NYHL summary tick). It is only REAL when run over the BROAD universe
    (UNIVERSE_SOURCE="thetadata"); over the S&P-500 subset it would just echo the
    large-cap breadth, so we require the broad universe to mark it "ok".

    Degrades gracefully: Terminal down -> status "unavailable" (connection
    refused handled, never crashes); coverage too thin -> "unavailable". It
    NEVER fabricates a value.
    """
    if not config.EXCHANGE_BREADTH_ENABLED:
        return {"status": "disabled", "score": None,
                "source": "(toggle off)",
                "reason": "EXCHANGE_BREADTH_ENABLED is False"}

    # Probe whether the ThetaData Terminal is reachable locally. The key is read
    # (to confirm it is configured) but NEVER printed or sent in a URL.
    have_key = bool(get_thetadata_key())
    try:
        terminal_up = thetadata.is_terminal_up()
    except Exception:
        terminal_up = False

    if not terminal_up:
        reason = ("ThetaData Terminal not reachable at "
                  f"{config.THETADATA_BASE_URL} (start the local Terminal, "
                  "logged in with THETADATA_API_KEY, to enable).")
        if not have_key:
            reason += " THETADATA_API_KEY is also not set."
        return {"status": "unavailable", "score": None,
                "source": "ThetaData local Terminal (REST :25510)",
                "reason": reason}

    # Terminal IS up. Real exchange breadth requires the BROAD universe; over the
    # S&P-500 subset this would just restate large-cap breadth (not exchange-wide),
    # so stay honest and mark it unavailable rather than mislabel it.
    if getattr(config, "UNIVERSE_SOURCE", "sp500") != "thetadata":
        return {"status": "unavailable", "score": None,
                "source": "ThetaData local Terminal (REST :25510)",
                "reason": ("Terminal is up but UNIVERSE_SOURCE is not 'thetadata'. "
                           "Set PHASE2_UNIVERSE_SOURCE=thetadata for a true broad "
                           "NYSE/NASDAQ advance-decline / NH-NL reading.")}

    # Already computed inline by compute_breadth over the broad universe? It
    # passes its tallies via fetch_exchange_breadth_from_tallies(); this no-arg
    # path is the standalone probe used when called directly.
    return {"status": "unavailable", "score": None,
            "source": "ThetaData local Terminal (REST :25510)",
            "reason": ("Terminal up + broad universe selected; call "
                       "fetch_exchange_breadth_from_tallies() with the run's "
                       "advance/decline + NH/NL counts to score it.")}


def exchange_breadth_score_from_tallies(n_adv, n_dec, n_high, n_low):
    """
    Map raw exchange-wide tallies to a 0..100 sub-score (see fetch_exchange_breadth
    for the formula). Returns None if there is no signal (zero issues on both
    axes). Pure function -- no I/O.
    """
    ad_tot = n_adv + n_dec
    hl_tot = n_high + n_low
    parts = []
    if ad_tot > 0:
        parts.append(((n_adv - n_dec) / ad_tot + 1.0) / 2.0)
    if hl_tot > 0:
        parts.append(((n_high - n_low) / hl_tot + 1.0) / 2.0)
    if not parts:
        return None
    return round(100.0 * (sum(parts) / len(parts)), 1)


# --------------------------------------------------------------------------- #
# Top-level aggregator
# --------------------------------------------------------------------------- #
def compute_breadth(prev_ad_line=0.0, progress=None):
    """
    Pull the universe, fetch prices, compute breadth + proxy + regime.

    `prev_ad_line` seeds the cumulative A/D line (read from the last CSV row by
    main.py) so the A/D line accumulates across daily runs.
    """
    api_key = get_tiingo_key()
    # ThetaData (local Terminal) is a complete price source on its own. Only the
    # Tiingo-only path strictly requires the Tiingo key: if ThetaData is preferred
    # and its Terminal is up, we can run with no Tiingo key at all.
    if not api_key and not _use_thetadata_prices():
        return {
            "status": "needs_api_key",
            "universe_source": "(not loaded -- no Tiingo key, "
                               "ThetaData Terminal also down)",
            "subset": False,
            "full_universe_count": 0,
            "metrics": {},
            "tiingo_authenticated": False,
            "processed": 0,
            "attempted": 0,
        }

    tickers, source, subset, full_count = load_universe()
    if not tickers:
        return {
            "status": "error: could not resolve S&P 500 universe",
            "universe_source": source,
            "subset": subset,
            "full_universe_count": full_count,
            "metrics": {},
            "tiingo_authenticated": None,
            "processed": 0,
            "attempted": 0,
        }

    n_above_50 = n_above_200 = 0
    n_high = n_low = 0
    n_adv = n_dec = 0
    processed = 0
    n_from_cache = n_from_fetch = 0
    auth_ok = None  # becomes True on first successful fetch

    for i, tk in enumerate(tickers):
        if progress:
            progress(i + 1, len(tickers), tk)
        series, origin = get_series(tk, api_key,
                                    force_refresh=config.FORCE_REFRESH)
        if origin == "fetch":
            n_from_fetch += 1
            auth_ok = True  # valid JSON price data back -> key works
            time.sleep(config.TIINGO_REQUEST_PAUSE)  # pace only real Tiingo fetches
        elif origin == "fetch_theta":
            n_from_fetch += 1  # ThetaData fetch (no rate-limit pause needed)
        elif origin == "cache":
            n_from_cache += 1
        if series is None:
            continue
        sig = per_ticker_signals(series)
        if sig is None:
            continue
        processed += 1
        n_above_50 += 1 if sig["above_50"] else 0
        n_above_200 += 1 if sig["above_200"] else 0
        n_high += 1 if sig["new_high_52w"] else 0
        n_low += 1 if sig["new_low_52w"] else 0
        n_adv += 1 if sig["advanced"] else 0
        n_dec += 1 if sig["declined"] else 0

    if processed == 0:
        return {
            "status": ("error: no constituents returned usable price history "
                       "(check Tiingo key/entitlement/network; may be rate-"
                       "limited -- HTTP 429 -- with an empty cache)"),
            "universe_source": source,
            "subset": subset,
            "full_universe_count": full_count,
            "metrics": {},
            "tiingo_authenticated": bool(auth_ok),
            "processed": 0,
            "attempted": len(tickers),
            "from_cache": n_from_cache,
            "from_fetch": n_from_fetch,
        }

    pct50 = round(100.0 * n_above_50 / processed, 1)
    pct200 = round(100.0 * n_above_200 / processed, 1)
    net_hl = n_high - n_low
    net_hl_pct = round(100.0 * net_hl / processed, 2)
    ad_net = n_adv - n_dec
    ad_pct = round(100.0 * ad_net / processed, 2)
    ad_line = round(prev_ad_line + ad_net, 1)

    metrics = {
        "universe_count": processed,
        "pct_above_50dma": pct50,
        "pct_above_200dma": pct200,
        "new_highs_52w": n_high,
        "new_lows_52w": n_low,
        "net_highs_lows": net_hl,
        "net_highs_lows_pct": net_hl_pct,
        "advances": n_adv,
        "declines": n_dec,
        "ad_net": ad_net,
        "ad_line_cumulative": ad_line,
        # ad_pct is an internal composite input; kept out of BREADTH_KEYS but
        # passed to leadership_proxy below.
        "ad_pct": ad_pct,
    }

    # True exchange-breadth sub-score (additional, clearly separated).
    #
    # When this run used the BROAD ThetaData universe, the tallies above ARE
    # exchange-wide, so we score them directly into a real reading. Otherwise we
    # fall back to the probe (returns "unavailable" with a reason, and the proxy
    # stays the S&P-500-only blend with exchange_score=None) -- unchanged path.
    if (getattr(config, "UNIVERSE_SOURCE", "sp500") == "thetadata"
            and config.EXCHANGE_BREADTH_ENABLED
            and source.startswith("ThetaData")):
        ex_score = exchange_breadth_score_from_tallies(
            n_adv, n_dec, n_high, n_low
        )
        if ex_score is not None:
            ex = {"status": "ok", "score": ex_score,
                  "source": "ThetaData broad universe (issue-level A/D + NH/NL)",
                  "reason": (f"Computed over {processed} broad-universe issues: "
                             f"adv={n_adv} dec={n_dec} nh={n_high} nl={n_low}")}
        else:
            ex = {"status": "unavailable", "score": None,
                  "source": "ThetaData broad universe (issue-level A/D + NH/NL)",
                  "reason": "No advance/decline or NH/NL signal in the universe."}
    else:
        ex = fetch_exchange_breadth()
    exchange_score = ex.get("score") if ex.get("status") == "ok" else None

    score = leadership_proxy(metrics, exchange_score=exchange_score)
    metrics.pop("ad_pct", None)  # not a persisted column
    metrics["exchange_breadth_score"] = (
        round(exchange_score, 1) if exchange_score is not None else None
    )
    metrics["exchange_breadth_status"] = ex.get("status")
    metrics["leadership_proxy"] = score
    metrics["regime"] = classify_regime(score)

    return {
        "status": "ok",
        "universe_source": source,
        "subset": subset,
        "full_universe_count": full_count,
        "metrics": metrics,
        "exchange_breadth": ex,
        "tiingo_authenticated": bool(auth_ok),
        "data_source": (
            "thetadata" if _use_thetadata_prices() else "tiingo"
        ),
        "processed": processed,
        "attempted": len(tickers),
        "from_cache": n_from_cache,
        "from_fetch": n_from_fetch,
    }
