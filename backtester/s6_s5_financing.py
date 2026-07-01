"""
S6-as-S5-financing-leg feasibility test (PHASED go/no-go).

PHASE 1 (this pass): two descriptive go/no-go questions, NO tuned thresholds.
  Q1  Are S6's losses hedgeable by a permanent OTM tail? Decompose S6's CUMULATIVE
      LOSS into buckets by the size of the ADVERSE entry(14:00)->settle(16:00) SPX move
      that matters for each structure. A permanent OTM tail only pays on big (>2-3%)
      moves; if the loss is dominated by sub-2% chop, integration cannot work.
  Q2  Can the financing leg generate net-positive cash under any simple PRE-SPECIFIED
      "calm day" filter we already use (bottom VRP tercile, positive dealer gamma,
      morning RV below full-sample median, VIX contango)? A financing leg must be
      net-positive under at least one economically-sensible filter.

GATE: if Q1 shows loss dominated by sub-2% chop AND Q2 shows no simple filter makes S6
      net-positive -> integration REFUTED, stop, do not build Phase 2.

Data reused (NOT recomputed):
  output/s6_research/s6_strike_experiment_trades.csv  arm A_blind015 (fixed 0.15-delta
      daily 0DTE trade outcomes; intraday-managed with stops; per structure).
  output/s6_research/s6_vrp_signals.csv               per-day spot_1400 (14:00 entry
      spot via put-call parity), close_spot (16:00 settle spot), vrp_primary, vix_ts_prior,
      rv_morning. Provides the entry->settle move and the pre-specified calm filters.

ASCII-only output. No tuned knobs in Phase 1. All numbers are per 1-lot ($100 multiplier),
consistent with the source S6 P&L.
"""
import os
import sys
import pandas as pd
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "output", "s6_research")

# --- Adverse-move buckets (fixed, pre-specified; a tail hedge only pays on the big ones)
BUCKET_EDGES = [0.0, 0.005, 0.01, 0.02, 0.03, np.inf]
BUCKET_LABELS = ["<0.5%", "0.5-1%", "1-2%", "2-3%", ">3%"]

STRUCTURES = ["bull_put", "bear_call", "iron_condor"]


def load():
    strike = pd.read_csv(os.path.join(OUT, "s6_strike_experiment_trades.csv"))
    a = strike[strike["arm"] == "A_blind015"].copy()
    v = pd.read_csv(os.path.join(OUT, "s6_vrp_signals.csv"))[
        ["day", "spot_1400", "close_spot", "vrp_primary", "vix_ts_prior", "rv_morning"]
    ]
    m = a.merge(v, on="day", how="left")
    m = m[m["traded"] == True].copy()  # noqa: E712
    # signed entry->settle return (settle minus entry, / entry)
    m["ret"] = (m["close_spot"] - m["spot_1400"]) / m["spot_1400"]
    return m


def adverse_move(row):
    """Magnitude of the adverse-direction move for this structure.
    bull_put: hurt by DOWN moves  -> adverse = max(0, -ret)
    bear_call: hurt by UP moves    -> adverse = max(0, +ret)
    iron_condor: hurt by EITHER    -> adverse = abs(ret)
    """
    r = row["ret"]
    s = row["structure"]
    if s == "bull_put":
        return max(0.0, -r)
    if s == "bear_call":
        return max(0.0, r)
    return abs(r)  # iron_condor


