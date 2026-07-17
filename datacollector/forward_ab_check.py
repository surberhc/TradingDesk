"""
forward_ab_check.py — daily ThetaData-vs-IBKR forward-validation A/B check.

We are running the IBKR forward option collector (ibkr_forward_live.py, port-4001
live-data Gateway) in PARALLEL with the existing ThetaData EOD collector so we can
validate IBKR against ThetaData every day BEFORE ThetaData's one-month subscription
lapses. To let the two coexist for the same root/day (they would otherwise collide
in raw/options via storage.have_day), IBKR writes to a separate namespace
(config.RAW_OPTIONS_IBKR = raw/options_ibkr). This check diffs the two.

It is OFFLINE and PARQUET-ONLY: no Gateway, no network. It reads the raw ThetaData
parquet (config.RAW_OPTIONS) and the raw IBKR parquet (config.RAW_OPTIONS_IBKR) for
SPX and SPXW, compares coverage, greeks presence (the delayed-data red flag), and
the frozen GEX features — reusing features.gex.day_features() so no gamma math is
reimplemented here.

Target day: default = the most recent YYYYMMDD present as a parquet on BOTH sides
for BOTH SPX and SPXW; or force one via a single CLI arg `YYYYMMDD`. If there is no
common day yet (expected before IBKR's first live pull) it records a "stale" status
and exits 0 — nothing to compare.

Artifacts (under config.DATA_ROOT): appends a human-readable block to
forward_ab_check.log, refreshes forward_ab_heartbeat.txt, and writes the
"forward_ab_check" jobstatus key the EOD reporter aggregates. Prints a concise
comparison table to stdout. Never raises on a bad/missing day — it degrades to a
status + message like the other collectors. Exit 0 on ok/warn/skip.

Run manually:  <venv python> forward_ab_check.py [YYYYMMDD]
"""

from __future__ import annotations

import datetime as dt
import pathlib
import sys

import numpy as np
import pandas as pd

import config
from features import gex

# status.py lives in the sibling dailyreport project (the EOD reporter reads it).
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "dailyreport"))
import status as jobstatus  # noqa: E402

# --------------------------------------------------------------------------- #
# What we compare + tunable verdict thresholds (module-level so they're easy to
# adjust without touching the logic).
# --------------------------------------------------------------------------- #
SYMBOLS = ["SPX", "SPXW"]

# The IV column in the warehouse chain (ThetaData + IBKR both name it this — see
# ibkr_forward_live.SCHEMA_COLS and features.gex._prep, which reads implied_vol).
IV_COL = "implied_vol"

# net_gex relative % difference bands (IBKR vs ThetaData).
NET_GEX_OK_PCT = 15.0     # within this -> ok
NET_GEX_WARN_PCT = 35.0   # within this -> warn; beyond -> fail

# IBKR gamma-present fraction (rows with non-null, non-zero gamma). This is the
# key delayed-greeks red flag: delayed data often returns quotes but no greeks.
GAMMA_FRAC_WARN = 0.90    # below -> warn
GAMMA_FRAC_FAIL = 0.50    # below -> fail

# gamma_state (Positive/Neutral/Negative) MUST match exactly — a mismatch is a fail.
GAMMA_STATE_MUST_MATCH = True

_VERDICT_RANK = {"ok": 0, "warn": 1, "fail": 2}

LOG_NAME = "forward_ab_check.log"
HEARTBEAT_NAME = "forward_ab_heartbeat.txt"
JOB_KEY = "forward_ab_check"

# jobstatus vocabulary is ok | partial | fail | stale — map our verdicts onto it.
_STATUS_MAP = {"ok": "ok", "warn": "partial", "fail": "fail", "skip": "stale"}


# --------------------------------------------------------------------------- #
# Paths (resolved at call time from config.DATA_ROOT so tests can monkeypatch it)
# --------------------------------------------------------------------------- #
def _log_path() -> pathlib.Path:
    return config.DATA_ROOT / LOG_NAME


def _heartbeat_path() -> pathlib.Path:
    return config.DATA_ROOT / HEARTBEAT_NAME


def _log(msg: str) -> None:
    line = f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    try:
        print(line, flush=True)
    except Exception:
        pass
    try:
        p = _log_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Day resolution (offline: scan the parquet directories)
