# register_livedata_morning_watchdog_task.ps1
# ---------------------------------------------------------------------------
# Registers the TWO one-shot "still down" alarm tasks for the read-only live-DATA
# Gateway (port 4001). Both run the SAME existing launcher
# C:\TradingDesk\livebot\run_livedata_morning_watchdog.cmd
# (livebot\livedata_morning_watchdog.py), which TCP-probes 4001 and, if it is not
# serving on a trading day, emails ONE nudge through the existing dailyreport mailer
# and exits. The module's own trading-day guard skips weekends/holidays.
#
#   (a) LiveDataMorningStillDownAlarm_0845CT  - weekdays 08:45 CT, one shot.
#       ALREADY REGISTERED — this task was created by hand on 2026-08-05 and has been
#       firing correctly at 08:45 CT on weekdays ever since; it emailed a real
#       "4001 NOT confirmed up" nudge on 2026-08-20, 2026-08-24 and again on 2026-08-31.
#       What was missing was never the task itself — it was this script. This is the
#       (previously missing) checked-in REGISTRATION SCRIPT for it: running it is
#       idempotent and simply makes that hand registration reproducible and
#       version-controlled.
#
#   (b) LiveDataPreForwardPullAlarm_1700CT    - weekdays 17:00 CT, one shot.
#       The genuinely NEW task — this script is what adds it.
#       The nightly EOD option pull (datacollector\forward_daily_live.py, ~17:30 CT) is
#       the thing that silently fails when 4001 is down. An 08:45-only check cannot
#       catch a gateway that wedges during the day, so this second one-shot probe gives
#       ~30 minutes of warning before the pull runs.
#
# SAFE TO REGISTER: the module these tasks run ONLY TCP-probes port 4001 and may send
# one email through the existing dailyreport mailer. It launches NOTHING and can never
# push a 2FA itself. No elevation, no stored password, no order path.
#
# CAVEAT — INTERACTIVE LOGON HOLE (deliberately left open here):
#   Both tasks are Principal LogonType=Interactive, matching their siblings
#   (LiveDataGatewayChainedOpen_0805CT, LiveTradeGatewayOpen_0800CT,
#   S8MorningStillDownAlarm_0845CT). That means they do NOT fire when Andrew is not
#   logged into Windows — the exact silent-failure hole documented for
#   S8MorningStillDownAlarm_0845CT on 2026-08-24. Re-registering these as
#   LogonType=Password (the way HeartbeatStalenessAlarm is registered) WOULD close that
#   hole, but it requires an interactive credential prompt that only Andrew can answer,
#   so it is deliberately left as a follow-up rather than guessed at here.
#
# Idempotent/re-runnable: registered with -Force, so re-running replaces in place.
# Remove with  Unregister-ScheduledTask -TaskName '<name>'.
# Touches NOTHING else — no gateway, no port, no other scheduled task.
# ---------------------------------------------------------------------------
$ErrorActionPreference = 'Stop'

$cmd = 'C:\TradingDesk\livebot\run_livedata_morning_watchdog.cmd'

# Shared: same principal / settings idiom as the sibling gateway tasks.
$prin = New-ScheduledTaskPrincipal -UserId 'andre' -LogonType Interactive -RunLevel Limited

$set  = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

# --- (a) 08:45 CT morning still-down alarm -------------------------------------
$actA  = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument ('/c "' + $cmd + '"')

# Weekdays at 08:45 CT. ONE SHOT — no repetition.
$trigA = New-ScheduledTaskTrigger -Weekly `
         -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At '8:45AM'

$descA = 'One-shot morning "still down" alarm for the read-only live-DATA Gateway ' +
         '(port 4001). Weekdays 08:45 CT: TCP-probes 4001 and, if it is not serving on ' +
         'a trading day, emails ONE nudge via the dailyreport mailer, then exits. ' +
         'Probe-and-email only: launches nothing, never pushes a 2FA. LogonType=' +
         'Interactive, so it does NOT fire when nobody is logged into Windows. ' +
         'Registered by hand 2026-08-05 and firing correctly since; this registration ' +
         'script was checked in 2026-08-31 to make it reproducible.'

Register-ScheduledTask -TaskName 'LiveDataMorningStillDownAlarm_0845CT' -Action $actA `
        -Trigger $trigA -Principal $prin -Settings $set -Description $descA -Force | Out-Null

# --- (b) 17:00 CT pre-forward-pull alarm ---------------------------------------
$actB  = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument ('/c "' + $cmd + '"')

# Weekdays at 17:00 CT. ONE SHOT — no repetition.
$trigB = New-ScheduledTaskTrigger -Weekly `
         -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At '5:00PM'

$descB = 'One-shot pre-pull "still down" alarm for the read-only live-DATA Gateway ' +
         '(port 4001). Weekdays 17:00 CT, ~30 min before the nightly EOD option pull ' +
         '(datacollector\forward_daily_live.py, ~17:30 CT) which silently fails when ' +
         '4001 is down. The 08:45 check cannot catch a gateway that wedges during the ' +
         'day, so this second probe gives warning while there is still time to act. ' +
         'Same module: probe-and-email only, launches nothing, never pushes a 2FA. ' +
         'LogonType=Interactive, so it does NOT fire when nobody is logged into ' +
         'Windows. Registered 2026-08-31.'

Register-ScheduledTask -TaskName 'LiveDataPreForwardPullAlarm_1700CT' -Action $actB `
        -Trigger $trigB -Principal $prin -Settings $set -Description $descB -Force | Out-Null

# --- verification -------------------------------------------------------------
foreach ($name in @('LiveDataMorningStillDownAlarm_0845CT','LiveDataPreForwardPullAlarm_1700CT')) {
    $t = Get-ScheduledTask -TaskName $name
    Write-Output ("REGISTERED " + $name + " state=" + $t.State + " user=" + $t.Principal.UserId +
                  " logon=" + $t.Principal.LogonType + " runlevel=" + $t.Principal.RunLevel)
    Write-Output ("ACTION: " + $t.Actions[0].Execute + " " + $t.Actions[0].Arguments)
    Write-Output ("TRIGGER: start=" + $t.Triggers[0].StartBoundary + " days=" + $t.Triggers[0].DaysOfWeek +
                  " repetition=" + $(if ($t.Triggers[0].Repetition.Interval) { $t.Triggers[0].Repetition.Interval } else { 'none' }))
    Write-Output ("NEXTRUN: " + (Get-ScheduledTaskInfo -TaskName $name).NextRunTime)
    Write-Output ''
}