def phase1_q1(m):
    """Loss decomposition by adverse-move bucket, per structure."""
    lines = []
    lines.append("=" * 78)
    lines.append("PHASE 1 - Q1: Are S6's losses hedgeable? (loss by adverse entry->settle move)")
    lines.append("A permanent OTM tail only pays on the big buckets (roughly >2-3%).")
    lines.append("=" * 78)
    tables = {}
    for s in STRUCTURES:
        sub = m[m["structure"] == s].copy()
        sub["adv"] = sub.apply(adverse_move, axis=1)
        sub["bucket"] = pd.cut(sub["adv"], bins=BUCKET_EDGES, labels=BUCKET_LABELS,
                               right=False, include_lowest=True)
        losers = sub[sub["pnl_dollars"] < 0].copy()
        total_loss = -losers["pnl_dollars"].sum()  # positive number
        # max-loss days: pnl at the structure's worst (most negative) value
        worst = sub["pnl_dollars"].min()
        maxloss_days = sub[np.isclose(sub["pnl_dollars"], worst)]

        rows = []
        for b in BUCKET_LABELS:
            lb = losers[losers["bucket"] == b]
            loss_b = -lb["pnl_dollars"].sum()
            n_days = len(sub[sub["bucket"] == b])
            n_loss_days = len(lb)
            n_maxloss = len(maxloss_days[maxloss_days["bucket"] == b])
            frac = (loss_b / total_loss) if total_loss > 0 else 0.0
            rows.append({
                "bucket": b, "loss_$": round(loss_b, 0),
                "frac_of_total_loss": round(frac, 3),
                "n_loss_days": n_loss_days, "n_maxloss_days": n_maxloss,
                "n_trade_days": n_days,
            })
        tbl = pd.DataFrame(rows)
        tables[s] = tbl
        sub2 = m[m["structure"] == s]
        lines.append("")
        lines.append(f"-- {s} --  total loss ${round(total_loss,0):,.0f} "
                     f"(worst-day pnl ${worst:.0f}, {len(maxloss_days)} max-loss days) "
                     f"net pnl ${sub2['pnl_dollars'].sum():,.0f}")
        lines.append(tbl.to_string(index=False))
        # sub-2% share (the killer read)
        sub2pct = tbl[tbl["bucket"].isin(["<0.5%", "0.5-1%", "1-2%"])]["frac_of_total_loss"].sum()
        tail_share = tbl[tbl["bucket"].isin(["2-3%", ">3%"])]["frac_of_total_loss"].sum()
        lines.append(f"   sub-2% chop share of loss = {sub2pct:.1%} | "
                     f"tail-sized (>2%) share = {tail_share:.1%}")
    return "\n".join(lines), tables


def net_pnl(sub):
    return sub["pnl_dollars"].sum()


def phase1_q2(m):
    """Net cumulative premium P&L overall and under pre-specified calm filters."""
    lines = []
    lines.append("")
    lines.append("=" * 78)
    lines.append("PHASE 1 - Q2: Can the financing leg generate net-positive cash?")
    lines.append("Pre-specified calm filters (already used; NOT tuned here):")
    lines.append("  (a) bottom VRP tercile (vrp_primary in lowest third)")
    lines.append("  (b) prior-close positive dealer gamma (day_type_gamma == 'positive')")
    lines.append("  (c) morning RV below full-sample median")
    lines.append("  (d) VIX contango (day_type_vix == 'contango')")
    lines.append("=" * 78)

    # pre-specified thresholds derived from the data's own distribution (not tuned to pnl)
    vrp_ok = m.dropna(subset=["vrp_primary"])
    vrp_lo = vrp_ok["vrp_primary"].quantile(1.0 / 3.0)
    rv_ok = m.dropna(subset=["rv_morning"])
    rv_med = rv_ok["rv_morning"].median()

    def filt_bottom_vrp(d):
        return d[d["vrp_primary"] <= vrp_lo]

    def filt_pos_gamma(d):
        return d[d["day_type_gamma"] == "positive"]

    def filt_rv_below_med(d):
        return d[d["rv_morning"] < rv_med]

    def filt_contango(d):
        return d[d["day_type_vix"] == "contango"]

    filters = [
        ("ALL days (baseline)", lambda d: d),
        ("(a) bottom VRP tercile", filt_bottom_vrp),
        ("(b) positive dealer gamma", filt_pos_gamma),
        ("(c) morning RV < median", filt_rv_below_med),
        ("(d) VIX contango", filt_contango),
    ]

    rows = []
    for s in STRUCTURES + ["ALL_structures"]:
        base = m if s == "ALL_structures" else m[m["structure"] == s]
        for name, fn in filters:
            sub = fn(base)
            rows.append({
                "structure": s, "filter": name,
                "n_days": len(sub),
                "net_pnl_$": round(net_pnl(sub), 0),
                "avg_pnl_$": round(sub["pnl_dollars"].mean(), 2) if len(sub) else float("nan"),
                "breach_rate": round(sub["breached"].mean(), 3) if len(sub) else float("nan"),
            })
    tbl = pd.DataFrame(rows)
    lines.append(f"(thresholds: vrp bottom-tercile cut <= {vrp_lo:.4f}; rv median = {rv_med:.4f})")
    lines.append("")
    lines.append(tbl.to_string(index=False))

    any_positive = (tbl[tbl["structure"] == "ALL_structures"]["net_pnl_$"] > 0).any()
    # also check any single-structure / filter positive (economically sensible cell)
    positives = tbl[(tbl["net_pnl_$"] > 0) & (tbl["filter"] != "ALL days (baseline)")]
    lines.append("")
    lines.append(f"Net-positive cells under a calm filter: {len(positives)}")
    if len(positives):
        lines.append(positives.to_string(index=False))
    return "\n".join(lines), tbl


