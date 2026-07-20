"""
DEPRECATED SHIM -- kept only for backward compatibility.

The breadth logic now lives in the top-level `breadth.py` (and the .env loader
in `env_loader.py`), per the Phase-2 project layout. Importing
`fetchers.breadth` re-exports everything from the canonical top-level module so
old import paths keep working. Prefer `import breadth`.
"""

from breadth import *  # noqa: F401,F403
from breadth import (  # noqa: F401  explicit re-exports for tooling
    compute_breadth,
    load_universe,
    fetch_tiingo_prices,
    get_series,
    per_ticker_signals,
    leadership_proxy,
    classify_regime,
    fetch_exchange_breadth,
)
