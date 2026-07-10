"""
S8 replication test — Phase 1 (calibration).

Deterministic replay of REAL trades: for each MATCHED row in
combo_ledger_tat_joined.csv, take the REAL entry date+time+template straight
from the ledger (no invented schedule, no firing-probability draw) and feed it
directly into s8_mechanical_simulator's actual per-trade engine
(simulate_trade / estimate_spot / _load_day). This isolates the question
"does the core strike-selection + fill/exit engine reproduce known real
trades?" from the separate (and previously wrong) question of "what schedule
would the simulator invent on its own?"

Read-only against s8_mechanical_simulator.py -- no edits to that file.

Usage (calibration subset only, see __main__ at bottom):
    python s8_replication_test.py
"""

from __future__ import annotations

import re
import time

import pandas as pd

from s8_mechanical_simulator import TEMPLATES, simulate_trade, estimate_spot, _load_day

LEDGER_CSV = "combo_ledger_tat_joined.csv"
OUTPUT_CSV = "s8_replication_test_results.csv"

# TEMPLATES is keyed by short codes ("Puts-80-$4"), but the ledger's
# tat_Template column holds the full Template.name string ("British IC -
# Puts - 80 - $4"), sometimes with stray leading/trailing whitespace
# (confirmed: 'British IC - Calls - 80 - $4 ' and 'British IC - Puts - 50 -
# $2 ' both carry a trailing space in the raw CSV). Build a name->Template
# lookup keyed on the stripped Template.name instead of trusting TEMPLATES
# dict keys to equal tat_Template verbatim.
NAME_TO_TEMPLATE = {t.name.strip(): t for t in TEMPLATES.values()}

LONG_STRIKE_RE = re.compile(r"\((\d+)\)")  # "[np.int64(6975)]" -> 6975; NOT a bare \d+ (matches "64" in "int64" first)


def parse_long_strike(s: str) -> float | None:
    m = LONG_STRIKE_RE.search(str(s))
    if m is None:
        return None
    return float(m.group(1))


def load_matched_rows() -> pd.DataFrame:
    df = pd.read_csv(LEDGER_CSV)
    m = df[df["tat_match"] == "MATCHED"].copy()
    return m


def build_trade_spec(row: pd.Series) -> dict | None:
    """Derive everything simulate_trade() needs from one real ledger row.
    Returns None (with a reason) if the row can't be used."""
    date_str = str(int(row["TradeDate"]))  # YYYYMMDD

    tmpl_name_raw = row["tat_Template"]
    template = NAME_TO_TEMPLATE.get(str(tmpl_name_raw).strip())
    if template is None:
        return {"skip_reason": f"no TEMPLATES match for tat_Template={tmpl_name_raw!r}"}

    open_dt = str(row["short_open_dt"])  # e.g. "2025-12-31 10:07:04"
    try:
        hhmm = open_dt.split(" ")[1][:5]  # floor to minute, "HH:MM"
    except Exception:
        return {"skip_reason": f"unparseable short_open_dt={open_dt!r}"}

    real_long_strike = parse_long_strike(row["long_strikes"])
    if real_long_strike is None:
        return {"skip_reason": f"unparseable long_strikes={row['long_strikes']!r}"}

    qty = row["short_open_qty"]
    if pd.isna(qty) or qty == 0:
        return {"skip_reason": f"bad short_open_qty={qty!r}"}
    real_pnl_per_spread = row["total_realized_pnl"] / abs(qty)

    return {
        "date_str": date_str,
        "template": template,
        "template_name": template.name,
        "entry_hhmm": hhmm,
        "real_short_strike": float(row["short_strike"]),
        "real_long_strike": real_long_strike,
        "real_pnl_per_spread": real_pnl_per_spread,
        "skip_reason": None,
    }


