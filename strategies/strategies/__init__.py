"""
strategies — the shared strategy brain.

One named file per strategy (e.g. all_weather.py), plus parts/ for the common
machinery they use (regime, duration, defensive, volatility, sector, real_assets,
portfolio, reentry). The decision logic here is PURE: given market state (prices +
macro up to a point in time) and current positions, produce target portfolio weights.

Two runners drive the SAME code:
  * backtester — historical bars, simulated fills,
  * paperbot   — live IBKR paper data, real paper orders.

So a strategy you backtest is the exact same file you trade. Keep this package free
of data access, simulation, and broker I/O.
"""
