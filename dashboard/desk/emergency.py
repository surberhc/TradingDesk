"""emergency.py — the desk's break-glass emergency controls (Halt + inert Flatten).

THE MOST SAFETY-CRITICAL FILE ON THE DESK. Read this header before touching it.

Two responsibilities, deliberately kept far apart:

  1. HALT (real, and it works today) — process/task control ONLY. It stops and
     turns off the Windows scheduled tasks that drive the desk's automation, and
     force-kills the matching strategy python processes at the OS level so a HUNG
     bot is stopped just as reliably as a healthy one. It talks to Windows, never
     to a broker. It transmits NOTHING. It cannot place, modify, arm, or cancel an
     order because there is no order code in this file at all.

  2. FLATTEN (INERT SCAFFOLD — does nothing today) — the future "get flat / close
     everything" button. Right now there is NOTHING REAL TO CLOSE: Strategy 8 is a
     zero-transmit pilot that holds no real positions, and Strategy 0 is on the
     paper account / real-money gated. So flatten_preview() tells the truth in
     plain English and flatten_execute() is a hard stub that RAISES. There is
     ZERO order-transmit code anywhere in this module — no ib.placeOrder, no order
     router, no arm, no broker connection of any kind. That is by design and must
     stay that way until a deliberate, gated, HUMAN-armed real-money milestone.

Everything is logged to the durable event store when it is available. eventlog.py
may be authored in parallel, so record_event is imported defensively and every
call is guarded — a missing or differently-shaped logger never breaks a halt.

Plain-English rule: the owner is a non-coder. Every action and every message
spells out, in full sentences, exactly what it does.
"""
from __future__ import annotations

import json
import subprocess
from datetime import datetime

# --- Durable event log — imported defensively (may be authored in parallel). --
try:
    from eventlog import record_event  # type: ignore
except Exception:  # pragma: no cover - eventlog may not exist yet
    record_event = None  # type: ignore


def _plain_message(event: str, fields: dict) -> str:
    """A full, non-technical sentence describing an emergency-control event, for the
    durable event log (PLAIN-ENGLISH RULE #1). If the caller already supplied a
    finished sentence in ``message``, that wins."""
    if fields.get("message"):
        return str(fields["message"])
    which = fields.get("which")
    label = _which_label(which) if which else "the desk"
    if event == "emergency_halt_requested":
        n = len(fields.get("tasks") or [])
        procs = (" and force-stop its running programs"
                 if fields.get("kills_processes") else "")
        return (f"Emergency HALT requested for {label}: stopping and turning off "
                f"{n} scheduled task(s){procs}. No broker was contacted and no order "
                f"was sent.")
    if event == "emergency_halt_action":
        outcome = "succeeded" if fields.get("ok") else "did not fully complete"
        target = fields.get("target", "a task")
        return (f"Emergency halt step for {label} on '{target}' {outcome}. "
                f"No order was sent.")
    if event == "emergency_halt_completed":
        outcome = "fully completed" if fields.get("ok") else "only partly completed"
        return (f"Emergency HALT of {label} {outcome} across "
                f"{fields.get('actions', 0)} action(s). Nothing was transmitted to "
                f"any broker.")
    if event == "emergency_flatten_preview":
        return (f"Viewed the emergency get-flat preview for {label}. Nothing was "
                f"closed and no order was sent — there are no live positions to "
                f"flatten today.")
    if event == "emergency_flatten_blocked":
        return (f"An emergency get-flat was requested for {label} but it is not armed "
                f"— nothing was closed and no order was sent (there are no live "
                f"positions to flatten today).")
    return f"Emergency control event '{event}' for {label}."


def _severity_for(event: str, fields: dict) -> str:
    """Map an emergency event to the event log's colour tier (good/warn/bad/info)."""
    if event in ("emergency_halt_action", "emergency_halt_completed"):
        return "good" if fields.get("ok") else "bad"
    if event == "emergency_halt_requested":
        return "warn"
    return "info"


