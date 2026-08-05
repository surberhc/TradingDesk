# register_livedata_chained_open_task.ps1
# ---------------------------------------------------------------------------
# Registers "LiveDataGatewayChainedOpen_0805CT" — the DEPENDENCY-GATED morning bring-up
# for the read-only live-DATA Gateway (port 4001), chained behind the 4003 live-trade
# lane so the two IBKR Mobile 2FA prompts never overlap.
#
# Each ~5-min cycle (livebot\livedata_chained_open.py): 4001 already up -> nothing; else
# 4003 confirmed up -> launch 4001 (fires its own 2FA); else (4003 pending) -> wait. So
# there is never more than ONE pending 2FA, and if the owner never answers 4003, 4001
# never fires. Once 4001 is up it stays up via AutoRestartTime + the watchdog,
# INDEPENDENT of 4003.
#
# Runs WHEN LOGGED ON as 'andre' (Interactive, Limited) so the Gateway GUI / 2FA renders
# on the desktop — same principal as the 4003 open task and the morning alert; needs NO
# elevation and NO stored password. Launched hidden via run_hidden.vbs.
#
# Registered 2026-08-05 as the attended, gated replacement for BOTH the disabled
# unattended 17:20 cold-launcher (LiveDataGatewayEnsureUp_1720CT) and the earlier
# fixed-offset LiveDataGatewayOpen_0812CT (unregistered). Does NOT touch the 4003 lane —
# it only observes 4003's port read-only. Idempotent/reversible: re-run with -Force to
# replace; remove with  Unregister-ScheduledTask -TaskName 'LiveDataGatewayChainedOpen_0805CT'.
# ---------------------------------------------------------------------------
$ErrorActionPreference = 'Stop'

$act  = New-ScheduledTaskAction -Execute 'wscript.exe' `
        -Argument '//B //Nologo "C:\TradingDesk\run_hidden.vbs" "C:\TradingDesk\livebot\run_livedata_chained_open.cmd"'

# Weekdays: first check 08:05 CT, then every 5 min until 12:00 CT (window = 3h55m).
$trig = New-ScheduledTaskTrigger -Weekly `
        -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At '8:05AM'
$rep  = (New-ScheduledTaskTrigger -Once -At '8:05AM' `
        -RepetitionInterval (New-TimeSpan -Minutes 5) `
        -RepetitionDuration (New-TimeSpan -Hours 3 -Minutes 55)).Repetition
$trig.Repetition = $rep

$prin = New-ScheduledTaskPrincipal -UserId 'andre' -LogonType Interactive -RunLevel Limited

$set  = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

$desc = 'Dependency-gated morning bring-up for the read-only live-DATA Gateway (port ' +
        '4001), chained behind 4003. Weekdays every 5 min 08:05-12:00 CT: launches 4001 ' +
        'ONLY after 4003 is confirmed up, so the two 2FA pushes never overlap. Read-only ' +
        'lane; observes 4003 read-only, never touches it. Registered ENABLED 2026-08-05.'

Register-ScheduledTask -TaskName 'LiveDataGatewayChainedOpen_0805CT' -Action $act -Trigger $trig `
        -Principal $prin -Settings $set -Description $desc -Force | Out-Null

$t = Get-ScheduledTask -TaskName 'LiveDataGatewayChainedOpen_0805CT'
$r = $t.Triggers[0].Repetition
Write-Output ("REGISTERED state=" + $t.State + " user=" + $t.Principal.UserId +
              " logon=" + $t.Principal.LogonType + " runlevel=" + $t.Principal.RunLevel)
Write-Output ("ACTION: " + $t.Actions[0].Execute + " " + $t.Actions[0].Arguments)
Write-Output ("TRIGGER: start=" + $t.Triggers[0].StartBoundary + " days=" + $t.Triggers[0].DaysOfWeek +
              " interval=" + $r.Interval + " duration=" + $r.Duration)
Write-Output ("NEXTRUN: " + (Get-ScheduledTaskInfo -TaskName 'LiveDataGatewayChainedOpen_0805CT').NextRunTime)