def gate_verdict(q1_tables, q2_tbl):
    lines = []
    lines.append("")
    lines.append("=" * 78)
    lines.append("PHASE 1 GATE VERDICT")
    lines.append("=" * 78)
    # Q1: is loss dominated by sub-2% chop across structures?
    sub2_shares = {}
    for s, tbl in q1_tables.items():
        sub2 = tbl[tbl["bucket"].isin(["<0.5%", "0.5-1%", "1-2%"])]["frac_of_total_loss"].sum()
        sub2_shares[s] = sub2
    q1_refuted = all(v > 0.5 for v in sub2_shares.values())  # majority of loss from chop

    # Q2: any calm filter net-positive (per-structure or all)?
    positives = q2_tbl[(q2_tbl["net_pnl_$"] > 0) & (q2_tbl["filter"] != "ALL days (baseline)")]
    q2_refuted = len(positives) == 0

    lines.append("Q1 sub-2% chop share of cumulative loss, per structure:")
    for s, v in sub2_shares.items():
        lines.append(f"    {s:12s}: {v:.1%}  ({'chop-dominated' if v>0.5 else 'tail-material'})")
    lines.append(f"Q1 refuted (all structures chop-dominated): {q1_refuted}")
    lines.append(f"Q2 refuted (no calm filter net-positive): {q2_refuted}")
    lines.append("")
    if q1_refuted and q2_refuted:
        verdict = "REFUTED ON BOTH COUNTS -> do NOT build Phase 2."
    elif not q1_refuted and not q2_refuted:
        verdict = "BOTH PROMISING -> proceed to Phase 2."
    else:
        verdict = ("MIXED -> one count promising; per the task, proceed to Phase 2 "
                   "(either question promising is enough).")
    lines.append("VERDICT: " + verdict)
    return "\n".join(lines), q1_refuted, q2_refuted


def main():
    m = load()
    print(f"[load] A_blind015 traded rows: {len(m)} | "
          f"date range {m['day'].min()}..{m['day'].max()} | "
          f"structures {sorted(m['structure'].unique())}")
    # sanity: proof on a few diverse days
    print("\n[proof] sample days (entry->settle move + structure pnl):")
    proof_days = ["2022-01-05", "2022-02-24", "2022-09-13", "2023-03-13",
                  "2024-08-05", "2025-04-07"]
    pv = m[m["day"].isin(proof_days)][
        ["day", "structure", "spot_1400", "close_spot", "ret", "pnl_dollars", "breached"]
    ].copy()
    pv["ret_pct"] = (pv["ret"] * 100).round(2)
    print(pv[["day", "structure", "spot_1400", "close_spot", "ret_pct",
              "pnl_dollars", "breached"]].to_string(index=False))

    q1_text, q1_tables = phase1_q1(m)
    q2_text, q2_tbl = phase1_q2(m)
    gate_text, q1_ref, q2_ref = gate_verdict(q1_tables, q2_tbl)

    report = "\n".join([q1_text, q2_text, gate_text])
    print("\n" + report)

    with open(os.path.join(OUT, "s6_s5_phase1_verdict.txt"), "w") as f:
        f.write(report + "\n")
    q2_tbl.to_csv(os.path.join(OUT, "s6_s5_phase1_q2_filters.csv"), index=False)
    for s, tbl in q1_tables.items():
        tbl.to_csv(os.path.join(OUT, f"s6_s5_phase1_q1_{s}.csv"), index=False)
    print("\n[done] wrote s6_s5_phase1_verdict.txt + q1/q2 CSVs")
    return q1_ref, q2_ref


if __name__ == "__main__":
    main()
