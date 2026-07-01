r"""
s6_matrix.py — the S6 SENSITIVITY MATRIX (a MAP for understanding, not a dial to tune).

PAPER / research only. OFFLINE. Windows. STRICTLY READ-ONLY on every warehouse.

WHAT THIS IS
------------
The prior spike (s6_control.py) proved the honest fixed-delta 0DTE credit-spread baseline
LOSES in all three structures. This module extends that yardstick into a small,
PRE-SPECIFIED sensitivity surface so we can SEE where (if anywhere) the loss softens, and
— above all — tell a ROBUST PLATEAU apart from an isolated PEAK. We are hunting plateaus
and REJECTING peaks. We do NOT optimize, we do NOT pick a winner, we do NOT tune a
threshold. (Rule #1: never curve-fit.)

THE AXES (degrees of freedom kept deliberately tiny, all declared once below)
-----------------------------------------------------------------------------
  * exit knob : {stop@2x credit, stop@3x credit, hold-to-settlement}  — exactly 3, no grid.
  * dealer-gamma regime : {positive GEX, negative GEX} from the PRIOR EOD (no look-ahead).
  * VIX term structure : {contango, backwardation} from the PRIOR EOD VIX9D/VIX ratio at
    the standard 1.0 crossover (NOT a tuned threshold).
  * structure : {bull_put, bear_call, iron_condor} — reported SEPARATELY.
Everything else is FIXED at the control's documented constants (delta 0.15, 5-wide, 14:00
entry, $0.05 winner, $0.30 min credit, honest bid/ask fills). We do NOT sweep delta or
entry time.

HOW IT REUSES THE CONTROL (avoids a full 27 GB re-read)
-------------------------------------------------------
The control already stored, per (day, structure), the EXACT chosen legs and the 2x-stop
outcome (winner / stop / settle) in s6_control_trades.csv. Raising or removing the stop
can ONLY change days whose 2x stop actually FIRED:
  * a 'winner' or 'settle' row is IDENTICAL under 2x / 3x / hold (the stop never bound), so
    we carry those forward untouched — no re-read.
  * a 'stop' row must be re-scanned intraday to learn what 3x and hold would have done. We
    rebuild the SAME legs from the stored strikes and re-walk that one day's NBBO. Only
    ~650 days need re-reading (the stop-days), not all 1092.
This is exact, not an approximation: the legs and the causal minute-walk are the control's
own (s6_control._scan_exit / _spread_debit_to_close), so the 2x column reproduces the
control byte-for-byte and the 3x/hold columns share the identical fill engine.

NO LOOK-AHEAD
-------------
The day classifier uses ONLY the prior trading day's EOD data (gamma sign + VIX ratio),
which is fully knowable before the 14:00 entry. classify_day() is pinned by a no-look-ahead
test (tests/test_s6_matrix.py) and the standing causality guard.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path

import numpy as np
import pandas as pd

import s5_intraday_data as s5
import s6_control as ctrl

# --------------------------------------------------------------------------- #
# Declared axis constants (NOT tuned — standard crossovers / GEX sign only).
# --------------------------------------------------------------------------- #
EXIT_KNOBS = ("stop_2x", "stop_3x", "hold")     # exactly these three
STOP_MULTIPLES = {"stop_2x": 2.0, "stop_3x": 3.0, "hold": None}  # None => no stop

# VIX term-structure regime from the STANDARD 1.0 crossover of VIX9D / VIX.
#   ratio < 1  => VIX9D below VIX  => upward-sloping short end  => CONTANGO (calm)
#   ratio > 1  => VIX9D above VIX  => inverted short end        => BACKWARDATION (stress)
# 1.0 is the definitional crossover, not a fitted knob.
VIX_CROSSOVER = 1.0

# The 0DTE product we trade is SPXW; classify on the SPXW EOD dealer-gamma table so the
# regime label matches the traded instrument. (SPX index-root table gives a near-identical
# sign; we use SPXW to be self-consistent.)
GEX_DAILY_PARQUET = Path(r"C:\TradingDesk-Local\warehouse\derived\SPXW_gex_daily.parquet")
VIX_PARQUET = Path(r"C:\TradingDesk-Local\bt_data\_vix.parquet")
VIX9D_PARQUET = Path(r"C:\TradingDesk-Local\bt_data\_vix9d.parquet")

# Train / test split (fixed, declared once — an out-of-sample discipline, not a selector).
TRAIN_END = _dt.date(2024, 6, 30)     # train: 2022-04 .. 2024-06 ; test: 2024-07 .. 2026-06
THIN_N = 30                           # cells with fewer trade-days than this are unusable

CONTROL_TRADES_CSV = ctrl.OUTPUT_DIR / "s6_control_trades.csv"
OUTPUT_DIR = ctrl.OUTPUT_DIR


# --------------------------------------------------------------------------- #
# Day classifier — PRIOR-EOD gamma sign + VIX term structure (no look-ahead).
# --------------------------------------------------------------------------- #
def _load_gamma_daily() -> pd.DataFrame:
    """Prior-EOD dealer-gamma state per date, indexed by date (python date)."""
    g = pd.read_parquet(GEX_DAILY_PARQUET)[["date", "gamma_state", "net_gex"]].copy()
    g["date"] = pd.to_datetime(g["date"].astype(str), format="%Y%m%d").dt.date
    g["gamma_state"] = g["gamma_state"].astype(str).str.strip().str.title()
    return g.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)


def _load_vix_ratio() -> pd.DataFrame:
    """Prior-EOD VIX9D/VIX ratio per date. NaN where either series is missing that day."""
    vix = pd.read_parquet(VIX_PARQUET).rename(columns={"vix": "vix"})
    v9 = pd.read_parquet(VIX9D_PARQUET).rename(columns={"vix9d": "vix9d"})
    j = v9.join(vix, how="inner")
    j["ratio"] = j["vix9d"] / j["vix"]
    out = j.reset_index()[["date", "ratio"]].copy()
    out["date"] = pd.to_datetime(out["date"]).dt.date
    return out.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)


class DayClassifier:
    """Classify a trade-day by the PRIOR trading day's EOD gamma sign + VIX term structure.

    Strictly causal: for a trade-day D we take the LAST row STRICTLY BEFORE D in each EOD
    series. Nothing from D (or later) is ever read. This is what makes the label knowable
    before the 14:00 entry.
    """

    def __init__(self, gamma: pd.DataFrame | None = None, vix: pd.DataFrame | None = None):
        self.gamma = gamma if gamma is not None else _load_gamma_daily()
        self.vix = vix if vix is not None else _load_vix_ratio()
        self._g_dates = self.gamma["date"].to_numpy()
        self._v_dates = self.vix["date"].to_numpy()

    @staticmethod
    def _prior_row(dates: np.ndarray, frame: pd.DataFrame, d: _dt.date):
        """Row of `frame` for the greatest date STRICTLY LESS THAN d, or None."""
        # dates is sorted ascending; find the insertion point for d and step back one.
        idx = int(np.searchsorted(dates, d, side="left")) - 1
        if idx < 0:
            return None
        return frame.iloc[idx]

    def gamma_regime(self, d: _dt.date) -> str:
        """'positive' | 'negative' | 'neutral' | 'unknown' from the PRIOR EOD gamma sign."""
        row = self._prior_row(self._g_dates, self.gamma, d)
        if row is None:
            return "unknown"
        st = str(row["gamma_state"]).lower()
        if st.startswith("pos"):
            return "positive"
        if st.startswith("neg"):
            return "negative"
        if st.startswith("neu"):
            return "neutral"
        return "unknown"

    def vix_regime(self, d: _dt.date) -> str:
        """'contango' | 'backwardation' | 'unknown' from the PRIOR EOD VIX9D/VIX ratio."""
        row = self._prior_row(self._v_dates, self.vix, d)
        if row is None or not np.isfinite(row["ratio"]):
            return "unknown"
        return "backwardation" if float(row["ratio"]) > VIX_CROSSOVER else "contango"

    def classify(self, d: _dt.date) -> dict:
        return {"day": d, "gamma_regime": self.gamma_regime(d),
                "vix_regime": self.vix_regime(d)}


# --------------------------------------------------------------------------- #
# Multi-exit re-scan for a single stop-day (reuses the control's fill engine).
# --------------------------------------------------------------------------- #
def _legs_from_row(row: pd.Series) -> list[tuple] | None:
    """Rebuild the exact leg list the control traded, from the stored strikes.

    bull_put   : short PUT @short_strike,  long PUT @long_strike
    bear_call  : short CALL @short_strike, long CALL @long_strike
    iron_condor: bull_put legs + bear_call legs (call side in *_strike_2)
    """
    s = row["structure"]
    sk, lk = row["short_strike"], row["long_strike"]
    if s == "bull_put":
        return [(float(sk), "PUT", +1), (float(lk), "PUT", -1)]
    if s == "bear_call":
        return [(float(sk), "CALL", +1), (float(lk), "CALL", -1)]
    if s == "iron_condor":
        sk2, lk2 = row["short_strike_2"], row["long_strike_2"]
        if not (np.isfinite(sk2) and np.isfinite(lk2)):
            return None
        return [(float(sk), "PUT", +1), (float(lk), "PUT", -1),
                (float(sk2), "CALL", +1), (float(lk2), "CALL", -1)]
    return None


def _scan_all_exits(
    nbbo: pd.DataFrame,
    legs: list[tuple],
    entry_credit: float,
    entry_minute: pd.Timestamp,
    settle_minute: pd.Timestamp,
) -> dict[str, tuple[str, pd.Timestamp, float]]:
    """Walk the minutes ONCE and return the (reason, minute, debit) for ALL exit knobs.

    Shares the control's exact causal engine: winner fires at debit<=WINNER_DEBIT; a stop
    fires at debit>=(1+mult)*credit for that knob's multiple; hold has no stop. Each knob is
    resolved at the FIRST minute its own rule binds — never peeking past that minute. If no
    rule binds by settlement, close at the last marked debit ('settle'). This reproduces
    s6_control._scan_exit for the 2x knob byte-for-byte, and shares the fill math for 3x/hold.
    """
    minutes = sorted(m for m in nbbo["minute"].unique()
                     if entry_minute < m <= settle_minute)
    thresholds = {k: (None if STOP_MULTIPLES[k] is None
                      else (1.0 + STOP_MULTIPLES[k]) * entry_credit)
                  for k in EXIT_KNOBS}
    resolved: dict[str, tuple[str, pd.Timestamp, float]] = {}
    last_debit = float("nan")
    last_minute = entry_minute
    for m in minutes:
        snap = ctrl._snap_at(nbbo, m)
        debit = ctrl._spread_debit_to_close(snap, legs)
        if debit is None:
            continue  # unquoted minute -> cannot act; never invent a fill.
        last_debit, last_minute = debit, m
        winner = debit <= ctrl.WINNER_DEBIT
        for k in EXIT_KNOBS:
            if k in resolved:
                continue
            if winner:
                resolved[k] = ("winner", m, debit)
                continue
            thr = thresholds[k]
            if thr is not None and debit >= thr:
                resolved[k] = ("stop", m, debit)
        if len(resolved) == len(EXIT_KNOBS):
            break
    for k in EXIT_KNOBS:
        if k not in resolved:
            resolved[k] = ("settle", last_minute, last_debit)
    return resolved


def _rescan_stop_day_batch(
    d: _dt.date, rows: list[pd.Series]
) -> dict[str, dict[str, tuple[str, float]]]:
    """Re-derive (reason, pnl) per exit knob for ALL stop-rows of one DAY at once.

    Loads the day's 0DTE NBBO exactly ONCE (the expensive I/O) and re-scans every
    structure's stored legs against it. Returns {structure: {knob: (reason, pnl)}}.
    A structure absent from the result means it could not be re-marked (caller falls back
    to the stored 2x outcome for that structure)."""
    out: dict[str, dict[str, tuple[str, float]]] = {}
    try:
        dd = s5.load_day(d)
        chain = s5.zero_dte_chain(d, day_data=dd)
        nbbo = chain.nbbo
    except Exception:
        return out
    if nbbo.empty:
        return out
    entry_minute = pd.Timestamp(_dt.datetime.combine(d, ctrl.ENTRY_TIME))
    settle_minute = pd.Timestamp(_dt.datetime.combine(d, ctrl.SETTLEMENT_TIME))
    for row in rows:
        legs = _legs_from_row(row)
        if legs is None:
            continue
        entry_credit = float(row["entry_credit"])
        exits = _scan_all_exits(nbbo, legs, entry_credit, entry_minute, settle_minute)
        per_knob = {}
        ok = True
        for k, (reason, _m, debit) in exits.items():
            if not np.isfinite(debit):
                ok = False
                break
            pnl = (entry_credit - debit) * ctrl.CONTRACT_MULTIPLIER * ctrl.N_CONTRACTS
            per_knob[k] = (reason, pnl)
        if ok:
            out[row["structure"]] = per_knob
    return out


# --------------------------------------------------------------------------- #
# Build the per-trade table with all three exit-knob P&Ls + day classification.
# --------------------------------------------------------------------------- #
_ENRICHED_CSV = OUTPUT_DIR / "s6_matrix_trades_enriched.csv"


def build_enriched_trades(
    control_csv: Path = CONTROL_TRADES_CSV,
    classifier: DayClassifier | None = None,
    verbose: bool = True,
    save: bool = True,
    resume: bool = True,
) -> pd.DataFrame:
    """One row per traded (day, structure) with pnl under each exit knob + regime labels.

    CRASH-RESILIENT + RESUMABLE: stop-day re-scans (the only I/O) are checkpointed to a
    partial CSV keyed by (day, structure); a killed run resumes and skips finished keys.
    winner/settle rows need no re-scan (stop never bound => identical under 2x/3x/hold), so
    they are filled instantly from the control CSV.
    """
    clf = classifier if classifier is not None else DayClassifier()
    df = pd.read_csv(control_csv)
    df["traded"] = df["traded"].astype(str).str.lower().isin(["true", "1"])
    traded = df[df["traded"]].copy()
    traded["day"] = pd.to_datetime(traded["day"]).dt.date

    # Regime labels (prior-EOD, causal).
    labels = traded["day"].map(lambda d: clf.classify(d))
    traded["gamma_regime"] = [x["gamma_regime"] for x in labels]
    traded["vix_regime"] = [x["vix_regime"] for x in labels]

    # Time half.
    traded["half"] = np.where(traded["day"] <= TRAIN_END, "train", "test")

    # Resume: re-scan cache keyed by (day, structure).
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    cache: dict[tuple, dict[str, tuple[str, float]]] = {}
    if resume and _ENRICHED_CSV.is_file():
        try:
            prev = pd.read_csv(_ENRICHED_CSV)
            for _, r in prev.iterrows():
                key = (str(r["day"]), r["structure"])
                cache[key] = {
                    k: (r[f"reason_{k}"], float(r[f"pnl_{k}"])) for k in EXIT_KNOBS
                }
        except Exception:
            cache = {}
    if verbose and cache:
        print(f"resume: {len(cache)} enriched rows already cached; reusing them", flush=True)

    stop_rows = traded[traded["exit_reason"] == "stop"]
    n_stop = len(stop_rows)

    # Group stop-rows that still need a re-scan by DAY, so each day's parquet loads ONCE
    # (the expensive I/O). Days fully cached from a prior run are skipped entirely.
    to_scan: dict[_dt.date, list[pd.Series]] = {}
    for _, row in stop_rows.iterrows():
        key = (str(row["day"]), row["structure"])
        if key in cache:
            continue
        to_scan.setdefault(row["day"], []).append(row)
    n_days_to_scan = len(to_scan)
    if verbose:
        print(f"{len(traded)} traded rows; {n_stop} are stop-rows. "
              f"{n_days_to_scan} distinct stop-DAYS still need a re-scan "
              f"(winner/settle rows are identical across exit knobs; one parquet load/day).",
              flush=True)

    import csv
    fieldnames = (["day", "structure", "gamma_regime", "vix_regime", "half",
                   "entry_credit", "control_exit_reason"]
                  + [f"reason_{k}" for k in EXIT_KNOBS]
                  + [f"pnl_{k}" for k in EXIT_KNOBS])
    write_header = not _ENRICHED_CSV.is_file()
    scanned_days = 0
    # Re-scan uncached stop-days (batched per day) and append to the checkpoint CSV.
    with open(_ENRICHED_CSV, "a", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        if write_header:
            writer.writeheader()
        for d in sorted(to_scan):
            day_rows = to_scan[d]
            batch = _rescan_stop_day_batch(d, day_rows)
            for row in day_rows:
                key = (str(row["day"]), row["structure"])
                base_pnl = float(row["pnl_dollars"])
                per_knob = batch.get(
                    row["structure"],
                    # Fallback: keep the stored 2x outcome (rare unmarkable day) — recorded
                    # under a distinct reason so it is auditable, never silent.
                    {k: ("stop_fallback", base_pnl) for k in EXIT_KNOBS},
                )
                cache[key] = per_knob
                rec_row = {
                    "day": str(row["day"]), "structure": row["structure"],
                    "gamma_regime": row["gamma_regime"], "vix_regime": row["vix_regime"],
                    "half": row["half"], "entry_credit": row["entry_credit"],
                    "control_exit_reason": "stop",
                }
                for k in EXIT_KNOBS:
                    rec_row[f"reason_{k}"] = per_knob[k][0]
                    rec_row[f"pnl_{k}"] = per_knob[k][1]
                writer.writerow(rec_row)
            fh.flush()  # persist this whole day before moving on
            scanned_days += 1
            if verbose and (scanned_days % 25 == 0 or scanned_days == n_days_to_scan):
                print(f"  re-scanned {scanned_days}/{n_days_to_scan} stop-days", flush=True)

    # Assemble the full enriched table (winner/settle carried forward; stop from cache).
    records = []
    for _, row in traded.iterrows():
        base_pnl = float(row["pnl_dollars"])
        base_reason = str(row["exit_reason"])
        if base_reason == "stop":
            per_knob = cache.get((str(row["day"]), row["structure"]),
                                 {k: ("stop_fallback", base_pnl) for k in EXIT_KNOBS})
        else:
            per_knob = {k: (base_reason, base_pnl) for k in EXIT_KNOBS}
        rec = {
            "day": row["day"], "structure": row["structure"],
            "gamma_regime": row["gamma_regime"], "vix_regime": row["vix_regime"],
            "half": row["half"], "entry_credit": row["entry_credit"],
            "control_exit_reason": base_reason,
        }
        for k in EXIT_KNOBS:
            rec[f"reason_{k}"] = per_knob[k][0]
            rec[f"pnl_{k}"] = per_knob[k][1]
        records.append(rec)

    enriched = pd.DataFrame(records)
    if save:
        enriched.to_csv(OUTPUT_DIR / "s6_matrix_trades.csv", index=False)
        if verbose:
            print(f"Saved enriched trades to {OUTPUT_DIR / 's6_matrix_trades.csv'} "
                  f"({scanned_days} stop-days re-scanned this run)", flush=True)
    return enriched


# --------------------------------------------------------------------------- #
# Cell statistics + plateau/peak classification.
# --------------------------------------------------------------------------- #
def _cell_stats(sub: pd.DataFrame, pnl_col: str) -> dict:
    """Robustness stats for one cell (one exit x gamma x vix x structure), using pnl_col."""
    pnl = sub[pnl_col].to_numpy(dtype=float)
    n = len(pnl)
    if n == 0:
        return {"n": 0}
    daily = sub.sort_values("day")[pnl_col].to_numpy(dtype=float)
    wins = pnl[pnl > 0]
    losses = pnl[pnl <= 0]
    win_rate = len(wins) / n
    avg_win = float(wins.mean()) if len(wins) else 0.0
    avg_loss = float(losses.mean()) if len(losses) else 0.0
    loss_over_win = (abs(avg_loss) / avg_win) if avg_win > 0 else float("nan")
    # max consecutive losing days
    max_streak = cur = 0
    for v in daily:
        cur = cur + 1 if v < 0 else 0
        max_streak = max(max_streak, cur)
    # per-half totals
    half_totals = {}
    for h in ("train", "test"):
        hp = sub[sub["half"] == h][pnl_col]
        half_totals[h] = (round(float(hp.sum()), 2), int(len(hp)))
    return {
        "n": n,
        "win_rate": round(win_rate, 4),
        "avg_win_$": round(avg_win, 2),
        "avg_loss_$": round(avg_loss, 2),
        "loss_over_win": round(loss_over_win, 3),
        "total_pnl_$": round(float(pnl.sum()), 2),
        "worst_day_$": round(float(pnl.min()), 2),
        "max_consec_losing_days": int(max_streak),
        "train_pnl_$": half_totals["train"][0], "train_n": half_totals["train"][1],
        "test_pnl_$": half_totals["test"][0], "test_n": half_totals["test"][1],
    }


def build_matrix(enriched: pd.DataFrame) -> pd.DataFrame:
    """Full surface: one row per (structure, gamma_regime, vix_regime, exit_knob) cell.

    Only the two requested gamma regimes (positive/negative) and two VIX regimes
    (contango/backwardation) are reported as headline cells; neutral/unknown days are
    excluded from the headline matrix (they are not one of the pre-specified regimes) but
    counted in a separate diagnostic so nothing is hidden.
    """
    rows = []
    structures = ["bull_put", "bear_call", "iron_condor"]
    for structure in structures:
        for gamma in ("positive", "negative"):
            for vix in ("contango", "backwardation"):
                cell = enriched[(enriched["structure"] == structure)
                                & (enriched["gamma_regime"] == gamma)
                                & (enriched["vix_regime"] == vix)]
                for knob in EXIT_KNOBS:
                    st = _cell_stats(cell, f"pnl_{knob}")
                    rows.append({
                        "structure": structure, "gamma": gamma, "vix": vix,
                        "exit": knob, **st,
                    })
    return pd.DataFrame(rows)


def classify_plateau_peak(matrix: pd.DataFrame) -> pd.DataFrame:
    """Tag each cell PLATEAU / PEAK / thin / loss, WITHOUT selecting any winner.

    A cell is a candidate edge only if total_pnl > 0. Then:
      * thin       : n < THIN_N  -> unusable, cannot conclude (reported, never a finding).
      * both_halves: profitable in BOTH train and test halves.
      * neighbor_ok: the ADJACENT exit knob in the same (structure,gamma,vix) column is also
                     profitable (2x<->3x<->hold ordering). Breadth across the exit axis.
      * PLATEAU    : profitable AND both_halves AND neighbor_ok AND not thin.
      * PEAK       : profitable but isolated (one-half-only OR no neighbor agreement OR thin)
                     -> distrust / reject.
      * loss       : total_pnl <= 0 (the honest default — matches the losing control).
    """
    m = matrix.copy()
    order = {"stop_2x": 0, "stop_3x": 1, "hold": 2}
    prof = {}
    for _, r in m.iterrows():
        prof[(r["structure"], r["gamma"], r["vix"], r["exit"])] = (r.get("total_pnl_$", 0) or 0) > 0

    def neighbor_ok(r) -> bool:
        neighbors = {"stop_2x": ["stop_3x"], "stop_3x": ["stop_2x", "hold"],
                     "hold": ["stop_3x"]}[r["exit"]]
        return any(prof.get((r["structure"], r["gamma"], r["vix"], nb), False) for nb in neighbors)

    tags = []
    for _, r in m.iterrows():
        n = r.get("n", 0) or 0
        total = r.get("total_pnl_$", 0) or 0
        if total <= 0:
            tags.append("loss")
            continue
        if n < THIN_N:
            tags.append("PEAK(thin)")
            continue
        both = (r.get("train_pnl_$", 0) or 0) > 0 and (r.get("test_pnl_$", 0) or 0) > 0
        nb = neighbor_ok(r)
        if both and nb:
            tags.append("PLATEAU")
        else:
            reason = []
            if not both:
                reason.append("one-half")
            if not nb:
                reason.append("no-neighbor")
            tags.append("PEAK(" + ",".join(reason) + ")")
    m["classification"] = tags
    return m


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def _fmt_matrix_table(m: pd.DataFrame, structure: str) -> str:
    sub = m[m["structure"] == structure].copy()
    cols = ["gamma", "vix", "exit", "n", "total_pnl_$", "train_pnl_$", "train_n",
            "test_pnl_$", "test_n", "win_rate", "loss_over_win", "worst_day_$",
            "max_consec_losing_days", "classification"]
    cols = [c for c in cols if c in sub.columns]
    with pd.option_context("display.width", 250, "display.max_columns", 60,
                           "display.max_rows", 200):
        return sub[cols].to_string(index=False)


def per_structure_verdict(m: pd.DataFrame, structure: str) -> str:
    sub = m[m["structure"] == structure]
    plateaus = sub[sub["classification"] == "PLATEAU"]
    peaks = sub[sub["classification"].astype(str).str.startswith("PEAK")]
    if len(plateaus):
        lines = [f"VERDICT [{structure}]: {len(plateaus)} ROBUST PLATEAU cell(s) beat the "
                 f"losing control (profitable in BOTH halves, neighbor agrees, n>={THIN_N}):"]
        for _, r in plateaus.iterrows():
            lines.append(f"    - gamma={r['gamma']}, vix={r['vix']}, exit={r['exit']}: "
                         f"total=${r['total_pnl_$']} (train ${r['train_pnl_$']}, "
                         f"test ${r['test_pnl_$']}, n={r['n']})")
        lines.append(f"    ({len(peaks)} other profitable-looking cells were classified as "
                     f"PEAK and are DISTRUSTED/REJECTED.) NOTE: a plateau is a map feature, "
                     f"NOT a recommendation — adoption requires Andrew's explicit blessing.")
        return "\n".join(lines)
    return (f"VERDICT [{structure}]: NO robust plateau. {len(peaks)} cell(s) looked "
            f"profitable but every one is an isolated PEAK (one-half-only, no neighbor "
            f"agreement, or thin-n) and is REJECTED. Once robustness is demanded, the "
            f"strategy stays unprofitable — consistent with the losing control.")


def run(verbose: bool = True, save: bool = True) -> dict:
    """Full pipeline: enrich trades -> build matrix -> classify -> report."""
    enriched = build_enriched_trades(verbose=verbose, save=save)
    matrix = build_matrix(enriched)
    matrix = classify_plateau_peak(matrix)
    if save:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        matrix.to_csv(OUTPUT_DIR / "s6_matrix_surface.csv", index=False)

    if verbose:
        # Diagnostic: how days fell into regimes (incl. excluded neutral/unknown).
        diag = (enriched.drop_duplicates("day")
                .groupby(["gamma_regime", "vix_regime"]).size()
                .rename("day_count").reset_index())
        print("\n=== DAY-REGIME DISTRIBUTION (distinct days; headline uses "
              "positive/negative x contango/backwardation only) ===", flush=True)
        print(diag.to_string(index=False), flush=True)

        for structure in ["bull_put", "bear_call", "iron_condor"]:
            print(f"\n{'='*90}\nS6 SENSITIVITY MATRIX — {structure}\n{'='*90}", flush=True)
            print(_fmt_matrix_table(matrix, structure), flush=True)
            print("\n" + per_structure_verdict(matrix, structure), flush=True)
    return {"enriched": enriched, "matrix": matrix}


if __name__ == "__main__":
    run()
