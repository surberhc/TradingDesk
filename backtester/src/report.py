"""
report.py — Build the self-contained HTML results report. SPEC.md §15.

One standalone HTML file in output/ containing:
  * Equity curve: strategy vs SPY vs 60/40 vs T-bills.
  * Underwater/drawdown chart: strategy vs SPY.
  * The full metrics table (risk-first, per SPEC §14).
  * A regime-over-time timeline (which regime was active each month).
  * A monthly allocation stacked-area chart (equity / defense / real-asset).

Charts are Plotly, with plotly.js embedded once so the file opens offline in any
browser. Proxy inputs (credit/volatility, and the yield source) are labeled.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import plotly.io as pio
from plotly.offline import get_plotlyjs

from strategies import config

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_PATH = PROJECT_ROOT / config.OUTPUT_DIR

# Regime ordering (healthy -> defensive) and colors for the timeline.
_REGIME_ORDER = ["RiskOn", "RiskOnNarrowing", "Caution", "Defensive", "CapitalPreservation"]
_REGIME_COLOR = {
    "RiskOn": "#1a9850", "RiskOnNarrowing": "#91cf60", "Caution": "#fee08b",
    "Defensive": "#fc8d59", "CapitalPreservation": "#d73027", "Undefined": "#cccccc",
}
_LINE_COLORS = {"strategy": "#1f77b4", "SPY": "#d62728", "60/40": "#7f7f7f", "T-bills": "#bcbd22"}


def _equity_curve(navs: pd.DataFrame) -> go.Figure:
    fig = go.Figure()
    for col in ("strategy", "SPY", "60/40", "T-bills"):
        if col in navs:
            fig.add_trace(go.Scatter(
                x=navs.index, y=navs[col], name=col, mode="lines",
                line=dict(color=_LINE_COLORS.get(col), width=2 if col == "strategy" else 1.3),
            ))
    fig.update_layout(
        title="Growth of $1 — strategy vs benchmarks",
        yaxis_type="log", yaxis_title="NAV (log scale)", template="plotly_white",
        legend=dict(orientation="h", y=-0.18), margin=dict(t=50, b=40),
    )
    return fig


def _drawdown(navs: pd.DataFrame) -> go.Figure:
    fig = go.Figure()
    for col in ("strategy", "SPY"):
        if col in navs:
            dd = navs[col] / navs[col].cummax() - 1.0
            fig.add_trace(go.Scatter(
                x=dd.index, y=dd, name=col, mode="lines", fill="tozeroy",
                line=dict(color=_LINE_COLORS.get(col)),
            ))
    fig.update_layout(
        title="Drawdown (underwater) — strategy vs SPY",
        yaxis_tickformat=".0%", yaxis_title="Drawdown", template="plotly_white",
        legend=dict(orientation="h", y=-0.18), margin=dict(t=50, b=40),
    )
    return fig


def _regime_timeline(monthly: pd.DataFrame) -> go.Figure:
    """Step timeline of the active regime each month, color-coded by health."""
    reg = monthly["regime"]
    levels = reg.map({r: len(_REGIME_ORDER) - i for i, r in enumerate(_REGIME_ORDER)})
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=reg.index, y=levels, mode="lines", line_shape="hv",
        line=dict(color="#444", width=1), showlegend=False,
    ))
    fig.add_trace(go.Scatter(
        x=reg.index, y=levels, mode="markers",
        marker=dict(size=7, color=[_REGIME_COLOR.get(r, "#ccc") for r in reg]),
        text=reg, hovertemplate="%{x|%Y-%m}: %{text}<extra></extra>", showlegend=False,
    ))
    fig.update_layout(
        title="Regime over time",
        yaxis=dict(
            tickmode="array",
            tickvals=list(range(1, len(_REGIME_ORDER) + 1)),
            ticktext=list(reversed(_REGIME_ORDER)),
        ),
        template="plotly_white", margin=dict(t=50, b=40),
    )
    return fig


def _allocation_area(monthly: pd.DataFrame) -> go.Figure:
    """Monthly equity / defense / real-asset sleeve mix as a stacked area."""
    fig = go.Figure()
    parts = [("equity", "Equity", "#1f77b4"), ("defense", "Defense (cash/Treasuries)", "#2ca02c"),
             ("real_asset", "Real assets (gold/commod./TIPS)", "#ff7f0e")]
    for name, label, color in parts:
        if name in monthly:
            fig.add_trace(go.Scatter(
                x=monthly.index, y=monthly[name], name=label, mode="lines",
                stackgroup="alloc", line=dict(width=0.5, color=color),
            ))
    fig.update_layout(
        title="Allocation over time (three sleeves: equity / real assets / defense)",
        yaxis_tickformat=".0%", yaxis_title="Weight", template="plotly_white",
        legend=dict(orientation="h", y=-0.18), margin=dict(t=50, b=40),
    )
    return fig


def _format_metrics_html(table: pd.DataFrame) -> str:
    """Render the metrics table with sensible per-row formatting."""
    pct_rows = {"CAGR", "Annual volatility", "Max drawdown", "Worst rolling 3m",
                "Worst rolling 12m", "Worst rolling 3y", "Downside deviation"}
    fmt = table.copy().astype(object)
    for row in table.index:
        for col in table.columns:
            v = table.loc[row, col]
            if pd.isna(v):
                fmt.loc[row, col] = "—"
            elif row in pct_rows:
                fmt.loc[row, col] = f"{v:.1%}"
            elif "months" in row:
                fmt.loc[row, col] = f"{v:.0f}"
            else:
                fmt.loc[row, col] = f"{v:.2f}"
    return fmt.to_html(classes="metrics", border=0)


def _holdings_area(weights: pd.DataFrame) -> go.Figure:
    """Per-ETF target weights over time, stacked and ordered by asset class."""
    order = (config.EQUITY_CORE + config.SECTORS + config.DEFENSIVE_ASSETS + config.REAL_ASSETS)
    cols = [c for c in order if c in weights.columns and weights[c].abs().sum() > 0]
    cols += [c for c in weights.columns if c not in cols and weights[c].abs().sum() > 0]
    fig = go.Figure()
    for c in cols:
        fig.add_trace(go.Scatter(
            x=weights.index, y=weights[c], name=c, mode="lines",
            stackgroup="holdings", line=dict(width=0.5),
            hovertemplate="%{x|%Y-%m}: " + c + " %{y:.0%}<extra></extra>",
        ))
    fig.update_layout(
        title="Holdings over time (target weight per ETF)",
        yaxis_tickformat=".0%", yaxis_title="Weight", template="plotly_white",
        legend=dict(orientation="h", y=-0.25, font=dict(size=10)), margin=dict(t=50, b=60),
    )
    return fig


def _signals_table_html(monthly: pd.DataFrame) -> str:
    """A scrollable month-by-month table of regime, score, sizing, and reasons."""
    rows = []
    for date, r in monthly.iterrows():
        rows.append(
            "<tr>"
            f"<td>{date.date()}</td>"
            f"<td>{r['regime']}</td>"
            f"<td class='num'>{r['score']:.0f}</td>"
            f"<td class='num'>{r['equity_target']:.0%}</td>"
            f"<td class='num'>{int(r['ladder_stage'])}</td>"
            f"<td class='num'>{r['equity']:.0%}</td>"
            f"<td class='num'>{r['defense']:.0%}</td>"
            f"<td class='num'>{r['real_asset_ticker'] if r.get('real_asset_ticker') else '—'}</td>"
            f"<td class='reasons'>{r['reasons']}</td>"
            "</tr>"
        )
    head = ("<tr><th>Month-end</th><th>Regime</th><th>Score</th><th>Equity tgt</th>"
            "<th>Ladder</th><th>Equity</th><th>Defense</th><th>Hedge</th><th>Reason codes</th></tr>")
    return (
        "<div class='signals'><table class='signals-tbl'>"
        f"<thead>{head}</thead><tbody>{''.join(rows)}</tbody></table></div>"
    )


def build_report(
    result: dict,
    metrics_table: pd.DataFrame,
    output_path: str | Path | None = None,
) -> Path:
    """
    Render the standalone HTML report into output/ and return its path.

    `result` is the dict returned by backtest.run_backtest(); `metrics_table` is
    the DataFrame from metrics.compute_metrics().
    """
    OUTPUT_PATH.mkdir(parents=True, exist_ok=True)
    version = result.get("version", config.ACTIVE_VERSION)
    if output_path is None:
        output_path = OUTPUT_PATH / f"backtest_report_{version}.html"
    output_path = Path(output_path)

    figs = [
        _equity_curve(result["benchmark_navs"]),
        _drawdown(result["benchmark_navs"]),
        _regime_timeline(result["monthly"]),
        _allocation_area(result["monthly"]),
        _holdings_area(result["weights"]),
    ]
    divs = "\n".join(pio.to_html(f, full_html=False, include_plotlyjs=False) for f in figs)
    signals_table = _signals_table_html(result["monthly"])

    # CSV exports the user can open in Excel: the month-by-month signal log and
    # the full per-ETF target weights over time.
    csv_signals = output_path.parent / f"monthly_signals_{version}.csv"
    csv_weights = output_path.parent / f"holdings_{version}.csv"
    OUTPUT_PATH.mkdir(parents=True, exist_ok=True)
    result["monthly"].to_csv(csv_signals)
    result["weights"].round(4).to_csv(csv_weights)

    navs = result["benchmark_navs"]
    start, end = navs.index.min().date(), navs.index.max().date()
    yield_note = (
        "real US Treasury 10y par yield" if result.get("yield_is_real")
        else f"PROXY ({config.YIELD_PROXY_TICKER} price trend)"
    )
    vol_note = (
        "real VIX (CBOE)" if result.get("vix_is_real")
        else "PROXY (SPY 63-day realized vol)"
    )
    credit_note = (
        "real ICE BofA HY OAS (FRED)" if result.get("credit_is_real")
        else "PROXY (HYG/IEF ratio — HY vs Treasury, captures credit stress + flight-to-quality)"
    )

    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Adaptive All-Weather Core — {version}</title>
<script>{get_plotlyjs()}</script>
<style>
  body {{ font-family: system-ui, Arial, sans-serif; margin: 24px; color: #222; max-width: 1000px; }}
  h1 {{ margin-bottom: 0; }} .sub {{ color: #666; margin-top: 4px; }}
  table.metrics {{ border-collapse: collapse; margin: 8px 0 24px; font-size: 14px; }}
  table.metrics th, table.metrics td {{ padding: 6px 14px; text-align: right; border-bottom: 1px solid #eee; }}
  table.metrics th {{ background: #f5f5f5; }}
  table.metrics td:first-child, table.metrics th:first-child {{ text-align: left; }}
  .note {{ background: #fff8e1; border-left: 4px solid #ffca28; padding: 10px 14px; font-size: 13px; margin: 16px 0; }}
  .chart {{ margin: 10px 0 28px; }}
  .signals {{ max-height: 460px; overflow: auto; border: 1px solid #e0e0e0; border-radius: 4px; margin: 8px 0 24px; }}
  table.signals-tbl {{ border-collapse: collapse; font-size: 12px; width: 100%; }}
  table.signals-tbl th {{ position: sticky; top: 0; background: #f0f0f0; padding: 6px 8px; text-align: left; }}
  table.signals-tbl td {{ padding: 4px 8px; border-bottom: 1px solid #f0f0f0; vertical-align: top; }}
  table.signals-tbl td.num {{ text-align: right; }}
  table.signals-tbl td.reasons {{ color: #555; font-size: 11px; max-width: 480px; }}
  table.signals-tbl tbody tr:hover {{ background: #f7fbff; }}
</style></head><body>
<h1>Adaptive All-Weather Core — {version}</h1>
<p class="sub">Backtest {start} to {end}. In-house research tooling; no live trades.
The mandate is a <b>smoother ride</b> (lower drawdown / downside), not beating SPY.</p>

<div class="note">
  <b>Macro inputs (SPEC §4, §6 / DATA.md):</b> 10-year yield = {yield_note};
  volatility = {vol_note}; credit stress = {credit_note}.
  Items marked PROXY upgrade automatically once the real source is downloaded
  (real credit spread needs a free FRED_API_KEY in .env).
</div>

<h2>Metrics</h2>
{_format_metrics_html(metrics_table)}

<h2>Charts</h2>
<div class="chart">{divs}</div>

<h2>Monthly signal log — every rebalance, what fired and why</h2>
<p class="sub">Each month-end signal date with its regime, 0-100 health score, equity
target, re-entry ladder stage, sleeve mix, the held inflation hedge, and the full
reason codes. Also exported to <code>monthly_signals_{version}.csv</code> and
<code>holdings_{version}.csv</code>.</p>
{signals_table}
</body></html>"""

    output_path.write_text(html, encoding="utf-8")
    return output_path
