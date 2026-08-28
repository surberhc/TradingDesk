"""test_no_flatten.py — there is NO "get flat" / emergency-close button on this desk.

OWNER DECISION (Andrew, 2026-08-25). The FLATTEN panic button was removed from the desk
entirely. His reasoning: he does not want a panic button, because he does not want to be
making irrational decisions on bad market days and he is not going to time the market.

It was never wired to a broker in the first place — flatten_execute() was a hard stub that
raised, and there was no order-transmit code of any kind behind it — so this removed a
button, not a capability.

This file is the standing guard against it coming back. It asserts the ABSENCE of the
mechanism in BOTH places it lived: the emergency module's functions, and the standalone
break-glass script's three --flatten-* CLI flags. Every test here FAILS against the
unmodified files.

WHAT IS EXPLICITLY NOT AFFECTED, and is smoke-tested below so a future edit cannot quietly
take it with it:
  * HALT — stopping/disabling the Windows scheduled tasks and force-stopping the strategy
    programs. That is the owner stopping the software, and it STAYS.
  * The MANUAL, file-based operator stop (the AUTOTRADE_DISABLED sentinel / KILL_SWITCH
    label on the paperbot's live-deploy rails). Different control; untouched.

HISTORICAL NOTE: an earlier draft of this docstring also listed paperbot/flatten_accounts.py
here as a DIFFERENT tool that stayed (a scoped, paper-only, dry-run-by-default cleanup
utility needing explicit --accounts/--symbols allowlists). That is no longer true — Andrew
had it DELETED later the same day (2026-08-25), along with its test, as paper-only and not
needed for live trading. It no longer exists, this file never touched it, and nothing here
depends on it.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make bare imports resolve regardless of pytest's rootdir / invocation cwd.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import emergency  # noqa: E402
import kill_switch  # noqa: E402


# --- 1. The emergency module has no flatten functions at all. -------------------------
@pytest.mark.parametrize("gone", ["flatten_preview", "flatten_execute"])
def test_the_flatten_scaffold_is_gone_from_the_emergency_module(gone):
    assert not hasattr(emergency, gone), (
        f"emergency.{gone} is back. The get-flat / emergency-close button was removed "
        f"2026-08-25 by owner decision and must not be re-added.")


def test_no_flatten_event_branch_survives_in_the_plain_english_messages():
    """The two flatten-only branches of _plain_message are gone, so a flatten event name
    now falls through to the GENERIC sentence instead of producing get-flat prose.

    Against the unmodified file these produced sentences about viewing the get-flat
    preview / a get-flat being requested; now they can only produce the fallback.
    """
    for event in ("emergency_flatten_preview", "emergency_flatten_blocked"):
        msg = emergency._plain_message(event, {"which": "all"})
        assert msg == f"Emergency control event '{event}' for ALL desk automation.", msg
        assert "get-flat" not in msg
        assert "closed" not in msg


# --- 2. The break-glass script's three --flatten-* flags are gone. --------------------
@pytest.mark.parametrize("flag", ["--flatten-all", "--flatten-s8", "--flatten-s0"])
def test_the_flatten_cli_flags_are_gone_from_the_kill_switch(flag, capsys):
    """argparse now rejects each of these: it is not a recognised option, so parsing fails
    and argparse raises SystemExit(2) after printing usage to stderr.

    Against the unmodified file each flag was VALID — it ran _do_flatten and RETURNED 3 —
    so this test could not even reach the SystemExit and fails hard against the old code.
    """
    with pytest.raises(SystemExit) as exc:
        kill_switch.main([flag])
    assert exc.value.code == 2, f"{flag} still appears to be a recognised option"
    err = capsys.readouterr().err
    assert "error:" in err
    # The usage line argparse prints must list ONLY the three halt options.
    assert flag not in err.splitlines()[0], f"{flag} is still in the CLI's usage line"
    assert "--halt-all" in err


def test_the_flatten_dispatch_helper_is_gone():
    assert not hasattr(kill_switch, "_do_flatten"), (
        "kill_switch._do_flatten is back. It was removed 2026-08-25 with the rest of the "
        "get-flat path.")


# --- 3. HALT still works, unchanged. --------------------------------------------------
def test_halt_all_still_dispatches_to_the_unchanged_halt_path(monkeypatch, capsys):
    """HALT smoke test: --halt-all must still resolve to emergency.halt_strategy('all')
    and report its outcome. Nothing here contacts Windows — halt_strategy is stubbed."""
    seen = {}

    def fake_halt(which):
        seen["which"] = which
        return {"which": which, "ok": True, "needs_admin": False,
                "actions": [{"target": "MorningExecuteDaily", "kind": "scheduled task",
                             "ok": True, "needs_admin": False,
                             "message": "Stopped it and turned it off."}],
                "summary": "Halted ALL desk automation."}

    monkeypatch.setattr(kill_switch.emergency, "halt_strategy", fake_halt)

    assert kill_switch.main(["--halt-all"]) == 0
    assert seen["which"] == "all"
    assert "Halted ALL desk automation." in capsys.readouterr().out


@pytest.mark.parametrize("flag, which", [("--halt-s8", "s8"), ("--halt-s0", "s0")])
def test_the_other_two_halt_flags_still_dispatch(monkeypatch, flag, which):
    seen = {}
    monkeypatch.setattr(
        kill_switch.emergency, "halt_strategy",
        lambda w: (seen.update(which=w) or {"which": w, "ok": True, "actions": [],
                                            "summary": "ok"}))
    assert kill_switch.main([flag]) == 0
    assert seen["which"] == which


def test_the_halt_machinery_itself_is_all_still_present():
    """The OS-level halt path must be intact: the public entry points, the read-only
    status read, and the four pure PowerShell command builders."""
    for name in ("halt_strategy", "halt_status", "build_task_halt_command",
                 "build_process_kill_command", "build_task_status_command",
                 "build_process_status_command"):
        assert hasattr(emergency, name), f"emergency.{name} went missing — HALT must stay."


def test_the_halt_command_builder_still_stops_then_disables_each_task():
    """A pure, no-side-effect check that the halt command is unchanged in shape: for each
    task it stops it and then disables it, so it cannot restart on schedule."""
    cmd = emergency.build_task_halt_command(["MorningExecuteDaily"])
    assert "Stop-ScheduledTask" in cmd
    assert "Disable-ScheduledTask" in cmd
    assert "'MorningExecuteDaily'" in cmd