def _emit(event: str, **fields) -> None:
    """Record an emergency-control event to the durable event store.

    Calls ``eventlog.record_event`` with its REAL signature
    ``record_event(ts, source, category, message, severity, day)`` — the category
    is the machine event name, the message is a full plain-English sentence.

    Fully guarded: a missing logger or any error inside it can NEVER interfere with
    an emergency halt. (Historical note: this used to guess four wrong call shapes,
    all of which raised TypeError, so emergency actions were silently NOT logged —
    conductor #67. Fixed 2026-07-29.)"""
    if record_event is None:
        return
    ts = fields.pop("ts", None) or datetime.now().isoformat(timespec="seconds")
    fields.pop("module", None)
    try:
        record_event(
            ts=ts,
            source="Emergency controls",
            category=event,
            message=_plain_message(event, fields),
            severity=_severity_for(event, fields),
        )
    except Exception:
        return


# --------------------------------------------------------------------------- #
# The automation targets.                                                      #
# --------------------------------------------------------------------------- #
# Strategy 8 live pilot (own live gateway, port 4003). Scheduled tasks:
S8_TASKS: tuple[str, ...] = (
    "LiveTradeGatewayOpen_0800CT",
    "S8UnifiedService_Session",
    "S8Collector_Session",
    "S8SessionTeardown",
    "S8MorningStillDownAlarm_0845CT",
)
# Strategy 8 long-running python processes — identified ONLY by their command
# line containing one of these script markers (the load-bearing safety check,
# mirroring livebot/s8_reap.py: an unidentifiable process is NEVER killed).
S8_PROC_MARKERS: tuple[str, ...] = ("s8_service.py", "s8_collector.py")

# Strategy 0 automation. AccountMonitorDaily is historically part of S0 and is
# currently disabled; including it is harmless (disabling an already-off task is
# a no-op) and makes "halt Strategy 0" a complete belt-and-suspenders stop.
S0_TASKS: tuple[str, ...] = (
    "MorningExecuteDaily",
    "AccountMonitorDaily",
)

# Plain-English names so the owner sees what each task actually is.
FRIENDLY: dict[str, str] = {
    "LiveTradeGatewayOpen_0800CT": "Strategy 8 morning gateway opener",
    "S8UnifiedService_Session": "Strategy 8 all-day trading service",
    "S8Collector_Session": "Strategy 8 market-data collector",
    "S8SessionTeardown": "Strategy 8 end-of-day teardown",
    "S8MorningStillDownAlarm_0845CT": "Strategy 8 morning down alarm",
    "MorningExecuteDaily": "Strategy 0 morning execution check",
    "AccountMonitorDaily": "Strategy 0 account drift monitor (already disabled)",
}


def _which_label(which: str) -> str:
    return {
        "s8": "Strategy 8 (live pilot)",
        "s0": "Strategy 0",
        "all": "ALL desk automation",
    }.get(which, which)


def _tasks_for(which: str) -> list[str]:
    which = (which or "").lower()
    if which == "s8":
        return list(S8_TASKS)
    if which == "s0":
        return list(S0_TASKS)
    if which == "all":
        return list(S8_TASKS) + list(S0_TASKS)
    raise ValueError(f"halt target must be 's8', 's0', or 'all' (got {which!r})")


def _kills_processes(which: str) -> bool:
    """Only S8 runs all-day python processes worth force-killing."""
    return (which or "").lower() in ("s8", "all")


# --------------------------------------------------------------------------- #
# PowerShell command builders (PURE — build the text, never run it).           #
# Kept separate from execution so the exact commands can be dry-inspected /    #
# unit-tested without ever stopping or disabling a live task.                  #
# --------------------------------------------------------------------------- #
def _ps_literal(s: str) -> str:
    """A single-quoted PowerShell string literal, safely escaped."""
    return "'" + str(s).replace("'", "''") + "'"


def build_task_halt_command(names: list[str]) -> str:
    """PowerShell that, for each task name, Stop-ScheduledTask then
    Disable-ScheduledTask, capturing each step's error (if any) and the resulting
    state, and emits one JSON object per task. Transmits nothing — Windows only."""
    arr = ",".join(_ps_literal(n) for n in names)
    return (
        "$ErrorActionPreference='Stop';"
        f"$names=@({arr});"
        "$out=foreach($n in $names){"
        "  $stopErr=$null; $disErr=$null; $found=$true;"
        "  try{ Stop-ScheduledTask -TaskName $n -ErrorAction Stop | Out-Null }"
        "  catch{ $stopErr=$_.Exception.Message;"
        "    if($stopErr -match 'No MSFT_ScheduledTask|cannot find|does not exist'){$found=$false} }"
        "  try{ Disable-ScheduledTask -TaskName $n -ErrorAction Stop | Out-Null }"
        "  catch{ $disErr=$_.Exception.Message;"
        "    if($disErr -match 'No MSFT_ScheduledTask|cannot find|does not exist'){$found=$false} }"
        "  $st=$null;"
        "  try{ $st=(Get-ScheduledTask -TaskName $n -ErrorAction Stop).State.ToString() }catch{}"
        "  [PSCustomObject]@{TaskName=$n; Found=$found; StopError=$stopErr;"
        "    DisableError=$disErr; State=$st} }"
        "@($out) | ConvertTo-Json -Compress"
    )


