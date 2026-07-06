r"""
condor_alpha_beta.py — ARM 6: is Arm 5's positive P&L structural ALPHA or short-vol/short-gamma BETA?

PAPER / research only. OFFLINE. Windows. STRICTLY READ-ONLY on the warehouse.

Arm 5 (output/condor_cashsettle_hold_20260706.md) — the wide-wing 0DTE iron condor entered
14:00, held to COSTLESS cash settlement at 16:00, no management — came back net-positive and
OOS-positive across widths at honest fills. Its "refutation" was a random-subset participation
placebo, which is a day-SELECTION test mis-applied to a no-selection strategy.

The REAL open question (Andrew-blessed): is this a genuine edge, or just the volatility /
short-gamma risk premium ANY always-short-premium position would harvest in a benign 2022-2025
window? This arm decides it. Judge on NET MERIT.

THE EXPOSURE WINDOW (load-bearing). Arm 5 enters at 14:00 (entry_spot) and resolves at 16:00
cash intrinsic against the recovered 16:00 index level S* (settle_spot). The ENTIRE P&L is a
function of the realized 14:00->16:00 move. So every regression factor here uses the INTRADAY
14:00->16:00 move, NOT close-to-close daily returns:
    move  = (settle_spot - entry_spot) / entry_spot     (signed realized 2h move)
    |move|, move^2                                       (short-gamma / convexity exposure)
A short condor is short realized variance over exactly this window; the intercept after
controlling for the move is the premium/VRP harvested independent of the realized move = ALPHA.

The four pre-registered tests (see the task brief):
  1. Alpha-vs-beta regression on the 14:00->16:00 move (+ |move| + move^2), TRAIN vs TEST.
     Positive OOS-stable intercept after controlling for the move = alpha; collapses = beta.
  2. Passive short-premium benchmark on the SAME days: a delta-neutral ATM short straddle
     entered 14:00, held to cash settlement, honest entry fills. Does the CONDOR structure earn
     anything the passive short-vol position does not?
  3. VRP decomposition: implied-minus-realized (sold IV richer than realized 2h move), stable OOS?
  4. Tail-sufficiency / stress: characterize worst realized 2h moves; inject hypothetical -3/-5/-7%
     intraday crashes at the CAPPED (defined-risk) per-width loss; is the positive total calm-carry
     a fatter tail would erase? Flag that 0DTE dailies start ~2022 => COVID-scale intraday tails
     are OUT of sample.

Reuses the CSP alpha-vs-beta machinery (csp_alpha_beta.ols_alpha_beta, bootstrap, daily_metrics)
for consistency. The passive straddle benchmark reuses s6_control / s6_recon (14:00 entry, honest
fills, recovered spot) unchanged.
"""

from __future__ import annotations

import datetime as _dt
import time
from dataclasses import dataclass, asdict
from pathlib import Path

import numpy as np
import pandas as pd

import csp_alpha_beta as cab   # reuse ols_alpha_beta, bootstrap, daily_metrics, _stationary_blocks

HERE = Path(__file__).resolve().parent
ARM5_DAYS_CSV = HERE / "output" / "condor_cashsettle_hold" / "condor_cashsettle_hold_days.csv"
REPORT = HERE / "output" / "condor_alpha_beta_20260706.md"
CSV_DIR = HERE / "output" / "condor_alpha_beta"
CSV_DIR.mkdir(parents=True, exist_ok=True)

OOS_SPLIT = _dt.date(2024, 6, 30)     # train <= split ; test > split  (Arm-5 convention)
CLEAN_WIDTHS = [20, 30, 50]           # the cleanly-positive widths (task brief)
HEADLINE_F = "f50"                    # 50% half-spread entry fill (headline)
HEADLINE_WIDTH = 50
CONTRACT_MULTIPLIER = 100.0
TRADING_DAYS = 252.0
BOOT_SEED = 20260706

# hypothetical adverse 14:00->16:00 intraday crashes for the defined-risk stress (test 4b)
STRESS_MOVES = [-0.01, -0.02, -0.03, -0.05, -0.07]


# --------------------------------------------------------------------------- #
# Load Arm 5 per-day data, derive the 14:00->16:00 move
# --------------------------------------------------------------------------- #
def load_arm5() -> pd.DataFrame:
    df = pd.read_csv(ARM5_DAYS_CSV)
    df["traded"] = df["traded"].astype(str).str.lower().isin(["true", "1"])
    df = df[df["traded"]].copy()
    df["day"] = pd.to_datetime(df["day"])
    # signed realized 14:00->16:00 move (the trade's exposure window)
    df["move"] = (df["settle_spot"] - df["entry_spot"]) / df["entry_spot"]
    df["abs_move"] = df["move"].abs()
    df["move_sq"] = df["move"] ** 2
    df["half"] = np.where(df["day"] <= pd.Timestamp(OOS_SPLIT), "train", "test")
    df["year"] = df["day"].dt.year
    return df.sort_values("day").reset_index(drop=True)


def width_pnl_col(width: int, f: str) -> str:
    return f"pnl_w{width}_{f}"


def width_credit_col(width: int, f: str) -> str:
    return f"entry_credit_w{width}_{f}"