# --------------------------------------------------------------------------- #
def _days(base: pathlib.Path, sym: str) -> set[str]:
    d = base / sym
    if not d.exists():
        return set()
    return {p.stem for p in d.glob("*.parquet")
            if len(p.stem) == 8 and p.stem.isdigit()}


def _resolve_day(forced: str | None) -> str | None:
    """Latest YYYYMMDD present on BOTH sides for BOTH symbols, or the forced day."""
    if forced:
        return forced
    common: set[str] | None = None
    for sym in SYMBOLS:
        both = _days(config.RAW_OPTIONS_IBKR, sym) & _days(config.RAW_OPTIONS, sym)
        common = both if common is None else (common & both)
    if not common:
        return None
    return max(common)


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #
def _coverage(df: pd.DataFrame) -> dict:
    have_strike = "strike" in df.columns and len(df)
    return {
        "contracts": int(len(df)),
        "strikes": int(df["strike"].nunique()) if "strike" in df.columns else 0,
        "expirations": int(df["expiration"].nunique()) if "expiration" in df.columns else 0,
        "strike_min": float(df["strike"].min()) if have_strike else None,
        "strike_max": float(df["strike"].max()) if have_strike else None,
    }


def _greeks_presence(df: pd.DataFrame) -> tuple[float, float]:
    """(gamma_present_frac, iv_present_frac). gamma present = non-null & non-zero."""
    n = len(df)
    if n == 0:
        return 0.0, 0.0
    if "gamma" in df.columns:
        g = pd.to_numeric(df["gamma"], errors="coerce")
        gamma_frac = float(((g.notna()) & (g != 0)).sum()) / n
    else:
        gamma_frac = 0.0
    if IV_COL in df.columns:
        iv = pd.to_numeric(df[IV_COL], errors="coerce")
        iv_frac = float(iv.notna().sum()) / n
    else:
        iv_frac = 0.0
    return gamma_frac, iv_frac


def _rel_pct(ibkr, theta) -> float | None:
    """Relative % difference of IBKR vs ThetaData (|a-b|/|b|*100)."""
    if ibkr is None or theta is None:
        return None
    try:
        if theta == 0:
            return 0.0 if ibkr == 0 else float("inf")
        return abs(float(ibkr) - float(theta)) / abs(float(theta)) * 100.0
    except (TypeError, ValueError):
        return None


def _isnan(x) -> bool:
    try:
        return x is None or (isinstance(x, float) and np.isnan(x))
    except TypeError:
        return False