def build_process_kill_command(markers: list[str]) -> str:
    """PowerShell that finds python.exe processes whose COMMAND LINE contains one
    of the markers and force-stops each. The command-line match is the ONLY thing
    that authorises a kill (mirrors s8_reap): a process without a matching command
    line is never touched. Emits one JSON object per matched process."""
    conds = " -or ".join(f"$_.CommandLine -match {_ps_literal(m)}" for m in markers)
    return (
        "$ErrorActionPreference='SilentlyContinue';"
        "$procs=@(Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" |"
        f" Where-Object {{ $_.CommandLine -and ({conds}) }});"
        "$out=foreach($p in $procs){"
        "  $err=$null;"
        "  try{ Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop }"
        "  catch{ $err=$_.Exception.Message }"
        "  [PSCustomObject]@{ProcessId=$p.ProcessId; CommandLine=$p.CommandLine; Error=$err} }"
        "@($out) | ConvertTo-Json -Compress"
    )


def build_task_status_command(names: list[str]) -> str:
    """READ-ONLY PowerShell: report each task's current State. Mutates nothing."""
    arr = ",".join(_ps_literal(n) for n in names)
    return (
        "$ErrorActionPreference='SilentlyContinue';"
        f"$names=@({arr});"
        "$out=foreach($n in $names){"
        "  $t=Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue;"
        "  if($t){ [PSCustomObject]@{TaskName=$n; Found=$true; State=$t.State.ToString()} }"
        "  else{ [PSCustomObject]@{TaskName=$n; Found=$false; State=$null} } }"
        "@($out) | ConvertTo-Json -Compress"
    )


def build_process_status_command(markers: list[str]) -> str:
    """READ-ONLY PowerShell: list matching python.exe processes. Mutates nothing."""
    conds = " -or ".join(f"$_.CommandLine -match {_ps_literal(m)}" for m in markers)
    return (
        "$ErrorActionPreference='SilentlyContinue';"
        "$procs=@(Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" |"
        f" Where-Object {{ $_.CommandLine -and ({conds}) }});"
        "@($procs | Select-Object ProcessId,CommandLine) | ConvertTo-Json -Compress"
    )


# --------------------------------------------------------------------------- #
# Execution helper.                                                            #
# --------------------------------------------------------------------------- #
def _run_powershell(script: str, timeout: int = 30) -> dict:
    """Run a PowerShell command, returning {rc, stdout, stderr}. Never raises."""
    try:
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=timeout,
        )
        return {"rc": proc.returncode, "stdout": proc.stdout or "",
                "stderr": proc.stderr or ""}
    except Exception as exc:  # noqa: BLE001
        return {"rc": -1, "stdout": "", "stderr": f"{type(exc).__name__}: {exc}"}


def _parse_json(text: str):
    text = (text or "").strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return []
    if isinstance(data, dict):
        return [data]
    if isinstance(data, list):
        return data
    return []


def _looks_like_permission_error(msg: str) -> bool:
    m = (msg or "").lower()
    return ("access is denied" in m or "denied" in m or "administrator" in m
            or "elevat" in m or "requires elevation" in m or "0x80070005" in m)