# --------------------------------------------------------------------------- #
# TEST 1 — alpha-vs-beta regression on the 14:00->16:00 move
# --------------------------------------------------------------------------- #
def _ols_multi(y: np.ndarray, X_no_const: np.ndarray) -> dict:
    """OLS of y on [1, X]. Returns intercept (alpha), betas, R^2, and t-stats (incl. alpha)."""
    y = np.asarray(y, dtype=float)
    Xc = np.column_stack([np.ones(len(y)), X_no_const])
    coef, *_ = np.linalg.lstsq(Xc, y, rcond=None)
    resid = y - Xc @ coef
    n, k = Xc.shape
    dof = max(n - k, 1)
    sigma2 = float(resid @ resid) / dof
    XtX_inv = np.linalg.inv(Xc.T @ Xc)
    se = np.sqrt(np.diag(sigma2 * XtX_inv))
    tstats = coef / se
    ss_tot = float(((y - y.mean()) ** 2).sum())
    ss_res = float((resid ** 2).sum())
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else float("nan")
    return dict(coef=coef, se=se, t=tstats, r2=r2, n=n, k=k,
                alpha=float(coef[0]), t_alpha=float(tstats[0]),
                se_alpha=float(se[0]))


def bootstrap_intercept_ci(y: np.ndarray, X_no_const: np.ndarray,
                           block: int = 20, resamples: int = 2000,
                           seed: int = BOOT_SEED) -> dict:
    """Stationary block-bootstrap 95% CI for the DOLLAR intercept (per-day alpha, $)."""
    y = np.asarray(y, dtype=float)
    X_no_const = np.asarray(X_no_const, dtype=float)
    n = len(y)
    rng = np.random.default_rng(seed)
    alphas = np.empty(resamples)
    for b in range(resamples):
        idx = cab._stationary_blocks(n, block, rng)
        res = _ols_multi(y[idx], X_no_const[idx])
        alphas[b] = res["alpha"]
    lo, hi = np.percentile(alphas, [2.5, 97.5])
    return dict(lo=float(lo), hi=float(hi), mean=float(alphas.mean()))


def regress_width(df: pd.DataFrame, width: int, f: str, subset: str = "all",
                  do_boot: bool = True) -> dict:
    """Regress the width's daily P&L ($) on [move, |move|, move^2]. subset in all/train/test."""
    sub = df.copy()
    if subset == "train":
        sub = sub[sub["half"] == "train"]
    elif subset == "test":
        sub = sub[sub["half"] == "test"]
    pcol = width_pnl_col(width, f)
    sub = sub[sub[pcol].notna() & sub["move"].notna()]
    y = sub[pcol].to_numpy(dtype=float)
    X = sub[["move", "abs_move", "move_sq"]].to_numpy(dtype=float)
    if len(y) < 20:
        return dict(width=width, f=f, subset=subset, n=len(y), alpha=float("nan"),
                    t_alpha=float("nan"), r2=float("nan"),
                    beta_move=float("nan"), beta_absmove=float("nan"),
                    beta_movesq=float("nan"), ci_lo=float("nan"), ci_hi=float("nan"),
                    total_pnl=float(np.nansum(y)))
    reg = _ols_multi(y, X)
    ci = bootstrap_intercept_ci(y, X) if do_boot else dict(lo=float("nan"), hi=float("nan"))
    return dict(
        width=width, f=f, subset=subset, n=reg["n"],
        alpha=reg["alpha"], t_alpha=reg["t_alpha"], r2=reg["r2"],
        beta_move=float(reg["coef"][1]), beta_absmove=float(reg["coef"][2]),
        beta_movesq=float(reg["coef"][3]),
        t_move=float(reg["t"][1]), t_absmove=float(reg["t"][2]), t_movesq=float(reg["t"][3]),
        ci_lo=ci["lo"], ci_hi=ci["hi"],
        total_pnl=float(np.nansum(y)),
        mean_pnl=float(np.mean(y)),
    )


# --------------------------------------------------------------------------- #
# TEST 3 — VRP decomposition: entry credit ($ collected) vs realized breach cost ($ paid)
# --------------------------------------------------------------------------- #
def vrp_decompose(df: pd.DataFrame, width: int, f: str) -> dict:
    """Split the per-day P&L into premium sold (entry credit $) and realized cost paid
    (the intrinsic given up on breach days). VRP = credit - realized cost. A short condor's
    P&L IS the realized VRP over the 14:00->16:00 window: it collects the entry credit and
    pays out the capped intrinsic the realized move produced.
    """
    ccol = width_credit_col(width, f)
    pcol = width_pnl_col(width, f)
    sub = df[df[pcol].notna() & df[ccol].notna()].copy()
    # entry credit is in option points -> dollars
    credit_d = sub[ccol].to_numpy(dtype=float) * CONTRACT_MULTIPLIER
    pnl_d = sub[pcol].to_numpy(dtype=float)
    # realized cost paid (capped intrinsic) = credit - pnl  (identity: pnl = credit - cost)
    cost_d = credit_d - pnl_d
    out = dict(
        width=width, f=f, n=len(sub),
        premium_sold=float(credit_d.sum()),
        realized_cost=float(cost_d.sum()),
        vrp=float((credit_d - cost_d).sum()),   # == pnl total, by construction
        premium_per_day=float(credit_d.mean()),
        cost_per_day=float(cost_d.mean()),
    )
    # OOS split of VRP
    for name in ("train", "test"):
        m = sub["half"] == name
        c = sub.loc[m, ccol].to_numpy(dtype=float) * CONTRACT_MULTIPLIER
        p = sub.loc[m, pcol].to_numpy(dtype=float)
        out[f"{name}_premium"] = float(c.sum())
        out[f"{name}_cost"] = float((c - p).sum())
        out[f"{name}_vrp"] = float(p.sum())
        out[f"{name}_n"] = int(m.sum())
    # per-regime VRP (calm vs stress via gamma regime)
    for reg in ("positive", "neutral", "negative"):
        m = sub["gamma_regime"] == reg
        out[f"gamma_{reg}_vrp"] = float(sub.loc[m, pcol].sum())
    return out


