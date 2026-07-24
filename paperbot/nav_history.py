"""
nav_history.py — appends a per-account NAV snapshot to a local CSV every
account_monitor_run.py cycle, so we can compare S0's live paper NAV over time
against the backtest-expected curve (dashboard "S0 Performance vs Model" section,
and the EOD email's one-line since-inception stat).

Purely additive: reads NetLiq that account_monitor_run.py's read-only cycle
already pulls, and writes to a local (off-Drive) CSV — same STATE_DIR convention
as the monitor's other state files (baselines, earmarks). No broker calls here,
no order-path/strategy logic touched.

The live paper test started 2026-07-07 (all 5 accounts brought to target). There
is no way to backfill NAV history before that date — tracking starts accumulating
from whenever this feature first runs forward.

CSV columns: date, account, version, net_liq, buying_power, excess_liquidity
  date            — ISO date string (YYYY-MM-DD), the day of the read-only cycle.
  account         — full account number (e.g. "DU8922142").
  version         — strategy version per config.ENROLLMENT (Conservative/Balanced/Growth).
  net_liq         — NetLiquidation read that day (float). REQUIRED (row skipped if None).
  buying_power    — BuyingPower read that day (float | NaN). OPTIONAL observability.
  excess_liquidity— ExcessLiquidity read that day (float | NaN). OPTIONAL observability.

BP/XL are optional observability (conductor #26 — an EOD margin time series), read from
the SAME accountValues the S0 monitor already pulls (no extra broker call). They may be
absent on a given read (-> NaN); net_liq stays required. A pre-existing CSV written before
these two columns existed is reindexed on read so the old rows simply carry NaN for them —
no migration needed.

Upsert semantics: a row for a given (date, account) is OVERWRITTEN, not
duplicated, if the monitor happens to run more than once on the same day
(e.g. a manual re-run). This runs once a day, so performance doesn't matter —
simplicity wins: read the whole CSV, drop any matching (date, account) rows,
append the new ones, write back.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd

import config

NAV_HISTORY_CSV = Path(config.STATE_DIR) / "nav_history.csv"

COLUMNS = ["date", "account", "version", "net_liq", "buying_power", "excess_liquidity"]


def append_snapshot(today: date, snapshots: list[dict]) -> None:
    """Upsert one row per snapshot into NAV_HISTORY_CSV for `today`.

    Each snap dict must have "account" and "net_liq" (as produced by
    account_monitor_run.read_account_cycle). The version is looked up from
    config.ENROLLMENT — snapshots for accounts not in ENROLLMENT are skipped
    (defensive; every enrolled snapshot should already resolve).

    Snapshots with a missing/None net_liq are skipped (nothing useful to log;
    keeps a bad read from poisoning the history with NaN/garbage).
    """
    today_str = today.isoformat()
    new_rows = []
    for snap in snapshots:
        acct = snap.get("account")
        net_liq = snap.get("net_liq")
        if acct is None or net_liq is None:
            continue
        version = config.ENROLLMENT.get(acct)
        if version is None:
            continue
        # BP/XL are OPTIONAL — store the float when present, else None (-> NaN in CSV).
        # net_liq stays required (row already skipped above if it is None).
        bp = snap.get("buying_power")
        xl = snap.get("excess_liquidity")
        new_rows.append({"date": today_str, "account": acct,
                          "version": version, "net_liq": float(net_liq),
                          "buying_power": float(bp) if bp is not None else None,
                          "excess_liquidity": float(xl) if xl is not None else None})

    if not new_rows:
        return

    NAV_HISTORY_CSV.parent.mkdir(parents=True, exist_ok=True)

    if NAV_HISTORY_CSV.exists():
        existing = pd.read_csv(NAV_HISTORY_CSV, dtype={"date": str, "account": str,
                                                         "version": str})
        # A CSV written before buying_power/excess_liquidity existed lacks those columns.
        # Reindex to the full COLUMNS so the missing ones become NaN before the upsert.
        existing = existing.reindex(columns=COLUMNS)
    else:
        existing = pd.DataFrame(columns=COLUMNS)

    new_df = pd.DataFrame(new_rows, columns=COLUMNS)

    # Drop any existing rows matching (date, account) about to be re-written, then
    # append the fresh rows — this is the "overwrite not duplicate" upsert.
    if not existing.empty:
        key_new = set(zip(new_df["date"], new_df["account"]))
        mask_keep = ~existing.apply(lambda r: (r["date"], r["account"]) in key_new, axis=1)
        existing = existing[mask_keep]

    if existing.empty:
        out = new_df
    else:
        out = pd.concat([existing, new_df], ignore_index=True)
    out = out[COLUMNS]
    out.to_csv(NAV_HISTORY_CSV, index=False)


def load_history() -> pd.DataFrame:
    """Read NAV_HISTORY_CSV. Returns an empty-but-correctly-shaped DataFrame (the full
    COLUMNS) if the file doesn't exist yet — graceful cold start, never raises. A
    pre-existing CSV that lacks the buying_power/excess_liquidity columns is reindexed so
    callers always get the full shape (missing columns -> NaN)."""
    if not NAV_HISTORY_CSV.exists():
        return pd.DataFrame(columns=COLUMNS)
    df = pd.read_csv(NAV_HISTORY_CSV, dtype={"date": str, "account": str, "version": str})
    return df.reindex(columns=COLUMNS)