# --------------------------------------------------------------------------- #
# HALT — the real, OS-level stop. Transmits NOTHING.                           #
# --------------------------------------------------------------------------- #
def halt_strategy(which: str) -> dict:
    """Perform an OS-level halt of the given automation and return a structured,
    PLAIN-ENGLISH report of every action and its outcome.

    which: 's8' (Strategy 8 live pilot), 's0' (Strategy 0), or 'all'.

    What it does, and ONLY what it does:
      * Stop-ScheduledTask then Disable-ScheduledTask on that strategy's Windows
        scheduled tasks, so they stop now AND will not relaunch on schedule.
      * Best-effort force-kill of the matching strategy python processes (S8's
        all-day service/collector), so even a HUNG bot is stopped.

    It NEVER contacts a broker and NEVER transmits, cancels, or arms an order.
    It does not raise on partial failure — every problem (including "needs
    administrator") is collected and reported in plain English.
    """
    which = (which or "").lower()
    label = _which_label(which)
    actions: list[dict] = []
    try:
        task_names = _tasks_for(which)
    except ValueError as exc:
        return {
            "which": which, "ok": False, "actions": [],
            "summary": (f"Nothing was halted: {exc}. Please choose Strategy 8, "
                        f"Strategy 0, or ALL."),
        }

    _emit("emergency_halt_requested", which=which, tasks=task_names,
          kills_processes=_kills_processes(which))

    # 1) Scheduled tasks: stop + disable.
    task_cmd = build_task_halt_command(task_names)
    res = _run_powershell(task_cmd, timeout=45)
    rows = {r.get("TaskName"): r for r in _parse_json(res["stdout"])}
    for name in task_names:
        friendly = FRIENDLY.get(name, name)
        r = rows.get(name)
        if r is None:
            # No row came back — treat as an environment problem, not success.
            ok = False
            needs_admin = _looks_like_permission_error(res["stderr"])
            if needs_admin:
                msg = (f"Could not turn off the {friendly} ('{name}') — the "
                       f"dashboard may need to be run as administrator. It could "
                       f"restart on its next schedule.")
            else:
                msg = (f"Tried to stop and turn off the {friendly} ('{name}') but "
                       f"got no confirmation back from Windows. Please verify it "
                       f"in Task Scheduler.")
        elif not r.get("Found", True):
            ok = True  # Not present on this machine = nothing to stop = fine.
            needs_admin = False
            msg = (f"The {friendly} ('{name}') was not found on this machine — "
                   f"nothing to stop.")
        else:
            stop_err = r.get("StopError")
            dis_err = r.get("DisableError")
            state = (r.get("State") or "").lower()
            needs_admin = _looks_like_permission_error(stop_err) or \
                _looks_like_permission_error(dis_err)
            disabled_ok = (dis_err is None) or (state == "disabled")
            if disabled_ok and not stop_err:
                ok = True
                msg = (f"Stopped the {friendly} ('{name}') and turned it off so it "
                       f"will not restart on schedule.")
            elif disabled_ok and stop_err:
                # Disabled (won't relaunch) but the stop step complained — usually
                # because nothing was running to stop. Safe outcome.
                ok = True
                msg = (f"Turned off the {friendly} ('{name}') so it will not "
                       f"restart. (It did not appear to be running.)")
            elif needs_admin:
                ok = False
                msg = (f"Could not turn off the {friendly} ('{name}') — the "
                       f"dashboard may need to be run as administrator. It could "
                       f"restart on its next schedule.")
            else:
                ok = False
                detail = dis_err or stop_err or "unknown error"
                msg = (f"Could not fully turn off the {friendly} ('{name}'): "
                       f"{detail}. Please check it in Task Scheduler.")
        actions.append({"target": name, "kind": "scheduled task", "ok": ok,
                        "needs_admin": needs_admin, "message": msg})
        _emit("emergency_halt_action", which=which, target=name, kind="task",
              ok=ok, needs_admin=needs_admin, message=msg)

    # 2) Strategy python processes: force-kill matched PIDs (S8 only).
    if _kills_processes(which):
        proc_cmd = build_process_kill_command(list(S8_PROC_MARKERS))
        pres = _run_powershell(proc_cmd, timeout=45)
        procs = _parse_json(pres["stdout"])
        if not procs:
            if _looks_like_permission_error(pres["stderr"]):
                actions.append({
                    "target": "Strategy 8 python processes", "kind": "process",
                    "ok": False, "needs_admin": True,
                    "message": ("Could not check or stop the Strategy 8 python "
                                "processes — the dashboard may need to be run as "
                                "administrator."),
                })
                _emit("emergency_halt_action", which=which, kind="process",
                      ok=False, needs_admin=True)
            else:
                actions.append({
                    "target": "Strategy 8 python processes", "kind": "process",
                    "ok": True, "needs_admin": False,
                    "message": ("No running Strategy 8 python processes were found "
                                "to stop (the all-day service and collector were "
                                "not running)."),
                })
                _emit("emergency_halt_action", which=which, kind="process",
                      ok=True, killed=0)
        else:
            killed, failed = 0, 0
            for p in procs:
                pid = p.get("ProcessId")
                err = p.get("Error")
                if err:
                    failed += 1
                    needs_admin = _looks_like_permission_error(err)
                    m = (f"Could not stop Strategy 8 python process (PID {pid})"
                         + (" — may need administrator." if needs_admin
                            else f": {err}."))
                    actions.append({"target": f"python PID {pid}",
                                    "kind": "process", "ok": False,
                                    "needs_admin": needs_admin, "message": m})
                    _emit("emergency_halt_action", which=which, kind="process",
                          pid=pid, ok=False, needs_admin=needs_admin)
                else:
                    killed += 1
                    m = (f"Force-stopped a running Strategy 8 python process "
                         f"(PID {pid}).")
                    actions.append({"target": f"python PID {pid}",
                                    "kind": "process", "ok": True,
                                    "needs_admin": False, "message": m})
                    _emit("emergency_halt_action", which=which, kind="process",
                          pid=pid, ok=True)

    ok_all = all(a["ok"] for a in actions) if actions else True
    needs_admin_any = any(a.get("needs_admin") for a in actions)
    if ok_all:
        summary = (f"Halted {label}. Every task was stopped and turned off so it "
                   f"will not restart, and any running strategy processes were "
                   f"stopped. Nothing was transmitted to any broker.")
    elif needs_admin_any:
        summary = (f"Partly halted {label}. Some tasks or processes could not be "
                   f"turned off because the dashboard may need to be run as "
                   f"administrator — see the details below. Re-run as administrator "
                   f"to finish. Nothing was transmitted to any broker.")
    else:
        summary = (f"Partly halted {label}. Some actions did not fully complete — "
                   f"see the details below and verify in Task Scheduler. Nothing "
                   f"was transmitted to any broker.")
    _emit("emergency_halt_completed", which=which, ok=ok_all,
          needs_admin=needs_admin_any, actions=len(actions))
    return {"which": which, "ok": ok_all, "needs_admin": needs_admin_any,
            "actions": actions, "summary": summary}