def _compare_symbol(sym: str, theta: pd.DataFrame, ibkr: pd.DataFrame) -> dict:
    """Full A/B comparison for one symbol; returns a result dict incl. verdict."""
    cov_t, cov_i = _coverage(theta), _coverage(ibkr)
    gfrac_t, ivfrac_t = _greeks_presence(theta)
    gfrac_i, ivfrac_i = _greeks_presence(ibkr)

    feat_t = gex.day_features(theta) or {}
    feat_i = gex.day_features(ibkr) or {}

    reasons: list[str] = []
    verdict = "ok"

    def _bump(v: str) -> None:
        nonlocal verdict
        if _VERDICT_RANK[v] > _VERDICT_RANK[verdict]:
            verdict = v

    # GEX must be computable on both sides at all.
    if not feat_t or not feat_i:
        reasons.append("GEX unusable (day_features returned {} on "
                       + ("ThetaData" if not feat_t else "IBKR") + ")")
        _bump("fail")

    net_t, net_i = feat_t.get("net_gex"), feat_i.get("net_gex")
    net_rel = _rel_pct(net_i, net_t)
    if net_rel is not None:
        if net_rel <= NET_GEX_OK_PCT:
            pass
        elif net_rel <= NET_GEX_WARN_PCT:
            reasons.append(f"net_gex diff {net_rel:.1f}% > {NET_GEX_OK_PCT:.0f}%")
            _bump("warn")
        else:
            reasons.append(f"net_gex diff {net_rel:.1f}% > {NET_GEX_WARN_PCT:.0f}%")
            _bump("fail")

    state_t, state_i = feat_t.get("gamma_state"), feat_i.get("gamma_state")
    state_match = (state_t == state_i) and state_t is not None
    if GAMMA_STATE_MUST_MATCH and feat_t and feat_i and not state_match:
        reasons.append(f"gamma_state mismatch (Theta={state_t} IBKR={state_i})")
        _bump("fail")

    flip_t, flip_i = feat_t.get("gamma_flip"), feat_i.get("gamma_flip")
    flip_abs = (None if _isnan(flip_t) or _isnan(flip_i)
                else abs(float(flip_i) - float(flip_t)))
    flip_pct = None if (_isnan(flip_t) or _isnan(flip_i)) else _rel_pct(flip_i, flip_t)

    spot_t, spot_i = feat_t.get("spot"), feat_i.get("spot")
    spot_abs = (None if _isnan(spot_t) or _isnan(spot_i)
                else abs(float(spot_i) - float(spot_t)))

    # Delayed-greeks red flag — keyed on the IBKR gamma-present fraction.
    if gfrac_i < GAMMA_FRAC_FAIL:
        reasons.append(f"IBKR gamma-present fraction {gfrac_i:.2f} < {GAMMA_FRAC_FAIL:.2f}")
        _bump("fail")
    elif gfrac_i < GAMMA_FRAC_WARN:
        reasons.append(f"IBKR gamma-present fraction {gfrac_i:.2f} < {GAMMA_FRAC_WARN:.2f}")
        _bump("warn")

    return {
        "symbol": sym,
        "verdict": verdict,
        "reasons": reasons,
        "coverage": {
            "theta": cov_t, "ibkr": cov_i,
            "d_contracts": cov_i["contracts"] - cov_t["contracts"],
            "d_strikes": cov_i["strikes"] - cov_t["strikes"],
            "d_expirations": cov_i["expirations"] - cov_t["expirations"],
        },
        "greeks": {
            "theta_gamma_frac": gfrac_t, "ibkr_gamma_frac": gfrac_i,
            "theta_iv_frac": ivfrac_t, "ibkr_iv_frac": ivfrac_i,
        },
        "gex": {
            "theta_net_gex": net_t, "ibkr_net_gex": net_i, "net_gex_rel_pct": net_rel,
            "theta_gamma_state": state_t, "ibkr_gamma_state": state_i,
            "gamma_state_match": bool(state_match),
            "theta_flip": flip_t, "ibkr_flip": flip_i,
            "flip_abs": flip_abs, "flip_pct": flip_pct,
            "theta_spot": spot_t, "ibkr_spot": spot_i, "spot_abs": spot_abs,
        },
    }


def _worst(verdicts: list[str]) -> str:
    if not verdicts:
        return "skip"
    return max(verdicts, key=lambda v: _VERDICT_RANK[v])


def _fmt_num(x, nd=2) -> str:
    if x is None or _isnan(x):
        return "  n/a"
    if isinstance(x, float) and (x == float("inf") or x == float("-inf")):
        return "  inf"
    try:
        return f"{float(x):,.{nd}f}"
    except (TypeError, ValueError):
        return str(x)


