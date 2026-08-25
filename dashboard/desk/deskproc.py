"""deskproc.py — the ONE place the desk dashboard starts an outside process.

Why this module exists (plain English): the dashboard shells out to helper
scripts and to PowerShell to read scheduled tasks, build a rebalance preview,
probe the gateway, and so on. On Windows, starting one of those from the
dashboard pops a black console window on screen for a moment and then makes it
disappear. Nothing is wrong when that happens — but to the person using the
desk it looks like something flashed by that they were meant to read, and it
happens on almost every click. Windows can start the same process with no
console window at all, and that is all this module does.

WHAT THIS CHANGES: whether a console window is drawn on screen. NOTHING ELSE.
The command, its arguments, its working directory, its timeout, how its output
is captured, and its return code all pass straight through untouched. The
Control Plane reads these scripts' printed output with regular expressions, so
that output must stay byte-for-byte what it was.

HOW TO USE IT — a future ninth call site should do this and nothing more::

    import deskproc
    proc = deskproc.run([...], capture_output=True, text=True, timeout=30)

``deskproc.run`` is a drop-in for ``subprocess.run``. If some future caller
needs a different shape of process launch (``subprocess.Popen``, say), pass the
``NO_WINDOW`` constant below as ``creationflags`` rather than re-deriving it.

PORTABILITY: ``subprocess.CREATE_NO_WINDOW`` exists only on Windows. It is
resolved defensively here, so on Linux/macOS ``NO_WINDOW`` is simply ``0`` —
"no extra creation flags" — and this module imports and runs normally there.
"""
from __future__ import annotations

import subprocess
from typing import Any

# The Windows flag that starts a process with NO console window. Resolved with
# getattr so that importing this module on a non-Windows machine (CI, a test
# runner on Linux) works: 0 means "no special creation flags".
NO_WINDOW: int = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def run(*args: Any, creationflags: int = 0, **kwargs: Any) -> "subprocess.CompletedProcess":
    """``subprocess.run`` with the Windows console window suppressed.

    Every argument is forwarded to ``subprocess.run`` exactly as given, and the
    ``CompletedProcess`` it returns is handed back untouched. The only thing
    added is the no-window creation flag (OR-ed onto any flags the caller
    supplied, so a caller that needs its own flags keeps them).
    """
    return subprocess.run(*args, creationflags=creationflags | NO_WINDOW, **kwargs)
