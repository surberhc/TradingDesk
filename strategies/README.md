# strategies

The shared "brain." One named file per strategy, plus the common machinery they use.
Both `backtester\` and `paperbot\` import from here — so a strategy you test is the
exact same file you trade.

Contents:
- `all_weather.py` + `config.py` — S0, Adaptive All-Weather (the first strategy to paper)
- `small_tier.py` — the whole-share proxy tier for small accounts
- `spx_vol_control.py` — S4, single-asset SPX volatility-control fund
- `s8_strategy.py` + `s8_config.py` — S8, British IC + B2 (SPX 0DTE credit spreads).
  Folded in from `livebot\` so every algorithm-derived strategy definition lives here.
  S8's RUNTIME (session lifecycle, collector, monitor, locks, watchdogs, gateway
  reaping) deliberately stays in `livebot\` — as does `s8_risk.py`, which gates on live
  account margin, and `s8_vol.py`, which reads bars off IBKR.
- `parts\` — shared machinery: regime, duration, volatility, sector, etc.

## The line this package holds
Decision logic only: given market state and current positions, produce targets. No data
access, no simulation, no broker I/O — those belong to the runners (`backtester\`,
`paperbot\`, `livebot\`). If a module needs `ib_async`, a socket, a database, a clock, or
a file handle, it does not belong here.

One known exception, stated plainly rather than hidden: `s8_strategy.py` adds
`backtester\` to `sys.path` to reuse `s6_recon.py`'s already-validated put-call-parity
spot recovery and Black-Scholes delta. That is for a DIAGNOSTIC delta only (S8 selects on
credit, not delta), `s6_recon.py` is pure numpy/pandas with no IBKR imports, and the live
runner takes delta from IBKR's own greeks feed instead. It is still a reach out of this
package into a runner, so it is worth removing if that diagnostic ever moves or the
recon helpers get promoted into `parts\`.