def run(rows: pd.DataFrame) -> tuple[list[dict], dict]:
    """Run the replication for `rows` (already-filtered MATCHED subset).
    Groups by TradeDate so _load_day() is called once per date and quote0
    is reused across every trade on that date. Returns (output_rows, timing_stats).
    """
    output_rows: list[dict] = []
    timing = {"load_time_total": 0.0, "load_count": 0, "sim_time_total": 0.0, "sim_count": 0}

    specs = []
    skipped = 0
    for _, row in rows.iterrows():
        spec = build_trade_spec(row)
        if spec is None or spec.get("skip_reason"):
            skipped += 1
            if spec is not None:
                output_rows.append({
                    "date": row.get("TradeDate"),
                    "template": row.get("tat_Template"),
                    "skipped": True,
                    "skip_reason": spec["skip_reason"],
                })
            continue
        specs.append(spec)

    specs_by_date: dict[str, list[dict]] = {}
    for spec in specs:
        specs_by_date.setdefault(spec["date_str"], []).append(spec)

    for date_str, day_specs in specs_by_date.items():
        t0 = time.perf_counter()
        try:
            ohlc0, quote0 = _load_day(date_str)
        except Exception as e:
            for spec in day_specs:
                output_rows.append({
                    "date": date_str, "template": spec["template_name"],
                    "real_entry_time": spec["entry_hhmm"], "skipped": True,
                    "skip_reason": f"_load_day exception: {e!r}",
                })
            continue
        t1 = time.perf_counter()
        timing["load_time_total"] += (t1 - t0)
        timing["load_count"] += 1

        if quote0.empty:
            for spec in day_specs:
                output_rows.append({
                    "date": date_str, "template": spec["template_name"],
                    "real_entry_time": spec["entry_hhmm"], "skipped": True,
                    "skip_reason": "empty quote0 for date",
                })
            continue

        for spec in day_specs:
            ts = f"{date_str[0:4]}-{date_str[4:6]}-{date_str[6:8]}T{spec['entry_hhmm']}:00.000"

            t2 = time.perf_counter()
            spot_at_entry = estimate_spot(quote0, ts)
            sim_result = None
            sim_exc = None
            if spot_at_entry != spot_at_entry:  # NaN check, no math import needed
                sim_exc = "estimate_spot returned NaN (no quote snapshot at that minute)"
            else:
                try:
                    sim_result = simulate_trade(quote0, date_str, spec["template"], spec["entry_hhmm"], spot_at_entry)
                except Exception as e:
                    sim_exc = f"simulate_trade exception: {e!r}"
            t3 = time.perf_counter()
            timing["sim_time_total"] += (t3 - t2)
            timing["sim_count"] += 1

            real_short = spec["real_short_strike"]
            real_long = spec["real_long_strike"]
            real_pnl = spec["real_pnl_per_spread"]

            if sim_result is None:
                output_rows.append({
                    "date": date_str,
                    "template": spec["template_name"],
                    "real_entry_time": spec["entry_hhmm"],
                    "spot_at_entry": spot_at_entry,
                    "real_short_strike": real_short,
                    "sim_short_strike": None,
                    "real_long_strike": real_long,
                    "sim_long_strike": None,
                    "real_pnl_per_spread": real_pnl,
                    "sim_pnl_per_spread": None,
                    "pnl_diff": None,
                    "short_strike_diff": None,
                    "long_strike_diff": None,
                    "sim_returned_none": True,
                    "none_reason": sim_exc or "simulate_trade returned None (no credit / no strikes found)",
                })
                continue

            sim_short = sim_result.short_strike
            sim_long = sim_result.long_strike
            sim_pnl = sim_result.pnl_per_spread

            output_rows.append({
                "date": date_str,
                "template": spec["template_name"],
                "real_entry_time": spec["entry_hhmm"],
                "spot_at_entry": spot_at_entry,
                "real_short_strike": real_short,
                "sim_short_strike": sim_short,
                "real_long_strike": real_long,
                "sim_long_strike": sim_long,
                "real_pnl_per_spread": real_pnl,
                "sim_pnl_per_spread": sim_pnl,
                "pnl_diff": (sim_pnl - real_pnl) if pd.notna(sim_pnl) else None,
                "short_strike_diff": (sim_short - real_short) if sim_short is not None else None,
                "long_strike_diff": (sim_long - real_long) if sim_long is not None else None,
                "sim_returned_none": False,
                "none_reason": "",
                "sim_exit_reason": sim_result.exit_reason,
            })

    timing["skipped_rows"] = skipped
    timing["distinct_dates"] = len(specs_by_date)
    timing["trades_attempted"] = sum(len(v) for v in specs_by_date.values())
    return output_rows, timing


if __name__ == "__main__":
    import sys

    all_matched = load_matched_rows()

    if "--calib" in sys.argv:
        # --- Phase 1 calibration: first 20 MATCHED rows by row order ---
        rows_to_run = all_matched.head(20)
        label = "CALIBRATION (first 20 MATCHED rows)"
    else:
        # --- Phase 2: full MATCHED set ---
        rows_to_run = all_matched
        label = f"FULL RUN ({len(all_matched)} MATCHED rows)"

    t_start = time.perf_counter()
    out_rows, timing = run(rows_to_run)
    t_end = time.perf_counter()
    elapsed = t_end - t_start

    out_df = pd.DataFrame(out_rows)
    out_df.to_csv(OUTPUT_CSV, index=False)

    print(f"=== {label} ===")
    print(f"Elapsed wall-clock: {elapsed:.3f}s")
    print(f"Distinct dates loaded: {timing['distinct_dates']}")
    print(f"Trades attempted: {timing['trades_attempted']}")
    print(f"Rows skipped before load (bad template/parse): {timing['skipped_rows']}")
    if timing["load_count"] > 0:
        print(f"Total _load_day time: {timing['load_time_total']:.3f}s "
              f"({timing['load_time_total']/timing['load_count']:.3f}s/date avg)")
    if timing["sim_count"] > 0:
        print(f"Total estimate_spot+simulate_trade time: {timing['sim_time_total']:.3f}s "
              f"({timing['sim_time_total']/timing['sim_count']:.3f}s/trade avg)")

    # sanity eyeball
    valid = out_df[out_df.get("sim_returned_none") == False] if "sim_returned_none" in out_df.columns else pd.DataFrame()
    n_none = int((out_df.get("sim_returned_none") == True).sum()) if "sim_returned_none" in out_df.columns else 0
    print(f"\nsimulate_trade returned None for {n_none} / {len(out_df)} output rows")
    if len(valid) > 0:
        print(f"mean abs short_strike_diff: {valid['short_strike_diff'].abs().mean():.2f}")
        print(f"mean abs long_strike_diff: {valid['long_strike_diff'].abs().mean():.2f}")
        print(f"mean abs pnl_diff: {valid['pnl_diff'].abs().mean():.2f}")
    print(f"\nWrote {len(out_df)} rows to {OUTPUT_CSV}")
