"""pytest path bootstrap for livebot/'s tests.

WHY THIS EXISTS
---------------
S8's DEFINITION layer (s8_config.py, s8_strategy.py) was folded out of livebot/ into the
shared `strategies` package, so livebot's tests now do `from strategies import s8_config`.

Without this file, `strategies` would resolve through the venv's EDITABLE INSTALL, which
points at a fixed checkout (C:\\TradingDesk) — not necessarily the checkout the test file
lives in. Running the suite from a git worktree would then silently test a DIFFERENT
copy of the strategy definition than the one sitting next to the test. That is exactly the
failure mode the repo already guards against in s8_runner.py / s8_service.py, whose own
comments explain they add the repo's package parents from __file__ rather than trust the
editable install.

So: prepend this checkout's own package parents, derived from __file__ per CLAUDE.md
(never an absolute string), ahead of site-packages. Tests then always exercise the code
they ship with.

This affects pytest collection only — it changes no runtime path. The scheduled tasks get
the same directories from their .cmd launchers' PYTHONPATH.
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]

# Insert LAST-first so the final order is livebot, strategies, connections, paperbot,
# backtester — all ahead of site-packages.
for _pkg_parent in ("backtester", "paperbot", "connections", "strategies", "livebot"):
    _p = str(_REPO_ROOT / _pkg_parent)
    if _p in sys.path:
        sys.path.remove(_p)
    sys.path.insert(0, _p)
