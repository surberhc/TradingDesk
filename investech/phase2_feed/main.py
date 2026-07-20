"""
Phase-2 breadth-based "Leadership Proxy" feed -- runner.

Resolves the S&P 500 universe, pulls licensed Tiingo EOD prices, computes daily
breadth metrics, folds them into a transparent composite that APPROXIMATES (does
not replicate) InvesTech's proprietary NLC behavior, prints a readable summary,
and appends one row to data/breadth_daily.csv (de-duped by date).

Stdlib only (urllib/csv/json). Degrades gracefully if the Tiingo key is absent
(status="needs_api_key").
"""

import os
import sys
import csv
import datetime as _dt

import config
import breadth


def _read_prev_ad_line():
    """Read the most recent cumulative A/D line value from the CSV, or 0.0."""
    if not os.path.exists(config.CSV_PATH):
        return 0.0
    last = None
    try:
        with open(config.CSV_PATH, "r", newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                last = row
    except Exception:
        return 0.0
    if not last:
        return 0.0
    try:
        return float(last.get("ad_line_cumulative") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _progress(i, n, tk):
    # Lightweight inline progress so a long full-universe run shows life.
    if i == 1 or i == n or i % 25 == 0:
        sys.stdout.write(f"\r  fetching {i}/{n} ({tk}) ...        ")
        sys.stdout.flush()
        if i == n:
            sys.stdout.write("\n")


def print_summary(result):
    print("=" * 70)
    print("Phase-2 Leadership Proxy (breadth) -- "
          f"run {_dt.datetime.now().isoformat(timespec='seconds')}")
    print("=" * 70)
    print("NOTE: This is a TRANSPARENT APPROXIMATION of the BEHAVIOR of")
    print("      InvesTech Research's proprietary Negative Leadership Composite")
    print("      (Selling Vacuum vs Distribution). It is NOT a replica and does")
    print("      not reproduce their proprietary internals. Public/licensed")
    print("      (Tiingo) data only -- no InvesTech scraping.")
    print("-" * 70)
    print(f"Universe source : {result['universe_source']}")
    ds = result.get("data_source")
    if ds:
        print(f"Price source    : {ds} "
              f"(config.DATA_SOURCE={config.DATA_SOURCE})")
    print(f"Tiingo key      : {'authenticated' if result.get('tiingo_authenticated') else 'NOT authenticated'}")
    print(f"Constituents    : processed {result.get('processed', 0)} "
          f"of {result.get('attempted', 0)} attempted "
          f"(full universe = {result.get('full_universe_count', 0)})")
    print(f"EOD cache       : {result.get('from_cache', 0)} reused, "
          f"{result.get('from_fetch', 0)} fetched"
          f"{'  [FORCE REFRESH]' if config.FORCE_REFRESH else ''}")
    ex = result.get("exchange_breadth") or {}
    if ex:
        es = result.get("metrics", {}).get("exchange_breadth_score")
        print(f"Exchange breadth: {ex.get('status')} "
              f"(source: {ex.get('source')})"
              + (f" score={es}" if es is not None else ""))
        if ex.get("status") != "ok":
            print(f"                  reason: {ex.get('reason')}")
    if result.get("subset"):
        print("  *** SCAFFOLD LIMITATION: this run used a SUBSET of the universe,")
        print("      NOT the full S&P 500. Breadth %s are over the subset only.")
        print("      Set UNIVERSE_LIMIT=None (or PHASE2_UNIVERSE_LIMIT=0) for")
        print("      full-universe coverage.")
    print(f"Run status      : {result['status']}")
    print("-" * 70)

    m = result.get("metrics") or {}
    if not m:
        print("No breadth metrics computed (see status above).")
        print("=" * 70)
        return

    def show(label, key, suffix=""):
        v = m.get(key)
        v = "" if v is None else v
        print(f"  {label:<34} {v}{suffix}")

    print("BREADTH METRICS:")
    show("Universe count (usable)", "universe_count")
    show("% above 50-day MA", "pct_above_50dma", " %")
    show("% above 200-day MA", "pct_above_200dma", " %")
    show("52w new highs", "new_highs_52w")
    show("52w new lows", "new_lows_52w")
    show("Net new highs - new lows", "net_highs_lows")
    show("Net highs-lows (% of universe)", "net_highs_lows_pct", " %")
    show("Advances", "advances")
    show("Declines", "declines")
    show("A/D net (adv - dec)", "ad_net")
    show("A/D line (cumulative)", "ad_line_cumulative")
    print("-" * 70)
    exb = m.get("exchange_breadth_score")
    exb_status = m.get("exchange_breadth_status")
    if exb is not None:
        print(f"  Exchange breadth sub-score (0-100): {exb}  "
              f"[blended, provisional weight]")
    else:
        print(f"  Exchange breadth sub-score: (none -- {exb_status}); "
              f"proxy is S&P-500 breadth only")
    print("-" * 70)
    print(f"  LEADERSHIP PROXY (0-100): {m.get('leadership_proxy')}")
    print(f"  REGIME                  : {m.get('regime')}")
    print(f"  (Thresholds: >= {config.REGIME_BULL_MIN} bullish, "
          f"<= {config.REGIME_BEAR_MAX} bearish, else neutral)")
    print("=" * 70)


def append_row(result):
    os.makedirs(os.path.dirname(config.CSV_PATH), exist_ok=True)
    today = _dt.date.today().isoformat()
    run_ts = _dt.datetime.now().isoformat(timespec="seconds")

    fieldnames = ["date", "run_timestamp", "status", "subset",
                  "universe_source"] + config.BREADTH_KEYS
    m = result.get("metrics") or {}
    row = {
        "date": today,
        "run_timestamp": run_ts,
        "status": result.get("status", ""),
        "subset": "yes" if result.get("subset") else "no",
        "universe_source": result.get("universe_source", ""),
    }
    for key in config.BREADTH_KEYS:
        v = m.get(key)
        row[key] = "" if v is None else v

    existing = []
    if os.path.exists(config.CSV_PATH):
        with open(config.CSV_PATH, "r", newline="", encoding="utf-8") as f:
            for old in csv.DictReader(f):
                if old.get("date") != today:
                    existing.append(old)

    with open(config.CSV_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for old in existing:
            writer.writerow({fn: old.get(fn, "") for fn in fieldnames})
        writer.writerow(row)
    return config.CSV_PATH


def main():
    prev_ad = _read_prev_ad_line()
    print("Starting Phase-2 breadth pull (this may take a while for the full "
          "universe due to per-ticker Tiingo EOD requests)...\n")
    result = breadth.compute_breadth(prev_ad_line=prev_ad, progress=_progress)
    print()
    print_summary(result)
    path = append_row(result)
    print(f"\nWrote/updated today's row -> {path}")
    if result["status"] == "needs_api_key":
        print("Tiingo key not found -- set TIINGO_API_KEY in the desk .env or "
              "environment, then re-run.")


if __name__ == "__main__":
    main()