def halt_status(which: str = "all") -> dict:
    """Read (READ-ONLY) the current state of the automation so the UI can show
    what is currently halted vs running, in plain English. Mutates nothing."""
    which = (which or "all").lower()
    try:
        task_names = _tasks_for(which)
    except ValueError:
        task_names = _tasks_for("all")

    # Tasks.
    tres = _run_powershell(build_task_status_command(task_names), timeout=25)
    trows = {r.get("TaskName"): r for r in _parse_json(tres["stdout"])}
    tasks: list[dict] = []
    running_tasks = 0
    disabled_tasks = 0
    for name in task_names:
        friendly = FRIENDLY.get(name, name)
        r = trows.get(name)
        if r is None or not r.get("Found", False):
            tasks.append({"name": name, "friendly": friendly,
                          "state": "not found", "tier": "unknown",
                          "phrase": "Not found on this machine"})
            continue
        state = (r.get("State") or "").lower()
        if state == "running":
            running_tasks += 1
            tasks.append({"name": name, "friendly": friendly, "state": "running",
                          "tier": "good", "phrase": "Running now"})
        elif state == "disabled":
            disabled_tasks += 1
            tasks.append({"name": name, "friendly": friendly, "state": "disabled",
                          "tier": "unknown",
                          "phrase": "Turned off (disabled) — will not start"})
        else:  # Ready / Queued
            tasks.append({"name": name, "friendly": friendly, "state": state,
                          "tier": "info",
                          "phrase": "On and scheduled (idle right now)"})

    # Processes (only meaningful for S8 / all).
    procs: list[dict] = []
    proc_query_ok = True
    if _kills_processes(which):
        pres = _run_powershell(build_process_status_command(list(S8_PROC_MARKERS)),
                               timeout=25)
        rows = _parse_json(pres["stdout"])
        if not rows and pres["rc"] != 0:
            proc_query_ok = False
        for p in rows:
            pid = p.get("ProcessId")
            cmd = str(p.get("CommandLine") or "")
            marker = next((m for m in S8_PROC_MARKERS if m in cmd), "s8 process")
            procs.append({"pid": pid, "marker": marker,
                          "phrase": f"Strategy 8 python process running (PID {pid}, "
                                    f"{marker})"})

    # Plain-English overall.
    live_bits = []
    if running_tasks:
        live_bits.append(f"{running_tasks} scheduled task(s) running")
    if procs:
        live_bits.append(f"{len(procs)} strategy process(es) running")
    if live_bits:
        overall = ("Automation is RUNNING — " + ", ".join(live_bits) + ".")
        overall_tier = "good"
    elif disabled_tasks and disabled_tasks == len(task_names):
        overall = ("Automation is fully HALTED — every task is turned off and no "
                   "strategy processes are running.")
        overall_tier = "warn"
    else:
        overall = ("Automation is idle — nothing is running right now, but tasks "
                   "remain on their schedule and can start automatically.")
        overall_tier = "info"

    return {"which": which, "tasks": tasks, "processes": procs,
            "running_tasks": running_tasks, "disabled_tasks": disabled_tasks,
            "process_query_ok": proc_query_ok,
            "overall": overall, "overall_tier": overall_tier}


