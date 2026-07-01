r"""
s6_validate.py — honest validation of the intraday recon (s6_recon) vs EOD ground truth.

PAPER / research only. READ-ONLY on both warehouses.

THE HONESTY CHECK. The 1-minute warehouse has no spot/greeks; s6_recon rebuilds them
from quotes. Here we check that reconstruction against the EOD options warehouse
(`...\raw\options\SPXW\{YYYYMMDD}.parquet`), which DOES store per-strike `delta`,
`implied_vol`, `gamma`, and `underlying_price` at the EOD snapshot.

Two separate checks, because of one unavoidable data fact:

  * SPOT (underlying_price) is the SAME number for every expiration, and is meaningful.
    => We validate the recovered 0DTE spot at the late-day minute directly against the
       EOD `underlying_price`.

  * 0DTE DELTA at the EOD snapshot is DEGENERATE. EOD is stamped ~16:02 ET, i.e. at/after
    PM settlement, so the 0DTE time-to-expiry is ~0 and every stored 0DTE delta collapses
    to a 0/1 step. EOD 0DTE delta is therefore USELESS as ground truth for a 14:00 delta.
    => To validate the DELTA METHOD honestly we compare against a LONGER-DATED expiration
       (default ~30 calendar DTE) that is ALSO present in the 1-minute file. There the EOD
       greeks are non-degenerate, so a clean match proves our BS delta pipeline (parity
       spot -> per-strike IV inversion -> BS delta) is sound. We then state plainly the
       residual caveat that 0DTE-specific delta cannot be EOD-validated from this data.

Output: a tidy per-day, per-strike error frame + a printed summary (median / 95th-pct
absolute errors in spot points and in delta).
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path

import numpy as np
import pandas as pd

import s5_intraday_data as s5
import s6_recon as recon

EOD_SPXW_DIR = Path(r"C:\TradingDesk-Local\warehouse\raw\options\SPXW")

# The late-day minute we recover spot/delta at for validation. 15:59 is the last fully
# quoted regular-session minute; close to the EOD ~16:02 stamp so spot should line up.
VALIDATION_MINUTE = _dt.time(15, 59)
# For the delta method check, pick the available intraday expiration nearest this many
# calendar days out (non-0DTE => EOD greeks are meaningful). Declared, not tuned.
DELTA_CHECK_TARGET_DTE = 30
# Only compare deltas where the EOD delta is in a band that actually matters for strike
# selection (we pick ~0.15-delta shorts). Excluding deep-ITM/0-delta noise is honest, not
# cherry-picking: those legs are irrelevant to the strategy AND quoted as $0.025 stubs.
_DELTA_BAND = (0.03, 0.60)


def _eod_path(d: _dt.date) -> Path:
    return EOD_SPXW_DIR / f"{d.strftime('%Y%m%d')}.parquet"


def _load_eod(d: _dt.date) -> pd.DataFrame | None:
    p = _eod_path(d)
    if not p.is_file():
        return None
    return pd.read_parquet(p)


def _minute_snap(nbbo: pd.DataFrame, minute: pd.Timestamp) -> pd.DataFrame:
    return nbbo[nbbo["minute"] == minute][["strike", "right", "bid", "ask"]].copy()


def validate_day(d: _dt.date) -> dict | None:
    """Validate spot + delta-method reconstruction for one day.

    Returns a dict with scalar spot error and a per-strike delta-error DataFrame, or
    None if the day cannot be validated (missing EOD, no chain, etc.).
    """
    eod = _load_eod(d)
    if eod is None or eod.empty:
        return None

    minute = pd.Timestamp(_dt.datetime.combine(d, VALIDATION_MINUTE))
    eod_spot = float(eod["underlying_price"].dropna().iloc[0])

    # --- SPOT check: recover from the 0DTE chain (most strikes, tightest near-ATM). ---
    dd = s5.load_day(d)
    z = s5.zero_dte_chain(d, day_data=dd)
    spot_err = float("nan")
    recovered_spot = float("nan")
    if not z.nbbo.empty:
        snap0 = _minute_snap(z.nbbo, minute)
        sr = recon.recover_forward_spot(snap0, minute, d)
        if sr is not None:
            recovered_spot = sr.spot
            spot_err = recovered_spot - eod_spot

    # --- DELTA-METHOD check: nearest ~30-DTE expiration present intraday. ---
    intraday_exps = sorted(pd.to_datetime(dd.quote["expiration"].unique()).date)
    target = d + _dt.timedelta(days=DELTA_CHECK_TARGET_DTE)
    future = [e for e in intraday_exps if e > d]
    delta_df = None
    chosen_exp = None
    if future:
        chosen_exp = min(future, key=lambda e: abs((e - target).days))
        grid = s5.nbbo_grid(d, expiration=chosen_exp, minutes=pd.DatetimeIndex([minute]),
                            quote=dd.quote)
        if not grid.empty:
            snap = grid[["strike", "right", "bid", "ask"]].copy()
            sr2 = recon.recover_forward_spot(snap, minute, chosen_exp)
            if sr2 is not None:
                rec = recon.per_strike_delta(snap, minute, chosen_exp, sr2.spot)
                # EOD ground-truth deltas for this expiration.
                exp_str = chosen_exp.strftime("%Y-%m-%d")
                eod_exp = eod[eod["expiration"] == exp_str][
                    ["strike", "right", "delta", "implied_vol"]
                ].copy()
                merged = rec.merge(
                    eod_exp, on=["strike", "right"], suffixes=("_rec", "_eod")
                )
                if not merged.empty:
                    merged["abs_delta"] = merged["delta_eod"].abs()
                    band = merged[
                        (merged["abs_delta"] >= _DELTA_BAND[0])
                        & (merged["abs_delta"] <= _DELTA_BAND[1])
                    ].copy()
                    band["delta_err"] = band["delta_rec"] - band["delta_eod"]
                    band["day"] = d
                    band["check_exp"] = exp_str
                    delta_df = band

    return {
        "day": d,
        "eod_spot": eod_spot,
        "recovered_spot": recovered_spot,
        "spot_err": spot_err,
        "delta_check_exp": chosen_exp,
        "delta_df": delta_df,
    }


def validate_sample(days: list[_dt.date], verbose: bool = True) -> dict:
    """Validate a sample of days; print and return the aggregate error distribution."""
    spot_rows = []
    delta_frames = []
    for d in days:
        try:
            res = validate_day(d)
        except Exception as e:  # one bad day must not abort the sample
            if verbose:
                print(f"  [skip] {d}: {type(e).__name__}: {e}", flush=True)
            continue
        if res is None:
            if verbose:
                print(f"  [skip] {d}: no validation data", flush=True)
            continue
        spot_rows.append(
            {"day": d, "eod_spot": res["eod_spot"],
             "recovered_spot": res["recovered_spot"], "spot_err": res["spot_err"]}
        )
        if res["delta_df"] is not None and not res["delta_df"].empty:
            delta_frames.append(res["delta_df"])
        if verbose:
            nd = 0 if res["delta_df"] is None else len(res["delta_df"])
            print(
                f"  {d}: spot rec={res['recovered_spot']:.2f} eod={res['eod_spot']:.2f} "
                f"err={res['spot_err']:+.2f}  delta-check exp={res['delta_check_exp']} "
                f"(n={nd})",
                flush=True,
            )

    spot = pd.DataFrame(spot_rows)
    deltas = pd.concat(delta_frames, ignore_index=True) if delta_frames else pd.DataFrame()

    summary = {"n_days": len(spot), "spot": {}, "delta": {}}
    if not spot.empty:
        ae = spot["spot_err"].abs().dropna()
        summary["spot"] = {
            "median_abs_err_pts": float(ae.median()),
            "p95_abs_err_pts": float(ae.quantile(0.95)),
            "max_abs_err_pts": float(ae.max()),
            "n": int(ae.shape[0]),
        }
    if not deltas.empty:
        de = deltas["delta_err"].abs().dropna()
        summary["delta"] = {
            "median_abs_err": float(de.median()),
            "p95_abs_err": float(de.quantile(0.95)),
            "max_abs_err": float(de.max()),
            "n_strikes": int(de.shape[0]),
        }

    if verbose:
        print("\n=== VALIDATION SUMMARY ===", flush=True)
        print(f"days validated: {summary['n_days']}", flush=True)
        if summary["spot"]:
            s = summary["spot"]
            print(
                f"SPOT abs err (pts): median={s['median_abs_err_pts']:.2f} "
                f"p95={s['p95_abs_err_pts']:.2f} max={s['max_abs_err_pts']:.2f} "
                f"(n={s['n']} days)",
                flush=True,
            )
        if summary["delta"]:
            dl = summary["delta"]
            print(
                f"DELTA abs err: median={dl['median_abs_err']:.4f} "
                f"p95={dl['p95_abs_err']:.4f} max={dl['max_abs_err']:.4f} "
                f"(n={dl['n_strikes']} strikes, ~{DELTA_CHECK_TARGET_DTE}DTE method check)",
                flush=True,
            )
    return {"summary": summary, "spot": spot, "deltas": deltas}


# Diverse sample requested by the spike: calm days + 2022 selloff + 2023-03 SVB + 2024-08-05.
DIVERSE_SAMPLE = [
    _dt.date(2022, 6, 13),   # 2022 bear-market selloff (CPI shock week)
    _dt.date(2022, 9, 13),   # 2022 hot-CPI -5% day
    _dt.date(2023, 3, 10),   # SVB collapse
    _dt.date(2023, 3, 13),   # SVB aftermath
    _dt.date(2023, 7, 14),   # calm summer
    _dt.date(2023, 11, 3),   # calm
    _dt.date(2024, 8, 5),    # vol spike / yen-carry unwind
    _dt.date(2024, 8, 6),    # vol-spike aftermath
    _dt.date(2024, 12, 18),  # FOMC hawkish-cut selloff
    _dt.date(2025, 1, 15),   # calm
    _dt.date(2025, 4, 7),    # (tariff-era vol, if present)
    _dt.date(2025, 9, 17),   # calm/FOMC
    _dt.date(2026, 1, 15),   # recent calm
    _dt.date(2026, 6, 25),   # most recent
]


if __name__ == "__main__":
    avail = set(s5.available_days())
    sample = [d for d in DIVERSE_SAMPLE if d in avail]
    print(f"Validating {len(sample)} of {len(DIVERSE_SAMPLE)} diverse days present in warehouse\n")
    validate_sample(sample)