def _print_table(day: str, results: list[dict]) -> None:
    print(f"\nForward A/B check — day {day}")
    print("-" * 72)
    for r in results:
        c, g, x = r["coverage"], r["greeks"], r["gex"]
        print(f"[{r['symbol']}]  verdict={r['verdict'].upper()}")
        print(f"  contracts   Theta={c['theta']['contracts']:<7} "
              f"IBKR={c['ibkr']['contracts']:<7} d={c['d_contracts']:+}")
        print(f"  strikes     Theta={c['theta']['strikes']:<7} "
              f"IBKR={c['ibkr']['strikes']:<7} d={c['d_strikes']:+}")
        print(f"  expirations Theta={c['theta']['expirations']:<7} "
              f"IBKR={c['ibkr']['expirations']:<7} d={c['d_expirations']:+}")
        print(f"  gamma-frac  Theta={g['theta_gamma_frac']:.2f}    "
              f"IBKR={g['ibkr_gamma_frac']:.2f}")
        print(f"  iv-frac     Theta={g['theta_iv_frac']:.2f}    "
              f"IBKR={g['ibkr_iv_frac']:.2f}")
        print(f"  net_gex     Theta={_fmt_num(x['theta_net_gex'],0)}  "
              f"IBKR={_fmt_num(x['ibkr_net_gex'],0)}  "
              f"rel={_fmt_num(x['net_gex_rel_pct'],1)}%")
        print(f"  gamma_state Theta={x['theta_gamma_state']}  "
              f"IBKR={x['ibkr_gamma_state']}  match={x['gamma_state_match']}")
        print(f"  gamma_flip  Theta={_fmt_num(x['theta_flip'])}  "
              f"IBKR={_fmt_num(x['ibkr_flip'])}  "
              f"abs={_fmt_num(x['flip_abs'])}  pct={_fmt_num(x['flip_pct'],1)}%")
        print(f"  spot        Theta={_fmt_num(x['theta_spot'])}  "
              f"IBKR={_fmt_num(x['ibkr_spot'])}  abs={_fmt_num(x['spot_abs'])}")
        if r["reasons"]:
            print(f"  reasons: {'; '.join(r['reasons'])}")
    print("-" * 72)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def run(forced_day: str | None = None) -> dict:
    """Do the whole check. Returns a summary dict; never raises on a bad day."""
    day = _resolve_day(forced_day)
    if day is None:
        msg = ("no common day present on both sides for SPX+SPXW yet "
               "(expected before IBKR's first live pull) — nothing to compare")
        _log(f"SKIP: {msg}")
        _heartbeat_path().parent.mkdir(parents=True, exist_ok=True)
        _heartbeat_path().write_text(
            f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}  ----  SKIP  {msg}")
        jobstatus.write(JOB_KEY, _STATUS_MAP["skip"], message=msg)
        return {"day": None, "overall": "skip", "symbols": {}, "message": msg}

    results: list[dict] = []
    skipped: list[str] = []
    for sym in SYMBOLS:
        tp = config.RAW_OPTIONS / sym / f"{day}.parquet"
        ip = config.RAW_OPTIONS_IBKR / sym / f"{day}.parquet"
        if not tp.exists() or not ip.exists():
            skipped.append(sym)
            _log(f"  {sym}: missing on {'ThetaData' if not tp.exists() else 'IBKR'} "
                 f"side for {day} — skipped")
            continue
        try:
            theta = pd.read_parquet(tp)
            ibkr = pd.read_parquet(ip)
        except Exception as e:
            skipped.append(sym)
            _log(f"  {sym}: unreadable parquet ({e!r}) — skipped")
            continue
        results.append(_compare_symbol(sym, theta, ibkr))

    overall = _worst([r["verdict"] for r in results])
    _print_table(day, results)

    by_sym = {r["symbol"]: r["verdict"] for r in results}
    _log(f"=== forward_ab_check {day} overall={overall.upper()} "
         f"{by_sym} skipped={skipped} ===")
    for r in results:
        if r["reasons"]:
            _log(f"    {r['symbol']} reasons: {'; '.join(r['reasons'])}")

    sym_summary = " ".join(f"{s}={v.upper()}" for s, v in by_sym.items())
    _heartbeat_path().parent.mkdir(parents=True, exist_ok=True)
    _heartbeat_path().write_text(
        f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}  {day}  {overall.upper()}  "
        f"{sym_summary}" + (f"  skipped={','.join(skipped)}" if skipped else ""))

    metrics = {
        "day": day,
        "symbols": {r["symbol"]: {
            "verdict": r["verdict"],
            "ibkr_gamma_frac": round(r["greeks"]["ibkr_gamma_frac"], 3),
            "theta_gamma_frac": round(r["greeks"]["theta_gamma_frac"], 3),
            "net_gex_rel_pct": (None if r["gex"]["net_gex_rel_pct"] is None
                                else round(r["gex"]["net_gex_rel_pct"], 2)),
            "gamma_state_match": r["gex"]["gamma_state_match"],
        } for r in results},
        "skipped": skipped,
    }
    reason_txt = "; ".join(f"{r['symbol']}: {' / '.join(r['reasons'])}"
                           for r in results if r["reasons"]) or "all within thresholds"
    jobstatus.write(JOB_KEY, _STATUS_MAP.get(overall, "fail"), metrics=metrics,
                    message=f"ThetaData vs IBKR A/B ({day}): {overall.upper()} — {reason_txt}",
                    day=day)

    return {"day": day, "overall": overall, "symbols": by_sym,
            "skipped": skipped, "results": results}


def main() -> None:
    forced = None
    for a in sys.argv[1:]:
        if not a.startswith("--") and len(a) == 8 and a.isdigit():
            forced = a
            break
    run(forced)
    # Exit 0 on ok/warn/skip; the jobstatus + heartbeat carry the verdict. We keep
    # it simple and never hard-exit nonzero (mirrors the other collectors).
    sys.exit(0)


if __name__ == "__main__":
    main()
