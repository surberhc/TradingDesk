<#
    register_s0_month_end_snapshot_task.ps1  --  register the S0MonthEndSnapshot task (Job A).

    *** NOT RUN BY THE BUILD THAT CREATED IT. ***
    Andrew owns scheduling. This script exists so registration is written down rather than
    improvised; nothing registered it for you. Run it yourself, from an ELEVATED shell, when
    you want the snapshot on a schedule:

      (a) Win+X -> "Terminal (Admin)" -> accept the UAC prompt, then:
              powershell -ExecutionPolicy Bypass -File
                  "C:\TradingDesk\dailyreport\register_s0_month_end_snapshot_task.ps1"

      (b) From any existing (non-elevated) shell -- pops UAC for you:
              Start-Process powershell -Verb RunAs -ArgumentList '-NoExit',
                  '-ExecutionPolicy','Bypass','-File',
                  'C:\TradingDesk\dailyreport\register_s0_month_end_snapshot_task.ps1'

    WHAT IT REGISTERS (idempotent -- unregisters then re-registers):
      S0MonthEndSnapshot -> run_s0_month_end_snapshot.cmd
        Trigger : weekly, Mon-Fri at 14:50 local (~2:50pm CT), StartWhenAvailable.
                  14:50 CT is BEFORE the ~3:05pm CT live-trading Gateway teardown, so the
                  read-only holdings snapshot can still connect. The script self-checks the
                  trading calendar and only snapshots on Strategy 0's month-end SIGNAL day
                  (last trading day of the month), so a plain weekday trigger is correct --
                  it does nothing the other ~20 weekdays a month and exits 0.
        Logon   : Password (run whether-logged-on) so it survives logoff.
        Instances: IgnoreNew -- a re-fire while one is running is a safe no-op.

    IMPORTANT -- GATEWAY / LOGON CAVEAT: Job A needs the live-trading Gateway (port 4003) up
    to read the account, and the Gateway itself is brought up under Andrew's interactive
    login (see the S8 morning bring-up). If the desk's task principals run only when Andrew
    is logged on (Interactive), this snapshot inherits that same limitation -- it can only
    capture holdings while the Gateway is running. If the Gateway is down at 14:50, Job A
    writes an honest FAILED marker and the evening Job B emails "could not read holdings at
    close" rather than guessing a verdict.

    This task is INFORMATIONAL + READ-ONLY. It connects READ-ONLY (readonly=True) to read
    positions + NetLiquidation; it builds no order and transmits nothing. Reverse it any time
    with:  Unregister-ScheduledTask -TaskName S0MonthEndSnapshot -Confirm:$false

    Your password is never hardcoded: it is prompted for (Get-Credential) and passed straight
    to Register-ScheduledTask.

    VERIFY-DON'T-CLAIM: this script asserts the task actually exists with the expected
    trigger/principal after registering, and exits non-zero if not.
#>

$ErrorActionPreference = 'Stop'

$TaskName = 'S0MonthEndSnapshot'
$Launcher = Join-Path $PSScriptRoot 'run_s0_month_end_snapshot.cmd'

# --- 0. Elevation self-check ------------------------------------------------
$isAdmin = ([Security.Principal.WindowsPrincipal] `
            [Security.Principal.WindowsIdentity]::GetCurrent()
           ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Error "Not elevated. Open Win+X -> 'Terminal (Admin)', then re-run: powershell -ExecutionPolicy Bypass -File `"$PSCommandPath`""
    exit 1
}

if (-not (Test-Path $Launcher)) {
    Write-Error "Launcher not found: $Launcher"
    exit 1
}

# --- 1. Credentials (prompted, never stored) --------------------------------
Write-Host "Enter the Windows password for $env:USERDOMAIN\$env:USERNAME"
Write-Host "(needed so the task runs whether-logged-on; it is never written to disk)."
$cred = Get-Credential -UserName "$env:USERDOMAIN\$env:USERNAME" `
                       -Message 'Password for the S0MonthEndSnapshot scheduled task'

# --- 2. Build + register ----------------------------------------------------
$action = New-ScheduledTaskAction -Execute $Launcher

$trigger = New-ScheduledTaskTrigger -Weekly `
    -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday `
    -At 2:50PM

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries

$task = New-ScheduledTask -Action $action -Trigger $trigger -Settings $settings `
                          -Description 'Job A of the S0 month-end EXACT verdict: close-time holdings snapshot. Runs weekday ~14:50 CT (before the ~3:05pm live-trading Gateway teardown); the script self-checks the NYSE calendar and only snapshots on Strategy 0''s month-end signal day (last trading day of the month). READ-ONLY (readonly=True): reads positions + NetLiquidation from port 4003 and writes an off-repo JSON; builds no order, transmits nothing.'

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Write-Host "Existing $TaskName found -- unregistering before re-register."
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Register-ScheduledTask -TaskName $TaskName -InputObject $task `
                       -User $cred.UserName `
                       -Password $cred.GetNetworkCredential().Password | Out-Null

# --- 3. Assert it is REALLY there (no green banner without proof) -----------
$check = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $check) {
    Write-Error "FAILED: $TaskName does not exist after registration."
    exit 1
}
if (-not ($check.Triggers | Where-Object { $_.CimClass.CimClassName -eq 'MSFT_TaskWeeklyTrigger' })) {
    Write-Error "FAILED: $TaskName exists but has no weekly trigger."
    exit 1
}
if ($check.Principal.LogonType -ne 'Password') {
    Write-Error ("FAILED: $TaskName LogonType is '{0}', expected 'Password' " +
                 "(it would not survive logoff)." -f $check.Principal.LogonType)
    exit 1
}

Write-Host ""
Write-Host "VERIFIED: $TaskName registered." -ForegroundColor Green
Write-Host "  Action  : $Launcher"
Write-Host "  Triggers: weekly Mon-Fri 14:50 (StartWhenAvailable)"
Write-Host "  Logon   : Password (runs whether-logged-on)"
Write-Host ""
Write-Host "Reverse any time with:"
Write-Host "  Unregister-ScheduledTask -TaskName $TaskName -Confirm:`$false"
exit 0
