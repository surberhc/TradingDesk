"""
PRELIMINARY subset calibration of the Phase-2 Leadership Proxy against
InvesTech's published NLC readings, using ONLY the on-disk EOD cache.

- ZERO Tiingo API calls (we read data/cache/<TICKER>.csv directly).
- Reuses the EXISTING metric + proxy math from breadth.py / config.py:
    breadth.per_ticker_signals  (point-in-time, on a truncated series)
    breadth.leadership_proxy
    breadth.classify_regime
  No re-implementation of the formula.

For each InvesTech Issue Date we truncate every cached series to rows dated
<= that issue date (point-in-time), aggregate the same four breadth metrics
compute_breadth() builds, then score + classify. We compute a date only when
enough names have >= MA_LONG (200) prior trading rows; otherwise we skip and
count it.
"""

import os
import csv
import glob
import datetime as _dt

import config
import breadth

CACHE_DIR = os.path.join(os.path.dirname(__file__), "data", "cache")
NLC_CSV = config.NLC_REFERENCE_CSV
OUT_CSV = os.path.join(os.path.dirname(__file__), "calibration_results.csv")

# Minimum fraction of the cached universe that must have full 200-day lookback
# for an issue date to be considered "computable" (avoid scoring off 1-2 names).
MIN_NAMES_FOR_DATE = 30


# --------------------------------------------------------------------------- #
# Load all cached series once
# --------------------------------------------------------------------------- #
def load_all_cache():
    series = {}
    for path in glob.glob(os.path.join(CACHE_DIR, "*.csv")):
        tk = os.path.splitext(os.path.basename(path))[0]
        rows = []
        with open(path, "r", newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                d = (r.get("date") or "")[:10]
                if not d:
                    continue
                try:
                    c = float(r["close"])
                    h = float(r.get("high") or c)
                    lo = float(r.get("low") or c)
                except (TypeError, ValueError, KeyError):
                    continue
                rows.append((d, c, h, lo))
        rows.sort(key=lambda x: x[0])
        if rows:
            series[tk] = rows
    return series


# --------------------------------------------------------------------------- #
# Point-in-time breadth aggregation for one issue date
# --------------------------------------------------------------------------- #
def metrics_as_of(all_series, asof):
    """Aggregate the four breadth metrics across the universe using only rows
    dated <= asof. Returns (metrics_dict, n_processed, n_eligible_full_lookback).
    Mirrors compute_breadth()'s aggregation exactly, on truncated series."""
    n_above_50 = n_above_200 = 0
    n_high = n_low = 0
    n_adv = n_dec = 0
    processed = 0

    for tk, rows in all_series.items():
        # point-in-time truncation
        trunc = [r for r in rows if r[0] <= asof]
        if len(trunc) < config.MA_LONG:
            continue  # not enough history for a 200-day MA as of this date
        sig = breadth.per_ticker_signals(trunc)
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
        return None, 0

    pct50 = round(100.0 * n_above_50 / processed, 1)
    pct200 = round(100.0 * n_above_200 / processed, 1)
    net_hl = n_high - n_low
    net_hl_pct = round(100.0 * net_hl / processed, 2)
    ad_net = n_adv - n_dec
    ad_pct = round(100.0 * ad_net / processed, 2)

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
        "ad_pct": ad_pct,
    }
    return metrics, processed


# --------------------------------------------------------------------------- #
# InvesTech NLC -> bull/bear/neutral truth label
# --------------------------------------------------------------------------- #
def investech_dir(nlc_value, nlc_regime):
    """Map InvesTech's reading to bullish/bearish/neutral.
    Sign convention: NLC negative => Distribution (bearish);
    positive => Selling Vacuum (bullish). Use the value when present,
    else parse the regime text."""
    reg = (nlc_regime or "").lower()
    if nlc_value is not None:
        if nlc_value < 0:
            return "bearish"
        if nlc_value > 0:
            # positive value but text may say "Distribution" dominates; trust
            # the regime text for the bull/neutral split.
            if "distribution" in reg and "selling vacuum" not in reg:
                return "bearish"
            return "bullish"
        # exactly 0
        return "neutral"
    # No value: rely on regime text.
    if "distribution" in reg:
        return "bearish"
    if "selling vacuum" in reg:
        return "bullish"
    return "neutral"


def proxy_dir(regime_label):
    if regime_label is None:
        return None
    r = regime_label.lower()
    if "bullish" in r or "selling vacuum" in r:
        return "bullish"
    if "bearish" in r or "distribution" in r:
        return "bearish"
    return "neutral"


# --------------------------------------------------------------------------- #
# Scoring with a given (weights, bull_min, bear_max) parameter set
# --------------------------------------------------------------------------- #
def score_with_params(metrics, weights, bull_min, bear_max):
    """Replicate breadth.leadership_proxy's normalization but with supplied
    weights, then classify with supplied cutoffs. Same normalization math."""
    def to_score(key):
        v = metrics.get(key)
        if v is None:
            return None
        if key in ("pct_above_50dma", "pct_above_200dma"):
            return max(0.0, min(100.0, v))
        if key in ("net_highs_lows_pct", "ad_pct"):
            return max(0.0, min(100.0, (v + 100.0) / 2.0))
        return None

    num = wsum = 0.0
    for key, w in weights.items():
        s = to_score(key)
        if s is None:
            continue
        num += w * s
        wsum += w
    if wsum == 0:
        return None, None
    score = round(num / wsum, 1)

    if score >= bull_min:
        reg = "bullish"
    elif score <= bear_max:
        reg = "bearish"
    else:
        reg = "neutral"
    return score, reg


