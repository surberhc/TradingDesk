"""
download_data.py — Fetch historical adjusted prices from Tiingo into data/.

Built to DATA.md. Reads TIINGO_API_KEY from .env via python-dotenv / os.environ
(the key is NEVER hard-coded, printed, or logged). Pulls every ticker in
config.ALL_TICKERS (plus the HYG credit-proxy extra) for the configured date
range, writes one Parquet file per ticker plus data/_manifest.json, attempts a
real US Treasury 10y par-yield series (falling back to a clearly-labeled ETF
proxy), runs the DATA.md quality checks, and prints a plain-English summary.

Safe to re-run: each run overwrites the per-ticker files and rebuilds a fresh
manifest. Re-downloading is an explicit action (running this script) — the
backtest never triggers it as a side effect.

Run from the project root, with the venv active:
    python -m src.download_data
"""

from __future__ import annotations

import json
import os
import time
from datetime import date, datetime, timezone
from io import StringIO
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv

from connections import fred as shared_fred
from strategies import config

# Resolve paths relative to the project root (this file lives in src/).
PROJECT_ROOT = Path(__file__).resolve().parent.parent
# DATA_DIR/MANIFEST_FILE may be an absolute local path (data moved off Drive) or a
# project-relative one; resolve absolute paths as-is, else relative to the project.
_d = Path(config.DATA_DIR)
DATA_PATH = _d if _d.is_absolute() else PROJECT_ROOT / _d
_m = Path(config.MANIFEST_FILE)
MANIFEST_PATH = _m if _m.is_absolute() else PROJECT_ROOT / _m

# Macro-proxy extras: tickers config references for signals but that are not
# part of the tradeable universe (config.ALL_TICKERS). Downloaded and labeled
# so the credit/yield proxies can be built later (DATA.md §"Also needed").
MACRO_PROXY_TICKERS = ["HYG"]  # high-yield ETF for the HY-vs-Treasury credit proxy

# Custom-allocation extras: tickers that appear ONLY in an ANDREW-AUTHORED CRM allocation
# (paperbot/custom_target.py), never in a computed S0 book. They are downloaded because the
# desk cannot size an order for a ticker it has no price history for (data_loader.load_prices
# raises KeyError and the whole custom target fails closed).
#
# They are deliberately NOT in config.ALL_TICKERS. That list is S0's OWN tradeable universe
# and feeds the validated backtest engine, so putting an instrument in it would change S0's
# model. This union — the DOWNLOAD universe only — is the single place they enter anything.
CUSTOM_ALLOCATION_TICKERS = list(getattr(config, "CUSTOM_ALLOCATION_TICKERS", []))

# Be a good citizen on the free tier: a short pause between symbol requests.
_REQUEST_PAUSE_SECONDS = 0.8
_HTTP_TIMEOUT_SECONDS = 30


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------
def _get_api_key() -> str:
    """Read TIINGO_API_KEY from .env / environment. Never returns it to logs."""
    load_dotenv(PROJECT_ROOT / config.ENV_FILE)
    key = os.environ.get("TIINGO_API_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "TIINGO_API_KEY is not set. Add it to .env as TIINGO_API_KEY=... "
            "(see DATA.md). It is never printed or committed."
        )
    return key


# ---------------------------------------------------------------------------
# Tiingo price download
# ---------------------------------------------------------------------------
def _fetch_ticker(symbol: str, start: str, end: str, key: str) -> pd.DataFrame | None:
    """
    Download one ticker's daily adjusted close from Tiingo.

    Returns a DataFrame indexed by date with a single column named `symbol`
    (the adjusted close), or None if the symbol is unavailable. Raises on a
    rate-limit so the caller can pause and report rather than silently lose data.
    """
    url = f"{config.TIINGO_BASE_URL}/{symbol}/prices"
    params = {
        "startDate": start,
        "endDate": end,
        "format": "json",
        "columns": f"date,{config.PRICE_FIELD}",  # adjClose: split- AND dividend-adjusted
        "token": key,
    }
    resp = requests.get(url, params=params, timeout=_HTTP_TIMEOUT_SECONDS)

    if resp.status_code == 429:
        # Free-tier rate limit hit — surface clearly, do not crash mid-loop.
        raise RuntimeError(
            f"Tiingo rate limit hit while fetching {symbol}. "
            "Free tier allows ~50 symbols/hour; wait and re-run."
        )
    if resp.status_code == 404:
        return None  # symbol not on Tiingo's free tier — caller warns and continues
    resp.raise_for_status()

    rows = resp.json()
    if not rows:
        return None

    frame = pd.DataFrame(rows)
    # Tiingo dates are ISO timestamps; keep date only, drop tz.
    frame["date"] = pd.to_datetime(frame["date"]).dt.tz_localize(None).dt.normalize()
    frame = frame.rename(columns={config.PRICE_FIELD: symbol})
    frame = frame[["date", symbol]].dropna().set_index("date").sort_index()
    return frame


