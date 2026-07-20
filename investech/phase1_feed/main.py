"""
Phase-1 public market-risk metrics feed -- runner.

Runs every fetcher, prints a readable table to stdout, and appends one row
(one column per metric + a run timestamp) to data/metrics_daily.csv.

Re-running on the same calendar date OVERWRITES that day's row rather than
duplicating it (de-dup by the 'date' column).

Zero third-party dependencies required -- stdlib only.
"""

import os
import csv
import datetime as _dt

# Import the .env fallback loader FIRST. Importing it backfills FRED_API_KEY
# into os.environ (from C:\TradingDesk-Local\secrets\.env) when it is not
# already set, before any fetcher reads the variable. An already-set env var
# always wins -- the loader never overwrites it.
import env_loader  # noqa: F401  (imported for its load-on-import side effect)
import config
from fetchers import FETCHERS


def run_all():
    """Run every fetcher in config order; return list of result dicts."""
    results = []
    for key in config.METRIC_KEYS:
        fn = FETCHERS[key]
        try:
            results.append(fn())
        except Exception as e:  # defensive -- fetchers already guard internally
            results.append({
                "metric": key, "value": None, "as_of_date": None,
                "source": "", "status": f"error: unhandled ({e})",
            })
    return results


def print_table(results):
    """Pretty-print results as an aligned table to stdout."""
    headers = ("METRIC", "VALUE", "AS_OF", "STATUS", "SOURCE")
    rows = []
    for r in results:
        val = "" if r["value"] is None else f"{r['value']}"
        rows.append((
            r["metric"],
            val,
            r["as_of_date"] or "",
            r["status"],
            r["source"],
        ))
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))
    fmt = "  ".join("{:<" + str(w) + "}" for w in widths)
    line = "-" * (sum(widths) + 2 * (len(widths) - 1))
    print(fmt.format(*headers))
    print(line)
    for row in rows:
        print(fmt.format(*[str(c) for c in row]))


def append_row(results):
    """Append/overwrite today's row in the CSV, de-duplicating by date."""
    os.makedirs(os.path.dirname(config.CSV_PATH), exist_ok=True)

    today = _dt.date.today().isoformat()
    run_ts = _dt.datetime.now().isoformat(timespec="seconds")

    # Build this run's row: date, run_timestamp, then one value per metric,
    # plus a parallel <metric>_status column for transparency.
    fieldnames = ["date", "run_timestamp"]
    for key in config.METRIC_KEYS:
        fieldnames.append(key)
        fieldnames.append(key + "_status")

    by_metric = {r["metric"]: r for r in results}
    row = {"date": today, "run_timestamp": run_ts}
    for key in config.METRIC_KEYS:
        r = by_metric.get(key, {})
        row[key] = "" if r.get("value") is None else r["value"]
        row[key + "_status"] = r.get("status", "")

    # Load existing rows (if any), drop today's, keep the rest.
    existing = []
    if os.path.exists(config.CSV_PATH):
        with open(config.CSV_PATH, "r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            # If the schema changed (new metric added), we still read what we can.
            for old in reader:
                if old.get("date") != today:
                    existing.append(old)

    with open(config.CSV_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for old in existing:
            # Fill any missing columns so historical rows stay aligned.
            writer.writerow({fn: old.get(fn, "") for fn in fieldnames})
        writer.writerow(row)

    return config.CSV_PATH


def main():
    print(f"Phase-1 market-risk feed -- run {_dt.datetime.now().isoformat(timespec='seconds')}\n")
    results = run_all()
    print_table(results)
    path = append_row(results)
    ok = sum(1 for r in results if r["status"] == "ok")
    print(f"\nWrote/updated today's row -> {path}")
    print(f"{ok}/{len(results)} fetchers returned 'ok'.")


if __name__ == "__main__":
    main()