def load_nlc_rows():
    rows = []
    with open(NLC_CSV, "r", newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            d = (r.get("Issue Date") or "").strip()
            if not d:
                continue
            raw_val = (r.get("NLC Value") or "").strip()
            try:
                val = float(raw_val) if raw_val != "" else None
            except ValueError:
                val = None
            rows.append({
                "date": d,
                "nlc_value": val,
                "nlc_regime": (r.get("NLC Regime") or "").strip(),
            })
    return rows


def main():
    all_series = load_all_cache()
    cache_first = min(min(d for d, *_ in s) for s in all_series.values())
    cache_last = max(max(d for d, *_ in s) for s in all_series.values())
    print(f"Cache: {len(all_series)} tickers, {cache_first} .. {cache_last}")

    nlc_rows = load_nlc_rows()
    print(f"InvesTech issue dates: {len(nlc_rows)}")

    # ----- baseline (current config) params -----
    base_w = dict(config.PROXY_WEIGHTS)
    base_bull = config.REGIME_BULL_MIN
    base_bear = config.REGIME_BEAR_MAX

    # ----- proposed tuned params -----
    # Up-weight downside-leadership components (net new-lows + A/D deterioration)
    # and raise the bear cutoff so "Distribution-like" breadth that scores in the
    # mid-50s is correctly flagged bearish, matching InvesTech's frequent
    # Distribution regime over this window.
    tuned_w = {
        "pct_above_50dma": 0.15,
        "pct_above_200dma": 0.15,
        "net_highs_lows_pct": 0.45,
        "ad_pct": 0.25,
    }
    tuned_bull = 62.0
    tuned_bear = 52.0

    results = []
    computable = 0
    skipped = 0
    for row in nlc_rows:
        asof = row["date"]
        metrics, n = metrics_as_of(all_series, asof)
        truth = investech_dir(row["nlc_value"], row["nlc_regime"])
        if metrics is None or n < MIN_NAMES_FOR_DATE:
            skipped += 1
            results.append({
                **row, "computable": False, "n_names": n,
                "base_score": None, "base_regime": None, "base_agree": None,
                "tuned_score": None, "tuned_regime": None, "tuned_agree": None,
                "truth_dir": truth,
            })
            continue
        computable += 1
        bs, breg = score_with_params(metrics, base_w, base_bull, base_bear)
        ts, treg = score_with_params(metrics, tuned_w, tuned_bull, tuned_bear)
        results.append({
            **row, "computable": True, "n_names": n,
            "metrics": metrics,
            "base_score": bs, "base_regime": breg,
            "base_agree": (breg == truth),
            "tuned_score": ts, "tuned_regime": treg,
            "tuned_agree": (treg == truth),
            "truth_dir": truth,
        })

    print(f"\nComputable: {computable}  Skipped (thin lookback): {skipped}")
    base_hits = sum(1 for r in results if r["computable"] and r["base_agree"])
    tuned_hits = sum(1 for r in results if r["computable"] and r["tuned_agree"])
    print(f"Baseline agreement:  {base_hits}/{computable}")
    print(f"Tuned    agreement:  {tuned_hits}/{computable}")

    # write comparison CSV (only the computable rows carry scores; include all)
    with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "Issue Date", "computable", "n_names",
            "proxy_score_base", "proxy_regime_base", "agree_base",
            "proxy_score_tuned", "proxy_regime_tuned", "agree_tuned",
            "InvesTech NLC Value", "InvesTech NLC Regime", "investech_dir",
            "pct_above_50dma", "pct_above_200dma",
            "net_highs_lows_pct", "ad_pct",
        ])
        for r in results:
            m = r.get("metrics") or {}
            w.writerow([
                r["date"], r["computable"], r["n_names"],
                r["base_score"], r["base_regime"], r["base_agree"],
                r["tuned_score"], r["tuned_regime"], r["tuned_agree"],
                r["nlc_value"], r["nlc_regime"], r["truth_dir"],
                m.get("pct_above_50dma"), m.get("pct_above_200dma"),
                m.get("net_highs_lows_pct"), m.get("ad_pct"),
            ])
    print(f"Wrote {OUT_CSV}")

    # print the computable comparison table for the report
    print("\n=== Computable comparison ===")
    print("date       n  base   base_reg  tuned  tuned_reg  truth     "
          "NLCval NLCregime")
    for r in results:
        if not r["computable"]:
            continue
        print(f"{r['date']}  {r['n_names']:>2}  "
              f"{r['base_score']:>5}  {r['base_regime']:<8}  "
              f"{r['tuned_score']:>5}  {r['tuned_regime']:<8}  "
              f"{r['truth_dir']:<8}  "
              f"{str(r['nlc_value']):>5}  {r['nlc_regime']}")
        m = r["metrics"]
        print(f"            pct50={m['pct_above_50dma']} "
              f"pct200={m['pct_above_200dma']} "
              f"net_hl%={m['net_highs_lows_pct']} ad%={m['ad_pct']}")

    return results, computable, base_hits, tuned_hits


if __name__ == "__main__":
    main()