def _save_series(symbol: str, frame: pd.DataFrame) -> Path:
    """Write one ticker's price series to data/ in the configured format."""
    if config.STORAGE_FORMAT == "parquet":
        path = DATA_PATH / f"{symbol}.parquet"
        frame.to_parquet(path)
    else:
        path = DATA_PATH / f"{symbol}.csv"
        frame.to_csv(path)
    return path


# ---------------------------------------------------------------------------
# Treasury 10y yield — real source first, labeled ETF proxy as fallback
# ---------------------------------------------------------------------------
def _fetch_treasury_10y(start: str, end: str) -> tuple[pd.DataFrame | None, str]:
    """
    Try the US Treasury's public daily par-yield data for the 10-year rate.

    Returns (DataFrame[date -> 'us_treasury_10y'], source_label). On any failure
    returns (None, reason) so the caller can fall back to the ETF proxy and label
    it clearly. No API key is required for this public dataset.
    """
    start_year = pd.Timestamp(start).year
    end_year = (pd.Timestamp(end).year if end else datetime.now(timezone.utc).year)
    base = (
        "https://home.treasury.gov/resource-center/data-chart-center/"
        "interest-rates/daily-treasury-rates.csv/"
    )

    pieces: list[pd.DataFrame] = []
    for year in range(start_year, end_year + 1):
        url = f"{base}{year}/all"
        params = {
            "type": "daily_treasury_yield_curve",
            "field_tdr_date_value": str(year),
            "_format": "csv",
        }
        try:
            resp = requests.get(url, params=params, timeout=_HTTP_TIMEOUT_SECONDS)
            resp.raise_for_status()
            year_df = pd.read_csv(StringIO(resp.text))
        except Exception:
            # Treasury endpoint changed or unreachable — abandon the real source.
            return None, "treasury_fetch_failed"

        # Column for the 10-year par yield is labeled "10 Yr".
        ten_yr_col = next((c for c in year_df.columns if c.strip() == "10 Yr"), None)
        date_col = next((c for c in year_df.columns if c.strip().lower() == "date"), None)
        if ten_yr_col is None or date_col is None:
            return None, "treasury_schema_changed"

        slice_df = year_df[[date_col, ten_yr_col]].copy()
        slice_df.columns = ["date", "us_treasury_10y"]
        pieces.append(slice_df)
        time.sleep(_REQUEST_PAUSE_SECONDS)

    if not pieces:
        return None, "treasury_no_data"

    combined = pd.concat(pieces, ignore_index=True)
    combined["date"] = pd.to_datetime(combined["date"]).dt.normalize()
    combined = (
        combined.dropna()
        .drop_duplicates(subset="date")
        .set_index("date")
        .sort_index()
    )
    combined = combined.loc[combined.index >= pd.Timestamp(start)]
    if combined.empty:
        return None, "treasury_empty_after_filter"
    return combined, "us_treasury_par_yield"


def _save_yield(frame: pd.DataFrame, source_label: str) -> Path:
    """Persist the 10y yield (real or proxy) with its source label embedded."""
    out = frame.copy()
    out.attrs["source"] = source_label
    path = DATA_PATH / "_treasury_10y.parquet"
    out.to_parquet(path)
    return path


# ---------------------------------------------------------------------------
# Real macro upgrades: VIX (CBOE, no key) and HY credit spread (FRED, needs key)
# ---------------------------------------------------------------------------
def _fetch_vix(start: str) -> tuple[pd.DataFrame | None, str]:
    """CBOE published daily VIX close (public, no key). Returns (frame, source)."""
    url = "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv"
    try:
        resp = requests.get(url, timeout=_HTTP_TIMEOUT_SECONDS)
        resp.raise_for_status()
        df = pd.read_csv(StringIO(resp.text))
    except Exception:
        return None, "vix_fetch_failed"
    close_col = next((c for c in df.columns if c.strip().upper() == "CLOSE"), None)
    date_col = next((c for c in df.columns if c.strip().upper() == "DATE"), None)
    if close_col is None or date_col is None:
        return None, "vix_schema_changed"
    out = df[[date_col, close_col]].copy()
    out.columns = ["date", "vix"]
    out["date"] = pd.to_datetime(out["date"]).dt.normalize()
    out = out.dropna().set_index("date").sort_index()
    out = out.loc[out.index >= pd.Timestamp(start)]
    return (out, "cboe_vix") if not out.empty else (None, "vix_empty")


