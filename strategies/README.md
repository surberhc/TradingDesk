# strategies

The shared "brain." One named file per strategy, plus the common machinery they use.
Both `backtester\` and `paperbot\` import from here — so a strategy you test is the
exact same file you trade.

Planned contents:
- `all_weather.py`  — Adaptive All-Weather (the first strategy to paper)
- `iron_condor.py`, `swiss_condor.py`, `gamma_overlay.py` — later
- `parts\` — shared machinery: regime, duration, volatility, sector, etc.

Will be rebuilt from the CANONICAL `backtester\` and proven byte-identical before use.