# --------------------------------------------------------------------------- #
# TEST 4 — tail-sufficiency / defined-risk stress
# --------------------------------------------------------------------------- #
def tail_characterize(df: pd.DataFrame) -> dict:
    mv = df["move"].dropna()
    return dict(
        n=len(mv),
        worst_down=float(mv.min()), worst_up=float(mv.max()),
        p01=float(mv.quantile(0.01)), p05=float(mv.quantile(0.05)),
        p95=float(mv.quantile(0.95)), p99=float(mv.quantile(0.99)),
        std=float(mv.std()),
        worst_down_day=str(df.loc[mv.idxmin(), "day"].date()),
        n_below_m2pct=int((mv <= -0.02).sum()),
        n_below_m3pct=int((mv <= -0.03).sum()),
    )


def stress_defined_risk(df: pd.DataFrame, width: int, f: str, adverse_move: float) -> dict:
    """Inject a hypothetical adverse 14:00->16:00 move on a REPRESENTATIVE entry, report the
    CAPPED (defined-risk) per-width loss. For a short iron condor the max loss = width - credit
    (per contract, $): a move large enough to blow past the far wing caps the loss there. This
    is what makes the strategy defined-risk. We use the median entry credit at (width, f) as the
    representative credit, and compute the capped loss a crash of `adverse_move` would produce.
    """
    ccol = width_credit_col(width, f)
    med_credit_pts = float(df[ccol].dropna().median())        # option points
    med_credit_d = med_credit_pts * CONTRACT_MULTIPLIER
    med_spot = float(df["entry_spot"].dropna().median())
    # short put strike sits ~0.15 delta below spot; on a crash the put side goes ITM.
    # capped loss = min(intrinsic_at_breach, width) - credit. For any move beyond the far wing
    # the loss is fully capped at the wing: max_loss = width*100 - credit_$.
    move_pts = abs(adverse_move) * med_spot
    # distance from spot to the short put strike (approx from the data at this width)
    scol = f"short_put_k_w{width}"
    med_short_put = float(df[scol].dropna().median())
    dist_to_short = med_spot - med_short_put            # points spot must fall to reach short
    intrinsic_beyond_short = max(move_pts - dist_to_short, 0.0)
    capped_intrinsic = min(intrinsic_beyond_short, float(width))
    loss_d = capped_intrinsic * CONTRACT_MULTIPLIER - med_credit_d
    max_loss_d = float(width) * CONTRACT_MULTIPLIER - med_credit_d   # fully-capped worst case
    return {
        "width": width, "adverse_move": adverse_move,
        "move_pts": move_pts, "dist_to_short": dist_to_short,
        "capped_loss_d": float(loss_d), "max_capped_loss_d": float(max_loss_d),
        "med_credit_d": med_credit_d,
    }


def cluster_drawdown(df: pd.DataFrame, width: int, f: str, n_cluster: int,
                     adverse_move: float) -> dict:
    """What a CLUSTER of n_cluster consecutive fully-capped worst-case days does to the total.
    Each such day loses the fully-capped max (width*100 - credit). Compare to the observed
    total P&L to see how many capped tail days it takes to erase the calm carry."""
    pcol = width_pnl_col(width, f)
    total = float(df[pcol].sum())
    stress = stress_defined_risk(df, width, f, adverse_move)
    per_day_loss = stress["max_capped_loss_d"]   # positive number = dollars lost
    cluster_loss = n_cluster * per_day_loss
    return dict(
        width=width, n_cluster=n_cluster, per_day_capped_loss=per_day_loss,
        cluster_loss=cluster_loss, observed_total=total,
        days_to_erase=(total / per_day_loss if per_day_loss > 0 else float("nan")),
        total_after_cluster=total - cluster_loss,
    )


# --------------------------------------------------------------------------- #
# TEST 2 — passive short-premium benchmark (ATM short straddle, 14:00->cash settle)
# --------------------------------------------------------------------------- #
@dataclass
class StraddleTrade:
    day: str
    traded: bool = False
    skip_reason: str = ""
    atm_strike: float = float("nan")
    entry_spot: float = float("nan")
    settle_spot: float = float("nan")
    credit_mid: float = float("nan")     # option points, mid
    credit_f50: float = float("nan")     # option points, 50% half-spread worse
    credit_full: float = float("nan")    # option points, full worst-side
    pnl_mid_d: float = float("nan")      # $ P&L, mid fill
    pnl_f50_d: float = float("nan")      # $ P&L, f50 fill
    pnl_full_d: float = float("nan")     # $ P&L, full worst-side fill


def _fill_credit(bid: float, ask: float, frac: float) -> float:
    """SELL premium: mid at frac=0, toward BID as frac->1. credit = mid - frac*halfspread."""
    mid = 0.5 * (bid + ask)
    half = 0.5 * (ask - bid)
    return mid - frac * half


