"""kill_switch.py — the desk's BREAK-GLASS switch, independent of the dashboard.

Run this to stop ALL desk automation when you need to, even if the Streamlit
dashboard is down or hung. It performs the SAME OS-level halt as the dashboard's
"Halt automation" control: it stops and turns off the Windows scheduled tasks and
force-stops the running strategy programs, so nothing restarts. It talks to
Windows only — it NEVER contacts a broker and NEVER places, cancels, arms, or
transmits any order.

Usage (run as administrator if a task refuses to turn off):

    python kill_switch.py --halt-all     Stop and turn off ALL desk automation
    python kill_switch.py --halt-s8      Stop and turn off Strategy 8 (live pilot)
    python kill_switch.py --halt-s0      Stop and turn off Strategy 0

Those three flags are the WHOLE interface. There is no get-flat / emergency-close
option: the --flatten-all / --flatten-s8 / --flatten-s0 flags and the inert
emergency.flatten_preview / flatten_execute scaffold behind them were REMOVED
2026-08-25 by owner decision (Andrew) — he does not want a panic button, because he
does not want to be making irrational decisions on bad market days and he is not
going to time the market. There was never any order-transmit code behind them. Do
not re-add them here. HALT, above, is untouched.
"""
from __future__ import annotations

import argparse
import os
import sys

# Make sure this script's own folder is importable so `import emergency` works no
# matter what the current working directory is (derive from __file__, per the
# desk's path rules — never a hard-coded absolute string).
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import emergency  # noqa: E402  (single source of truth for the OS-level halt)


def _print_halt_report(report: dict) -> None:
    print()
    print("=" * 70)
    print(report["summary"])
    print("=" * 70)
    for a in report.get("actions", []):
        mark = "OK  " if a["ok"] else ("ADMIN" if a.get("needs_admin") else "WARN")
        print(f"  [{mark}] {a['message']}")
    print()


def _do_halt(which: str) -> int:
    label = {"s8": "Strategy 8 (live pilot)", "s0": "Strategy 0",
             "all": "ALL desk automation"}.get(which, which)
    print(f"Halting {label} at the operating-system level "
          f"(stopping tasks + strategy programs; no orders are sent)…")
    report = emergency.halt_strategy(which)
    _print_halt_report(report)
    # Non-zero exit if anything could not be fully turned off, so an operator (or
    # a wrapper script) can tell a partial halt from a clean one.
    return 0 if report.get("ok") else 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="kill_switch.py",
        description="Break-glass switch: stop ALL desk automation, independent of "
                    "the dashboard. Sends no orders, ever.",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--halt-all", action="store_true",
                       help="Stop and turn off ALL desk automation.")
    group.add_argument("--halt-s8", action="store_true",
                       help="Stop and turn off Strategy 8 (live pilot).")
    group.add_argument("--halt-s0", action="store_true",
                       help="Stop and turn off Strategy 0.")
    # NO --flatten-* FLAGS. The get-flat / emergency-close option was removed
    # 2026-08-25 by owner decision (see the module docstring). Do not re-add it.
    args = parser.parse_args(argv)

    if args.halt_all:
        return _do_halt("all")
    if args.halt_s8:
        return _do_halt("s8")
    if args.halt_s0:
        return _do_halt("s0")
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