# --------------------------------------------------------------------------- #
# FLATTEN — INERT SCAFFOLD. There is NOTHING REAL TO CLOSE today.              #
# There is NO order-transmit code in this module. flatten_execute() RAISES.    #
# --------------------------------------------------------------------------- #
def flatten_preview(which: str = "all") -> dict:
    """INERT. Return a truthful, plain-English statement that there is nothing
    real to close today. Opens NO broker connection and builds NO order objects."""
    which = (which or "all").lower()
    lines = [
        "There is nothing real to close right now — the desk holds no live "
        "positions that can be flattened.",
        "Strategy 8 is a zero-transmit pilot: it records what it WOULD have "
        "traded and holds NO real positions.",
        "Strategy 0 runs on the paper account and is real-money gated — no live "
        "position exists to close.",
    ]
    if which == "s8":
        headline = ("Strategy 8 holds no real positions (zero-transmit pilot) — "
                    "nothing to close.")
    elif which == "s0":
        headline = ("Strategy 0 is on the paper account / real-money gated — "
                    "nothing real to close.")
    else:
        headline = ("Nothing to close: no live positions exist anywhere on the "
                    "desk today.")
    _emit("emergency_flatten_preview", which=which)
    return {"which": which, "armed": False, "headline": headline, "lines": lines}


def flatten_execute(which: str = "all"):
    """THE STUB. Today this raises — there is nothing real to close and there is
    ZERO order-transmit code behind it.

    ------------------------------------------------------------------------
    INTENDED FUTURE DESIGN (NOT built, and not to be built without a deliberate,
    gated, human-armed real-money milestone):

      * Fired by exactly ONE guarded, deliberate HUMAN press. Never by the AI,
        never on a schedule, never as a side effect of anything.
      * A true emergency close of REAL positions:
          - IBKR global order cancel (reqGlobalCancel) to pull every working
            order first, then
          - aggressive closing orders per open position (marketable limits, or
            market where appropriate) to get flat fast.
      * PER STRATEGY, because the two books behave very differently:
          - Strategy 8 = fast 0-days-to-expiry option spreads (close the spread
            as a unit, mind pin risk / assignment near expiry), whereas
          - Strategy 0 = a slower, longer-horizon allocation book (unwind the
            equity/ETF positions in an orderly way).
      * Confirm flat afterward and report exactly what was sent and filled.
      * It is ALWAYS the human's finger on the trigger — the same review -> arm
        -> transmit gate that governs every real order on this desk. Building any
        part of the transmit path is an architecture change requiring explicit
        sign-off, and it belongs somewhere with a real order router, NOT here.
    ------------------------------------------------------------------------
    """
    _emit("emergency_flatten_blocked", which=(which or "all").lower())
    raise NotImplementedError(
        "Real-money emergency flatten is not armed yet — nothing real to close "
        "(Strategy 8 pilot holds no real positions; Strategy 0 is paper/gated). "
        "When real trading is live, this becomes one guarded human press = send "
        "aggressive closing orders + broker global-cancel. It will never be fired "
        "by the AI or automatically."
    )