def _fetch_hy_oas(start: str) -> tuple[pd.DataFrame | None, str]:
    """
    ICE BofA US High Yield OAS (FRED series BAMLH0A0HYM2) — full history requires a
    free FRED API key in .env as FRED_API_KEY. Without a key we return None so the
    credit signal falls back to the labeled HYG/IEF proxy (the key-free FRED graph
    CSV only serves ~3 recent years, which would corrupt older backtests).
    """
    try:
        series = shared_fred.fetch_series("BAMLH0A0HYM2", start=start)
    except RuntimeError:
        return None, "no_fred_key"
    except Exception:
        return None, "fred_fetch_failed"
    if series.empty:
        return None, "fred_empty"
    out = series.to_frame(name="hy_oas")
    out.index = pd.to_datetime(out.index).normalize()
    out.index.name = "date"
    out["hy_oas"] = pd.to_numeric(out["hy_oas"], errors="coerce")
    out = out.dropna().sort_index()
    return (out, "fred_BAMLH0A0HYM2") if not out.empty else (None, "fred_empty")


def _save_macro(frame: pd.DataFrame, filename: str, source_label: str) -> Path:
    out = frame.copy()
    out.attrs["source"] = source_label
    path = DATA_PATH / filename
    out.to_parquet(path)
    return path


# ---------------------------------------------------------------------------
# Data-quality checks (DATA.md §"Data-quality checks")
# ---------------------------------------------------------------------------
def _quality_check(symbol: str, frame: pd.DataFrame) -> list[str]:
    """Return a list of human-readable QC flags for one ticker (empty = clean)."""
    flags: list[str] = []
    prices = frame[symbol]

    # Zero or negative prices — a hard data error.
    if (prices <= 0).any():
        n = int((prices <= 0).sum())
        flags.append(f"{n} zero/negative price(s)")

    # Suspicious single-day moves: possible unadjusted split.
    daily_ret = prices.pct_change()
    big = daily_ret.abs() > config.QC_MAX_SINGLE_DAY_MOVE
    if big.any():
        worst = daily_ret[big].abs().max()
        flags.append(
            f"{int(big.sum())} day(s) move >"
            f"{config.QC_MAX_SINGLE_DAY_MOVE:.0%} (worst {worst:.0%}) — check split adj"
        )

    # Stale prices: same value many days running.
    run = (prices.diff() == 0)
    if run.any():
        # Longest run of consecutive unchanged prices.
        groups = (~run).cumsum()
        longest = run.groupby(groups).sum().max()
        if longest >= config.QC_STALE_PRICE_RUN:
            flags.append(f"stale run of {int(longest)} identical prices")

    # Calendar gaps within active life (business days only, to avoid weekends).
    bdays = pd.bdate_range(prices.index.min(), prices.index.max())
    missing = bdays.difference(prices.index)
    if len(missing) > 0:
        # Collapse to runs and flag any gap longer than the threshold.
        gap_lengths = _max_consecutive_gap(prices.index, bdays)
        if gap_lengths > config.QC_MAX_GAP_DAYS:
            flags.append(
                f"{len(missing)} missing business day(s); "
                f"longest gap {gap_lengths} days"
            )
    return flags


def _max_consecutive_gap(have: pd.DatetimeIndex, expected: pd.DatetimeIndex) -> int:
    """Longest run of consecutive expected business days that are missing."""
    have_set = set(have)
    longest = current = 0
    for day in expected:
        if day in have_set:
            current = 0
        else:
            current += 1
            longest = max(longest, current)
    return longest


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def download_universe() -> list[str]:
    """Every symbol a FULL run downloads, in order: S0's own universe, then the
    custom-allocation extras, then the macro proxies. Deduplicated, order preserved.

    S0's universe (config.ALL_TICKERS) is passed through UNTOUCHED — the extras are unioned
    on HERE, in the downloader, and never inside config.ALL_TICKERS itself, so nothing the
    backtest engine reads changes."""
    ordered = list(config.ALL_TICKERS) + CUSTOM_ALLOCATION_TICKERS + MACRO_PROXY_TICKERS
    return list(dict.fromkeys(ordered))


def _role_for(symbol: str) -> str:
    """The manifest `role` for one symbol."""
    if symbol in MACRO_PROXY_TICKERS:
        return "macro_proxy"
    if symbol in CUSTOM_ALLOCATION_TICKERS:
        return "custom_allocation"
    return "universe"