def run_passive_straddle_day(d: _dt.date, settle_spot: float,
                             day_data=None) -> StraddleTrade:
    """Passive delta-neutral ATM short straddle: at 14:00 sell the ATM call + ATM put (nearest
    strike to recovered spot), hold to COSTLESS 16:00 cash settlement. Honest entry fills (mid,
    f50, full). This is the naive always-short-premium comparator. NO management, NO wings.
    settle_spot = Arm-5's recovered 16:00 S* (shared, so the two are marked on the SAME move)."""
    import s5_intraday_data as s5
    import s6_recon as recon
    tr = StraddleTrade(day=str(d))
    try:
        dd = day_data if day_data is not None else s5.load_day(d)
        # SPEED: the trade only ever reads the 14:00 snapshot (it enters at 14:00 and settles at
        # the given 16:00 S* — no intraday marking). So we skip the full minute-grid rebuild and
        # recover the 14:00 NBBO directly from the store-on-change kept rows: for each 0DTE
        # contract, take its LAST kept quote AT-OR-BEFORE 14:00. That is exactly the forward-fill
        # value at 14:00 (a contract with no kept row by 14:00 is legitimately absent -> dropped,
        # never back-filled => no look-ahead), computed without a 300x-strike grid rebuild.
        entry_minute = pd.Timestamp(_dt.datetime.combine(d, _dt.time(14, 0)))
        exp_str = d.strftime("%Y-%m-%d")
        q = dd.quote
        q = q[(q["expiration"] == exp_str) & (q["timestamp"] <= entry_minute)]
        if q.empty:
            tr.skip_reason = "no 0dte chain"
            return tr
        # last kept row per (strike, right) at-or-before 14:00 == the 14:00 NBBO
        snap = (q.sort_values("timestamp")
                  .groupby(["strike", "right"], as_index=False)
                  .last()[["strike", "right", "bid", "ask"]])
        if snap.empty:
            tr.skip_reason = "no 14:00 snapshot"
            return tr
        sr = recon.recover_forward_spot(snap, entry_minute, d)
        if sr is None:
            tr.skip_reason = "spot recon failed"
            return tr
        spot = sr.spot
        tr.entry_spot = spot
        tr.settle_spot = settle_spot
        # ATM strike = nearest available strike to recovered spot with BOTH legs quoted
        strikes = sorted(snap["strike"].unique())
        atm = min(strikes, key=lambda k: abs(k - spot))
        crow = snap[(snap["strike"] == atm) & (snap["right"] == "CALL")]
        prow = snap[(snap["strike"] == atm) & (snap["right"] == "PUT")]
        if crow.empty or prow.empty:
            tr.skip_reason = "atm leg missing"
            return tr
        cb, ca = float(crow["bid"].iloc[0]), float(crow["ask"].iloc[0])
        pb, pa = float(prow["bid"].iloc[0]), float(prow["ask"].iloc[0])
        if not all(np.isfinite(v) for v in (cb, ca, pb, pa)) or ca <= 0 or pa <= 0:
            tr.skip_reason = "atm unquoted"
            return tr
        tr.atm_strike = atm
        # settlement intrinsic (cash, costless) at S*
        call_intrinsic = max(settle_spot - atm, 0.0)
        put_intrinsic = max(atm - settle_spot, 0.0)
        settle_cost_pts = call_intrinsic + put_intrinsic
        for fr, lbl in ((0.0, "mid"), (0.5, "f50"), (1.0, "full")):
            credit = _fill_credit(cb, ca, fr) + _fill_credit(pb, pa, fr)
            pnl_pts = credit - settle_cost_pts
            setattr(tr, f"credit_{lbl}", credit)
            setattr(tr, f"pnl_{lbl}_d", pnl_pts * CONTRACT_MULTIPLIER)
        tr.traded = True
        return tr
    except Exception as e:
        tr.skip_reason = f"error: {type(e).__name__}: {e}"
        return tr


def run_passive_straddle(df: pd.DataFrame, verbose: bool = True) -> pd.DataFrame:
    """Run the passive straddle over the SAME traded days as Arm 5, reusing Arm-5's recovered
    settle_spot so both are marked on the identical 14:00->16:00 move. FOREGROUND, supervised,
    resumable via a partial CSV (never detached to a background log)."""
    import s5_intraday_data as s5
    partial = CSV_DIR / "passive_straddle_partial.csv"
    done: set[str] = set()
    if partial.is_file():
        try:
            done = set(pd.read_csv(partial, usecols=["day"])["day"].astype(str))
        except Exception:
            done = set()
    settle_map = {str(r.day.date()): float(r.settle_spot) for r in df.itertuples()}
    days = [d for d in settle_map]
    n = len(days)
    write_header = not partial.is_file()
    import csv as _csv
    with open(partial, "a", newline="") as fh:
        w = _csv.DictWriter(fh, fieldnames=list(asdict(StraddleTrade(day="x")).keys()))
        if write_header:
            w.writeheader()
        for i, dstr in enumerate(days, 1):
            if dstr in done:
                continue
            d = _dt.date.fromisoformat(dstr)
            tr = run_passive_straddle_day(d, settle_map[dstr])
            w.writerow(asdict(tr))
            fh.flush()
            if verbose and (i % 100 == 0 or i == n):
                print(f"[straddle {i}/{n}] {dstr} traded={tr.traded}", flush=True)
    out = pd.read_csv(partial)
    out["traded"] = out["traded"].astype(str).str.lower().isin(["true", "1"])
    out["day"] = pd.to_datetime(out["day"])
    out.to_csv(CSV_DIR / "passive_straddle.csv", index=False)
    return out