# --------------------------------------------------------------------------- #
# The persistent, guarded Streamlit control strip (top of every page).         #
# streamlit + theme are imported LAZILY so this module stays importable (and    #
# testable, and usable by the standalone kill switch) without Streamlit.        #
# --------------------------------------------------------------------------- #
def render_emergency_bar() -> None:
    """A persistent, GUARDED emergency control strip meant to render at the TOP of
    every page. Collapsed by default; destructive actions require typing an exact
    confirm word. Everything here is read-only-safe with respect to trading —
    Halt is OS-level process/task control, Flatten is inert."""
    import streamlit as st  # lazy — keeps the module import-light for the CLI
    try:
        import theme as T
    except Exception:  # pragma: no cover
        T = None

    with st.expander(
        "🚨 Emergency controls (open only if you need to stop or flatten)",
        expanded=False,
    ):
        # --- Current state, in plain English. --------------------------------
        try:
            status = halt_status("all")
            tier = status["overall_tier"]
            if T is not None:
                st.markdown(
                    T.status_card("Right now", tier, status["overall"]),
                    unsafe_allow_html=True,
                )
            else:
                st.info(status["overall"])
            with st.expander("Show each task and process", expanded=False):
                for t in status["tasks"]:
                    st.write(f"- **{t['friendly']}** (`{t['name']}`): {t['phrase']}")
                if status["processes"]:
                    for p in status["processes"]:
                        st.write(f"- {p['phrase']}")
                elif _kills_processes("all"):
                    st.write("- No Strategy 8 python processes are running.")
        except Exception as exc:  # never let status reads break the control bar
            st.warning(
                "Could not read the current automation status right now "
                f"({type(exc).__name__}). The Halt buttons below still work."
            )

        st.divider()

        # --- HALT — real, OS-level. Guarded by typing HALT. ------------------
        st.markdown("#### Halt automation (stops the software — sends no orders)")
        st.caption(
            "This stops and turns off the desk's scheduled tasks and force-stops "
            "the running strategy programs, so nothing restarts. It does NOT "
            "contact any broker and does NOT place, cancel, or transmit any order. "
            "Type the word HALT below, then press a button."
        )
        halt_word = st.text_input(
            "Type HALT to confirm a stop", value="", key="emg_halt_word",
            placeholder="type HALT here",
        )
        confirmed_halt = halt_word.strip().upper() == "HALT"
        c1, c2, c3 = st.columns(3)
        halt_choice = None
        with c1:
            if st.button("🛑 Halt ALL automation", key="emg_halt_all",
                         use_container_width=True):
                halt_choice = "all"
        with c2:
            if st.button("Halt Strategy 8 only", key="emg_halt_s8",
                         use_container_width=True):
                halt_choice = "s8"
        with c3:
            if st.button("Halt Strategy 0 only", key="emg_halt_s0",
                         use_container_width=True):
                halt_choice = "s0"

        if halt_choice is not None:
            if not confirmed_halt:
                st.warning("Type HALT (all capitals) in the box above to confirm, "
                           "then press the button again. Nothing was stopped.")
            else:
                with st.spinner(f"Halting {_which_label(halt_choice)}…"):
                    report = halt_strategy(halt_choice)
                if report["ok"]:
                    st.success(report["summary"])
                elif report.get("needs_admin"):
                    st.error(report["summary"])
                else:
                    st.warning(report["summary"])
                for a in report["actions"]:
                    icon = "✅" if a["ok"] else ("🔒" if a.get("needs_admin") else "⚠️")
                    st.write(f"{icon} {a['message']}")

        st.divider()

        # --- FLATTEN — inert scaffold. Guarded by typing FLATTEN. -------------
        st.markdown("#### Get flat (emergency close) — not armed yet")
        st.caption(
            "When real-money trading is live, this will close open positions in an "
            "emergency. TODAY there is nothing real to close: Strategy 8 is a "
            "zero-transmit pilot holding no real positions, and Strategy 0 is on "
            "the paper account / real-money gated. This button sends NO orders."
        )
        preview = flatten_preview("all")
        st.info(preview["headline"])
        flat_word = st.text_input(
            "Type FLATTEN to confirm (currently does nothing to close)",
            value="", key="emg_flatten_word", placeholder="type FLATTEN here",
        )
        confirmed_flat = flat_word.strip().upper() == "FLATTEN"
        if st.button("Get flat (emergency close)", key="emg_flatten_btn",
                     use_container_width=True):
            if not confirmed_flat:
                st.warning("Type FLATTEN (all capitals) in the box above to "
                           "confirm, then press the button again.")
            else:
                # Wire the real stub behind the confirm. Today it raises; we catch
                # it and show the plain-English truth instead of a traceback.
                try:
                    flatten_execute("all")
                except NotImplementedError as exc:
                    st.info(f"Nothing to flatten yet — {exc}")
                except Exception as exc:  # defensive; must never crash the page
                    st.info(f"Nothing to flatten yet — {exc}")