def _load_manifest_tickers() -> dict[str, dict]:
    """The existing manifest's per-ticker block, or {} if there is none / it is unreadable.
    Only used by a TARGETED (`only=`) run, which must not erase the other 40+ entries."""
    try:
        return dict(json.loads(MANIFEST_PATH.read_text()).get("tickers", {}))
    except Exception:  # noqa: BLE001 — no manifest yet, or corrupt: start clean
        return {}


def main(only: list[str] | None = None) -> None:
    """Download tickers, write the manifest, run QC, print a summary.

    `only` restricts the run to the named symbols — a TARGETED refresh (e.g. adding a new
    custom-allocation ticker) that must not re-pull, and therefore cannot disturb, the 40+
    series already on disk. In that mode the existing manifest is MERGED into rather than
    replaced, and the macro series (Treasury 10y / VIX / HY OAS) are left exactly as they
    are. `only=None` (the default, and every scheduled caller) is the unchanged full run."""
    # Windows cmd defaults to cp1252; force UTF-8 so console prints never crash.
    try:
        import sys
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    key = _get_api_key()
    DATA_PATH.mkdir(parents=True, exist_ok=True)

    start = config.DATA_START
    end = config.BACKTEST_END or date.today().isoformat()

    targeted = only is not None
    if targeted:
        universe = list(dict.fromkeys(str(s).strip().upper() for s in only if str(s).strip()))
    else:
        universe = download_universe()
    print(f"Downloading {len(universe)} tickers from {start} to {end} "
          f"{'(TARGETED refresh — every other series on disk is left untouched)' if targeted else ''}...\n")

    manifest: dict[str, dict] = _load_manifest_tickers() if targeted else {}
    skipped: list[str] = []
    all_flags: dict[str, list[str]] = {}
    now_iso = datetime.now(timezone.utc).isoformat()

    for symbol in universe:
        try:
            frame = _fetch_ticker(symbol, start, end, key)
        except RuntimeError as exc:
            # Rate limit or hard error: stop cleanly with a clear message.
            print(f"\nSTOPPED: {exc}")
            print("Partial data may be on disk. Re-run after the limit resets.")
            return

        if frame is None or frame.empty:
            print(f"  ! {symbol}: unavailable on Tiingo — skipped")
            skipped.append(symbol)
            time.sleep(_REQUEST_PAUSE_SECONDS)
            continue

        _save_series(symbol, frame)
        flags = _quality_check(symbol, frame)
        if flags:
            all_flags[symbol] = flags

        role = _role_for(symbol)
        manifest[symbol] = {
            "first_date": frame.index.min().date().isoformat(),
            "last_date": frame.index.max().date().isoformat(),
            "rows": int(len(frame)),
            "downloaded_at": now_iso,
            "source": "tiingo_adjClose",
            "role": role,
            "qc_flags": flags,
        }
        tag = ("" if role == "universe"
               else " (macro proxy)" if role == "macro_proxy"
               else " (custom allocation)")
        print(
            f"  + {symbol}{tag}: {len(frame)} rows, "
            f"{manifest[symbol]['first_date']} -> {manifest[symbol]['last_date']}"
            + (f"  [{len(flags)} QC flag(s)]" if flags else "")
        )
        time.sleep(_REQUEST_PAUSE_SECONDS)

    if targeted:
        # A targeted refresh touches ONLY the named tickers. The macro series
        # (_treasury_10y / _vix / _hy_oas) are left byte-for-byte as they are, and their
        # manifest entries survive because the manifest was merged, not rebuilt.
        print("\nTARGETED refresh: macro series (Treasury 10y / VIX / HY credit spread) "
              "left untouched.")
    else:
        _refresh_macro_series(start, end, manifest, now_iso)

    # --- Write manifest ---
    manifest_payload = {
        "generated_at": now_iso,
        "data_start": start,
        "data_end": end,
        "tickers": manifest,
    }
    MANIFEST_PATH.write_text(json.dumps(manifest_payload, indent=2))

    # --- Summary ---
    downloaded = [s for s in universe if s not in skipped]
    earliest = min(
        (m["first_date"] for m in manifest.values() if "first_date" in m),
        default="n/a",
    )
    print("\n" + "=" * 60)
    print("DOWNLOAD SUMMARY")
    print("=" * 60)
    print(f"Downloaded : {len(downloaded)} tickers")
    print(f"Earliest   : {earliest}")
    if skipped:
        print(f"Skipped    : {', '.join(skipped)} (unavailable)")
    if all_flags:
        print(f"QC flags   : {len(all_flags)} ticker(s) flagged —")
        for sym, flags in all_flags.items():
            print(f"             {sym}: {'; '.join(flags)}")
        print(
            "\nReview the flags above. Do NOT run a backtest on a dataset with "
            "unresolved critical errors (zero/negative prices, bad splits)."
        )
    else:
        print("QC flags   : none — data looks clean")
    print(f"\nManifest   : {MANIFEST_PATH}")
    print("data/ is now READ-ONLY. Re-run this script to refresh.")