def benchmark_headtohead(df: pd.DataFrame, straddle: pd.DataFrame,
                         width: int, f: str) -> dict:
    """Condor vs passive straddle on the SAME days, per year and OOS. Reports condor P&L,
    straddle P&L, and condor-minus-straddle spread. If the condor just tracks passive
    short-vol, the 'edge' is beta."""
    pcol = width_pnl_col(width, f)
    scol = f"pnl_{f}_d" if f in ("mid", "f50", "full") else "pnl_f50_d"
    con = df[["day", "year", "half", pcol]].rename(columns={pcol: "condor"})
    st = straddle[straddle["traded"]][["day", scol]].rename(columns={scol: "straddle"})
    st["day"] = pd.to_datetime(st["day"])
    m = con.merge(st, on="day", how="inner").dropna(subset=["condor", "straddle"])
    m["spread"] = m["condor"] - m["straddle"]
    out = dict(width=width, f=f, n=len(m),
               condor_total=float(m["condor"].sum()),
               straddle_total=float(m["straddle"].sum()),
               spread_total=float(m["spread"].sum()))
    # per year
    out["by_year"] = (m.groupby("year")[["condor", "straddle", "spread"]].sum()
                      .round(0).to_dict("index"))
    # OOS
    out["by_half"] = (m.groupby("half")[["condor", "straddle", "spread"]].sum()
                      .round(0).to_dict("index"))
    # risk-adjusted (daily) — Sharpe on each leg
    cm = cab.daily_metrics(pd.Series(m["condor"].to_numpy() / 100000.0))
    sm = cab.daily_metrics(pd.Series(m["straddle"].to_numpy() / 100000.0))
    out["condor_sharpe"] = cm["sharpe"]
    out["straddle_sharpe"] = sm["sharpe"]
    out["_merged"] = m
    return out


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main(run_straddle: bool = True):
    t0 = time.time()
    print("[arm6] loading Arm 5 per-day P&L...", flush=True)
    df = load_arm5()
    print(f"[arm6] {len(df)} traded days {df['day'].min().date()}..{df['day'].max().date()}",
          flush=True)
    print(f"[arm6] 14:00->16:00 move: mean {df['move'].mean():+.4%} std {df['move'].std():.4%} "
          f"worst {df['move'].min():+.4%} best {df['move'].max():+.4%}", flush=True)

    # ---- TEST 1: regression per clean width, all/train/test ----
    reg_rows = []
    for w in CLEAN_WIDTHS:
        for subset in ("all", "train", "test"):
            reg_rows.append(regress_width(df, w, HEADLINE_F, subset, do_boot=(subset == "all")))
            r = reg_rows[-1]
            print(f"  [w{w} {subset}] alpha=${r['alpha']:.2f}/day t={r['t_alpha']:.2f} "
                  f"R2={r['r2']:.3f} beta|move|={r['beta_absmove']:.0f} n={r['n']}", flush=True)
    reg_df = pd.DataFrame(reg_rows)
    reg_df.to_csv(CSV_DIR / "regression_grid.csv", index=False)

    # ---- TEST 3: VRP decomposition ----
    vrp_rows = [vrp_decompose(df, w, HEADLINE_F) for w in CLEAN_WIDTHS]
    vrp_df = pd.DataFrame(vrp_rows)
    vrp_df.to_csv(CSV_DIR / "vrp_decomposition.csv", index=False)

    # ---- TEST 4: tail characterization + defined-risk stress ----
    tail = tail_characterize(df)
    stress_rows = []
    for w in CLEAN_WIDTHS:
        for mv in STRESS_MOVES:
            stress_rows.append(stress_defined_risk(df, w, HEADLINE_F, mv))
    stress_df = pd.DataFrame(stress_rows)
    stress_df.to_csv(CSV_DIR / "stress_defined_risk.csv", index=False)
    cluster_rows = [cluster_drawdown(df, w, HEADLINE_F, nc, -0.05)
                    for w in CLEAN_WIDTHS for nc in (3, 5, 10)]
    cluster_df = pd.DataFrame(cluster_rows)

    # ---- TEST 2: passive straddle benchmark ----
    h2h = None
    straddle = None
    if run_straddle:
        print("[arm6] running passive ATM short straddle benchmark (foreground)...", flush=True)
        straddle = run_passive_straddle(df)
        ns = int(straddle["traded"].sum())
        print(f"[arm6] straddle done: {ns} traded days", flush=True)
        h2h = {w: benchmark_headtohead(df, straddle, w, HEADLINE_F) for w in CLEAN_WIDTHS}

    write_report(df, reg_df, vrp_df, tail, stress_df, cluster_df, h2h, straddle,
                 time.time() - t0)
    print(f"[arm6] DONE {time.time()-t0:.0f}s -> {REPORT}", flush=True)


def _fmt(x, nd=2):
    if x is None or (isinstance(x, float) and not np.isfinite(x)):
        return "n/a"
    return f"{x:,.{nd}f}"


