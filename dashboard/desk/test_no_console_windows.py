"""test_no_console_windows.py — pins the "no flashing black window" fix.

THE DEFECT THIS GUARDS: on Windows, starting a helper script or PowerShell from
the dashboard draws a console window on screen for a moment and then makes it
vanish. Using the desk meant a black window flashing on almost every click.
The fix is that every process the dashboard starts is started with the Windows
no-console-window creation flag, which ``deskproc`` supplies in one place.

These tests are a SOURCE-LEVEL (AST) scan, which is the honest way to test this:
window visibility is decided by an argument at the call site, so what has to be
guaranteed is a property of every call site, not of one run. A future ninth call
site that spawns a process without the flag fails this file rather than quietly
bringing the flashing back.
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

import deskproc

DESK_DIR = Path(__file__).resolve().parent

# Every subprocess entry point that STARTS a new process (and so can draw a
# console window). Anything added to subprocess later that starts a process
# should be added here too.
SPAWNERS = {"run", "Popen", "call", "check_call", "check_output"}

# Ways of starting a process that CANNOT be told to hide their window at all.
# They have no place in the dashboard; deskproc.run is the way.
UNSUPPRESSIBLE_OS_CALLS = {"system", "popen", "startfile", "spawnl", "spawnle",
                           "spawnlp", "spawnlpe", "spawnv", "spawnve", "spawnvp",
                           "spawnvpe"}


def _python_files() -> list[Path]:
    return sorted(p for p in DESK_DIR.glob("*.py") if p.name != "__init__.py")


def _spawn_calls(tree: ast.AST) -> list[tuple[ast.Call, str]]:
    """Every call in the tree that starts a new OS process, as (node, label).

    Catches both ``subprocess.run(...)`` and ``run(...)`` where ``run`` came in
    via ``from subprocess import run``.
    """
    bare_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "subprocess":
            for alias in node.names:
                if alias.name in SPAWNERS:
                    bare_names.add(alias.asname or alias.name)

    found: list[tuple[ast.Call, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)
                and func.value.id == "subprocess" and func.attr in SPAWNERS):
            found.append((node, f"subprocess.{func.attr}"))
        elif isinstance(func, ast.Name) and func.id in bare_names:
            found.append((node, f"{func.id}() [from subprocess import ...]"))
    return found


def test_every_subprocess_call_in_desk_hides_its_console_window():
    """No process is started from dashboard/desk without the no-window flag."""
    offenders: list[str] = []
    for path in _python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node, label in _spawn_calls(tree):
            kwargs = {kw.arg for kw in node.keywords}
            if "creationflags" not in kwargs:
                offenders.append(f"{path.name}:{node.lineno}: {label}")
    assert not offenders, (
        "These calls start a process without suppressing the console window, so a "
        "black window will flash on screen when the dashboard is used. Call "
        "deskproc.run(...) instead of subprocess.run(...) (or, for another launch "
        "shape, pass creationflags=deskproc.NO_WINDOW):\n  " + "\n  ".join(offenders)
    )


def test_no_unsuppressible_process_launchers_in_desk():
    """os.system / os.popen / os.spawn* always draw a window — they are banned."""
    offenders: list[str] = []
    for path in _python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "os"
                    and node.func.attr in UNSUPPRESSIBLE_OS_CALLS):
                offenders.append(f"{path.name}:{node.lineno}: os.{node.func.attr}")
    assert not offenders, (
        "These launchers cannot hide their console window. Use deskproc.run:\n  "
        + "\n  ".join(offenders))


@pytest.mark.parametrize(
    "module_name, minimum",
    [("deskdata.py", 1), ("emergency.py", 1), ("eventlog.py", 1),
     ("page_control_plane.py", 5)],
)
def test_known_call_sites_still_route_through_deskproc(module_name, minimum):
    """The eight known shell-outs still go through the shared helper.

    Minimums, not exact counts: adding a ninth call site is fine, silently
    dropping one back to a raw subprocess call is not.
    """
    src = (DESK_DIR / module_name).read_text(encoding="utf-8")
    assert src.count("deskproc.run(") >= minimum, (
        f"{module_name} should start its processes with deskproc.run(...)")


def test_no_window_constant_is_the_real_windows_flag():
    """The flag is the genuine Windows constant, and 0 (harmless) elsewhere."""
    if sys.platform == "win32":
        assert deskproc.NO_WINDOW == subprocess.CREATE_NO_WINDOW
        assert deskproc.NO_WINDOW != 0
    else:
        # Portability: the constant does not exist off Windows, so it resolves to
        # 0 ("no extra creation flags") and nothing breaks on Linux/macOS/CI.
        assert deskproc.NO_WINDOW == 0


def test_run_forwards_everything_and_only_adds_the_flag(monkeypatch):
    """deskproc.run changes window visibility and NOTHING else."""
    seen: dict = {}

    def fake_run(*args, **kwargs):
        seen["args"] = args
        seen["kwargs"] = kwargs
        return "sentinel"

    monkeypatch.setattr(deskproc.subprocess, "run", fake_run)
    out = deskproc.run(["x", "y"], cwd="C:/somewhere", capture_output=True,
                       text=True, timeout=25)

    assert out == "sentinel"                      # return value passed straight back
    assert seen["args"] == (["x", "y"],)          # command untouched
    assert seen["kwargs"]["cwd"] == "C:/somewhere"
    assert seen["kwargs"]["capture_output"] is True
    assert seen["kwargs"]["text"] is True
    assert seen["kwargs"]["timeout"] == 25
    assert seen["kwargs"]["creationflags"] == deskproc.NO_WINDOW
    # Nothing else was invented.
    assert set(seen["kwargs"]) == {"cwd", "capture_output", "text", "timeout",
                                   "creationflags"}


def test_run_keeps_caller_supplied_creationflags():
    """A caller's own flags survive; the no-window bit is OR-ed on top."""
    captured: dict = {}

    def fake_run(*args, **kwargs):
        captured.update(kwargs)

    original = deskproc.subprocess.run
    deskproc.subprocess.run = fake_run
    try:
        deskproc.run(["x"], creationflags=0x00000008)
    finally:
        deskproc.subprocess.run = original
    assert captured["creationflags"] == (0x00000008 | deskproc.NO_WINDOW)


def test_real_process_still_runs_and_its_stdout_is_still_captured():
    """The flag must not change what the process does or what it prints.

    The Control Plane parses these scripts' stdout with regexes, so stdout has to
    survive the change exactly.
    """
    proc = deskproc.run([sys.executable, "-c", "print('desk-no-window-probe')"],
                        capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0
    assert proc.stdout.strip() == "desk-no-window-probe"