def _refresh_macro_series(start: str, end: str, manifest: dict, now_iso: str) -> None:
    """Fetch the macro series (Treasury 10y, VIX, HY credit spread) and record them in
    `manifest`. Unchanged FULL-run behavior, lifted verbatim into a function so a targeted
    ticker refresh can skip it without re-pulling or re-labeling anything."""
    # --- Treasury 10y yield: real source first, labeled proxy fallback ---
    print("\nFetching 10-year Treasury yield ...")
    yield_df, source_label = _fetch_treasury_10y(start, end)
    if yield_df is not None:
        _save_yield(yield_df, source_label)
        manifest["_treasury_10y"] = {
            "first_date": yield_df.index.min().date().isoformat(),
            "last_date": yield_df.index.max().date().isoformat(),
            "rows": int(len(yield_df)),
            "downloaded_at": now_iso,
            "source": source_label,
            "role": "macro_real",
            "qc_flags": [],
        }
        print(f"  + real US Treasury par yield: {len(yield_df)} rows")
    else:
        # TODO(data-upgrade): wire a robust real 10y source. For now the duration
        # engine will use the IEF price-trend proxy (config.YIELD_PROXY_TICKER),
        # which is already downloaded as part of the universe.
        manifest["_treasury_10y"] = {
            "source": f"PROXY:{config.YIELD_PROXY_TICKER}_price_trend",
            "reason": source_label,
            "role": "macro_proxy",
            "note": "Real Treasury par-yield unavailable; using IEF trend proxy.",
        }
        print(
            f"  ! real Treasury yield unavailable ({source_label}); "
            f"duration engine will use {config.YIELD_PROXY_TICKER} trend PROXY"
        )

    # --- Real macro upgrades: VIX (no key) and HY credit spread (FRED key) ---
    print("\nFetching macro upgrades (VIX, HY credit spread) ...")
    vix_df, vix_src = _fetch_vix(start)
    if vix_df is not None:
        _save_macro(vix_df, "_vix.parquet", vix_src)
        manifest["_vix"] = {
            "first_date": vix_df.index.min().date().isoformat(),
            "last_date": vix_df.index.max().date().isoformat(),
            "rows": int(len(vix_df)), "downloaded_at": now_iso,
            "source": vix_src, "role": "macro_real", "qc_flags": [],
        }
        print(f"  + real VIX (CBOE): {len(vix_df)} rows")
    else:
        manifest["_vix"] = {"source": f"PROXY:{config.VIX_PROXY}", "reason": vix_src,
                            "role": "macro_proxy"}
        print(f"  ! VIX unavailable ({vix_src}); using SPY realized-vol PROXY")

    oas_df, oas_src = _fetch_hy_oas(start)
    if oas_df is not None:
        _save_macro(oas_df, "_hy_oas.parquet", oas_src)
        manifest["_hy_oas"] = {
            "first_date": oas_df.index.min().date().isoformat(),
            "last_date": oas_df.index.max().date().isoformat(),
            "rows": int(len(oas_df)), "downloaded_at": now_iso,
            "source": oas_src, "role": "macro_real", "qc_flags": [],
        }
        print(f"  + real HY credit spread (FRED): {len(oas_df)} rows")
    else:
        proxy = "/".join(config.CREDIT_PROXY)
        manifest["_hy_oas"] = {"source": f"PROXY:{proxy}", "reason": oas_src,
                               "role": "macro_proxy"}
        note = "add a free FRED_API_KEY to .env to upgrade" if oas_src == "no_fred_key" else oas_src
        print(f"  ! HY credit spread unavailable ({note}); using {proxy} ratio PROXY")


def cli(argv: list[str] | None = None) -> None:
    """CLI entry. No arguments -> the unchanged FULL download. `--only SYM[,SYM...]`
    (repeatable) -> a targeted refresh of just those tickers."""
    import sys as _sys

    argv = _sys.argv[1:] if argv is None else list(argv)
    only: list[str] = []
    i = 0
    while i < len(argv):
        if argv[i] == "--only" and i + 1 < len(argv):
            only.extend(s for s in argv[i + 1].replace(" ", ",").split(",") if s)
            i += 2
        else:
            i += 1
    main(only=only or None)


if __name__ == "__main__":
    cli()