def write_report(df, reg_df, vrp_df, tail, stress_df, cluster_df, h2h, straddle, runtime_s):
    # --- verdict logic ---
    # headline = w50 f50, all-window
    hl = reg_df[(reg_df["width"] == HEADLINE_WIDTH) & (reg_df["subset"] == "all")].iloc[0]
    hl_train = reg_df[(reg_df["width"] == HEADLINE_WIDTH) & (reg_df["subset"] == "train")].iloc[0]
    hl_test = reg_df[(reg_df["width"] == HEADLINE_WIDTH) & (reg_df["subset"] == "test")].iloc[0]

    alpha = hl["alpha"]; t_alpha = hl["t_alpha"]
    ci_lo, ci_hi = hl["ci_lo"], hl["ci_hi"]
    ci_excludes_0 = (ci_lo > 0) or (ci_hi < 0)
    alpha_pos_all = alpha > 0
    # OOS stability: intercept positive AND meaningfully-signed in BOTH halves
    oos_stable = (hl_train["alpha"] > 0) and (hl_test["alpha"] > 0)
    # plateau: positive intercept across all clean widths (all-window)
    clean_all = reg_df[(reg_df["subset"] == "all")]
    plateau = bool((clean_all["alpha"] > 0).all())
    # passive benchmark: does the condor earn a POSITIVE spread over the passive straddle?
    if h2h is not None:
        spread_total = h2h[HEADLINE_WIDTH]["spread_total"]
        # OOS spread must also be positive in both halves
        bh = h2h[HEADLINE_WIDTH]["by_half"]
        spread_oos_pos = all(bh.get(k, {}).get("spread", -1) > 0 for k in ("train", "test"))
        beats_passive = (spread_total > 0)
    else:
        spread_total = float("nan"); beats_passive = None; spread_oos_pos = None

    # VERDICT — alpha only if: positive intercept, CI excludes 0, OOS-stable, plateau,
    # AND the condor structure earns a positive spread over the passive short-vol benchmark.
    is_alpha = bool(alpha_pos_all and ci_excludes_0 and oos_stable and plateau
                    and (beats_passive if beats_passive is not None else False))
    if is_alpha:
        verdict = ("ALPHA — a real, OOS-stable premium intercept survives after controlling for "
                   "the 14:00->16:00 move AND the condor earns a positive spread over the passive "
                   "short-vol benchmark")
    else:
        # Distinguish the flavor of beta.
        if not ci_excludes_0 or not alpha_pos_all:
            core = ("the intercept is not distinguishable from zero once the realized move is "
                    "controlled (CI spans 0)")
        elif not oos_stable:
            core = "the intercept collapses out of sample"
        elif beats_passive is False:
            core = ("a passive ATM short straddle on the same days harvests the same premium — "
                    "the condor structure adds nothing the naive short-vol position doesn't")
        else:
            core = "it fails a robustness leg (plateau)"
        verdict = f"BETA — the edge is harvested short-vol/short-gamma premium; {core}"

    L = []
    L.append("# ARM 6 — Is Arm 5's condor P&L structural ALPHA or short-vol/short-gamma BETA?\n")
    L.append(f"_Run 2026-07-06. Runtime {runtime_s:.0f}s. PAPER / research only, OFFLINE, "
             f"warehouse read-only. Frozen config untouched._\n")

    L.append("## VERDICT (lead)\n")
    L.append(f"### **{verdict}**\n")
    L.append(f"- **Headline (w50, f50, full window): intercept alpha = ${_fmt(alpha)}/day "
             f"(t = {_fmt(t_alpha)})**, bootstrap 95% CI [${_fmt(ci_lo)}, ${_fmt(ci_hi)}]/day "
             f"({'EXCLUDES' if ci_excludes_0 else 'SPANS'} 0), R² {_fmt(hl['r2'],3)}.")
    L.append(f"- Regression factors are the **realized 14:00->16:00 intraday move** (signed), "
             f"its magnitude |move|, and move² — the exact exposure window of the trade "
             f"(entered 14:00, cash-settled 16:00). NOT close-to-close daily returns.")
    L.append(f"- OOS: intercept ${_fmt(hl_train['alpha'])}/day (train, n={hl_train['n']}) vs "
             f"${_fmt(hl_test['alpha'])}/day (test, n={hl_test['n']}) — "
             f"{'stable' if oos_stable else 'NOT stable'}.")
    if h2h is not None:
        L.append(f"- Passive ATM short-straddle benchmark on the SAME days: condor "
                 f"${_fmt(h2h[HEADLINE_WIDTH]['condor_total'],0)} vs straddle "
                 f"${_fmt(h2h[HEADLINE_WIDTH]['straddle_total'],0)} => condor-minus-straddle "
                 f"spread **${_fmt(spread_total,0)}** "
                 f"({'condor adds value' if spread_total>0 else 'condor adds NOTHING passive short-vol doesn'+chr(39)+'t'}).")
    L.append("")
    L.append("> **0DTE tail-sample limitation (flagged prominently):** SPX 0DTE dailies begin "
             "~2022, so the entire sample is 2022-01→2026-07. COVID-scale (2020) and 2018-Q4 "
             "intraday crashes are **OUT of sample** — the window does not contain a severe "
             "systemic intraday tail. Any 'survives the tail' read is bounded by this; see §4.\n")

    # --- exposure window statement ---
    L.append("## The exposure window (stated first — load-bearing)\n")
    L.append(f"Arm 5 enters at 14:00 (`entry_spot`) and resolves at 16:00 costless cash "
             f"intrinsic against the recovered 16:00 level S* (`settle_spot`). The **entire** "
             f"P&L is a function of the realized **14:00->16:00 move** = "
             f"`(settle_spot - entry_spot)/entry_spot`. A short condor is short realized "
             f"variance over exactly this 2-hour window, so the regression controls for that "
             f"move (signed), |move|, and move². The intercept = premium harvested independent "
             f"of the realized move.\n")
    L.append(f"- Realized 14:00->16:00 move over {tail['n']} traded days: mean "
             f"{df['move'].mean():+.4%}, std {tail['std']:.4%}, "
             f"p01 {tail['p01']:+.3%}, p99 {tail['p99']:+.3%}.\n")

    # --- TEST 1 table ---
    L.append("## §1 Alpha-vs-beta regression — daily P&L $ ~ intercept + b·move + b·|move| + b·move²\n")
    L.append("Per clean width (w20/w30/w50) at the f50 fill; intercept ($/day) = alpha.\n")
    L.append("| width | subset | n | **alpha $/day** | t(alpha) | 95% CI $/day | R² | "
             "β(move) | β(|move|) | β(move²) | total P&L $ |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for _, r in reg_df.iterrows():
        ci = (f"[{_fmt(r['ci_lo'])}, {_fmt(r['ci_hi'])}]"
              if np.isfinite(r["ci_lo"]) else "n/a")
        L.append(f"| w{int(r['width'])} | {r['subset']} | {int(r['n'])} | "
                 f"**{_fmt(r['alpha'])}** | {_fmt(r['t_alpha'])} | {ci} | {_fmt(r['r2'],3)} | "
                 f"{_fmt(r['beta_move'],0)} | {_fmt(r['beta_absmove'],0)} | "
                 f"{_fmt(r['beta_movesq'],0)} | {_fmt(r['total_pnl'],0)} |")
    L.append("")
    L.append("_Reading: a positive, OOS-stable intercept whose CI excludes 0 = premium harvested "
             "independent of the realized move (alpha). An intercept that collapses across the "
             "train/test split or whose CI spans 0 once |move| is included = beta._\n")

    # --- TEST 2 head-to-head ---
    if h2h is not None:
        L.append("## §2 Passive short-premium benchmark — ATM short straddle, same days\n")
        L.append("Delta-neutral ATM short straddle entered 14:00, held to costless 16:00 cash "
                 "settlement, honest entry fills (f50). The naive always-short-premium comparator "
                 "(short vol, ~0 net delta, NO wings). If the condor merely tracks it, the 'edge' "
                 "is beta.\n")
        L.append("| width | n | condor $ | straddle $ | spread (C−S) $ | condor Sharpe | "
                 "straddle Sharpe |")
        L.append("|---|---|---|---|---|---|---|")
        for w in CLEAN_WIDTHS:
            h = h2h[w]
            L.append(f"| w{w} | {h['n']} | {_fmt(h['condor_total'],0)} | "
                     f"{_fmt(h['straddle_total'],0)} | {_fmt(h['spread_total'],0)} | "
                     f"{_fmt(h['condor_sharpe'])} | {_fmt(h['straddle_sharpe'])} |")
        L.append("")
        # per year + OOS for headline width
        h = h2h[HEADLINE_WIDTH]
        L.append(f"### Head-to-head by year & OOS (w{HEADLINE_WIDTH}, f50)\n")
        L.append("| bucket | condor $ | straddle $ | spread (C−S) $ |")
        L.append("|---|---|---|---|")
        for yr, row in sorted(h["by_year"].items()):
            L.append(f"| {yr} | {_fmt(row['condor'],0)} | {_fmt(row['straddle'],0)} | "
                     f"{_fmt(row['spread'],0)} |")
        for half in ("train", "test"):
            if half in h["by_half"]:
                row = h["by_half"][half]
                L.append(f"| **{half}** | {_fmt(row['condor'],0)} | {_fmt(row['straddle'],0)} | "
                         f"{_fmt(row['spread'],0)} |")
        L.append("")
        L.append("_The condor is defined-risk (capped wings); the passive straddle is uncapped. "
                 "A positive, OOS-stable spread means the condor STRUCTURE earns something the "
                 "naive short-vol position does not — that would be structural, not beta. A spread "
                 "≈0 or negative means the condor just harvests the same short-vol premium._\n")

    # --- TEST 3 VRP ---
    L.append("## §3 VRP decomposition — premium sold vs realized cost paid\n")
    L.append("A short condor's P&L IS the realized VRP over the 14:00->16:00 window: it collects "
             "the entry credit and pays the capped intrinsic the realized move produced. "
             "VRP = premium sold − realized cost (= total P&L, by identity).\n")
    L.append("| width | premium sold $ | realized cost $ | VRP (=P&L) $ | train VRP $ | "
             "test VRP $ | γ+ VRP $ | γ0 VRP $ | γ− VRP $ |")
    L.append("|---|---|---|---|---|---|---|---|---|")
    for _, r in vrp_df.iterrows():
        L.append(f"| w{int(r['width'])} | {_fmt(r['premium_sold'],0)} | "
                 f"{_fmt(r['realized_cost'],0)} | {_fmt(r['vrp'],0)} | {_fmt(r['train_vrp'],0)} | "
                 f"{_fmt(r['test_vrp'],0)} | {_fmt(r['gamma_positive_vrp'],0)} | "
                 f"{_fmt(r['gamma_neutral_vrp'],0)} | {_fmt(r['gamma_negative_vrp'],0)} |")
    L.append("")
    L.append("_γ+ = positive-gamma (calm) regime, γ− = negative-gamma (stress). If the VRP is "
             "concentrated in the calm (γ+) bucket and thin/negative in stress, the carry is "
             "regime-dependent short-vol premium._\n")

    # --- TEST 4 tail/stress ---
    L.append("## §4 Tail-sufficiency & defined-risk stress\n")
    L.append(f"**(a) Does the 2022-2026 window even contain severe intraday tails?**\n")
    L.append(f"- Worst realized 14:00->16:00 move: **{tail['worst_down']:+.3%}** "
             f"({tail['worst_down_day']}); best {tail['worst_up']:+.3%}. "
             f"p01 {tail['p01']:+.3%}, p05 {tail['p05']:+.3%}.")
    L.append(f"- Days with a ≤ −2% 2h move: {tail['n_below_m2pct']}; ≤ −3%: "
             f"{tail['n_below_m3pct']} out of {tail['n']}.")
    L.append(f"- **Limitation:** the worst 2h move in-sample is only ~{abs(tail['worst_down']):.1%}. "
             f"A COVID-scale intraday crash (−7% to −12% in 2h) is OUT of sample (0DTE dailies "
             f"start ~2022). The window does NOT stress-test a systemic tail.\n")
    L.append("**(b) Defined-risk stress — capped per-width loss under a hypothetical adverse "
             "14:00->16:00 move** (median entry credit as the representative trade):\n")
    L.append("| width | −1% | −2% | −3% | −5% | −7% | max-capped loss $ |")
    L.append("|---|---|---|---|---|---|---|")
    for w in CLEAN_WIDTHS:
        cells = {}
        maxloss = float("nan")
        for _, s in stress_df[stress_df["width"] == w].iterrows():
            cells[s["adverse_move"]] = s["capped_loss_d"]
            maxloss = s["max_capped_loss_d"]
        L.append(f"| w{w} | {_fmt(cells.get(-0.01),0)} | {_fmt(cells.get(-0.02),0)} | "
                 f"{_fmt(cells.get(-0.03),0)} | {_fmt(cells.get(-0.05),0)} | "
                 f"{_fmt(cells.get(-0.07),0)} | {_fmt(maxloss,0)} |")
    L.append("")
    L.append("_Loss is per-contract $ (credit − capped intrinsic). Beyond the far wing the loss "
             "is fully capped at width×100 − credit — that cap is what makes the strategy "
             "defined-risk; a −5% and a −50% crash cost the same capped amount._\n")
    L.append("**(c) Cluster stress — how many fully-capped worst-case days erase the calm carry** "
             f"(w{HEADLINE_WIDTH}, f50):\n")
    L.append("| width | observed total $ | per-day capped loss $ | days-to-erase | "
             "total after 3-day cluster $ | after 5-day $ | after 10-day $ |")
    L.append("|---|---|---|---|---|---|---|")
    for w in CLEAN_WIDTHS:
        sub = cluster_df[cluster_df["width"] == w]
        c3 = sub[sub["n_cluster"] == 3].iloc[0]
        c5 = sub[sub["n_cluster"] == 5].iloc[0]
        c10 = sub[sub["n_cluster"] == 10].iloc[0]
        L.append(f"| w{w} | {_fmt(c3['observed_total'],0)} | {_fmt(c3['per_day_capped_loss'],0)} "
                 f"| {_fmt(c3['days_to_erase'],1)} | {_fmt(c3['total_after_cluster'],0)} | "
                 f"{_fmt(c5['total_after_cluster'],0)} | {_fmt(c10['total_after_cluster'],0)} |")
    L.append("")
    L.append("_'days-to-erase' = how many fully-capped max-loss days it takes to wipe the "
             "observed multi-year total. A small number means the positive total is calm-carry a "
             "cluster of tail days would erase; a large number means the carry has real cushion. "
             "Note the cap bounds each day's loss — this is the upside of defined-risk vs the "
             "uncapped straddle in §2._\n")

    L.append("## Method notes\n")
    L.append("- Data: Arm 5 per-day P&L per width per fill from "
             "`output/condor_cashsettle_hold/condor_cashsettle_hold_days.csv` "
             "(entry_spot @14:00, recovered settle_spot @16:00 = S*).")
    L.append("- Regression + block bootstrap reuse `csp_alpha_beta` "
             "(`ols_alpha_beta` generalized to multi-factor here; `_stationary_blocks` block=20, "
             f"2000 resamples, seed {BOOT_SEED}).")
    L.append("- Passive straddle benchmark reuses `s6_control`/`s6_recon` (14:00 entry, recovered "
             "spot, honest fills) and Arm-5's own recovered settle_spot, so condor & straddle are "
             "marked on the IDENTICAL 14:00->16:00 move. Costless cash settlement (European SPXW).")
    L.append("- OOS split 2024-06-30 (train ≤ split < test). No parameter tuned to the data. "
             "Warehouse strictly read-only.\n")

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text("\n".join(L), encoding="utf-8")


if __name__ == "__main__":
    main()
