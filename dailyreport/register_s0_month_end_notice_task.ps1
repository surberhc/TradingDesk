<#
    register_s0_month_end_notice_task.ps1  --  register the S0MonthEndNotice task.

    *** NOT RUN BY THE BUILD THAT CREATED IT. ***
    Andrew owns scheduling. This script exists so registration is written down
    rather than improvised; nothing registered it for you. Run it yourself, from
    an ELEVATED shell, when you want the notice on a schedule:

      (a) Win+X -> "Terminal (Admin)" -> accept the UAC prompt, then:
              powershell -ExecutionPolicy Bypass -File
                  "C:\TradingDesk\dailyreport\register_s0_month_end_notice_task.ps1"

      (b) From any existing (non-elevated) shell -- pops UAC for you:
              Start-Process powershell -Verb RunAs -ArgumentList '-NoExit',
                  '-ExecutionPolicy','Bypass','-File',
                  'C:\TradingDesk\dailyreport\register_s0_month_end_notice_task.ps1'

    WHAT IT REGISTERS (idempotent -- unregisters then re-registers):
      S0MonthEndNotice -> run_s0_month_end_notice.cmd
        Trigger : weekly, Mon-Fri at 18:30 local (~18:30 CT), StartWhenAvailable.
                  18:30 is after the 4pm ET / 3pm CT cash close on every weekday;
                  the script self-checks the trading calendar and only emails on
                  Strategy 0's month-end SIGNAL day (last trading day of the month),
                  so a plain weekday trigger is correct -- it does nothing the other
                  ~20 evenings a month and exits 0.
        Logon   : Password (run whether-logged-on) so it survives logoff.
        Instances: IgnoreNew -- a re-fire while one is running is a safe no-op.

    This task is INFORMATIONAL only -- it emails the owner a heads-up. It touches
    NO order path, reads NO account, connects to NO gateway. Reverse it any time
    with a plain:  Unregister-ScheduledTask -TaskName S0MonthEndNotice -Confirm:$false

    Your password is never hardcoded: it is prompted for (Get-Credential) and passed
    straight to Register-ScheduledTask.

    VERIFY-DON'T-CLAIM: this script asserts the task actually exists with the
    expected trigger/principal after registering, and exits non-zero if not.
#>

$ErrorActionPreference = 'Stop'

$TaskName = 'S0MonthEndNotice'
$Launcher = Join-Path $PSScriptRoot 'run_s0_month_end_notice.cmd'

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
                       -Message 'Password for the S0MonthEndNotice scheduled task'

# --- 2. Build + register ----------------------------------------------------
$action = New-ScheduledTaskAction -Execute $Launcher

$trigger = New-ScheduledTaskTrigger -Weekly `
    -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday `
    -At 6:30PM

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries

$task = New-ScheduledTask -Action $action -Trigger $trigger -Settings $settings `
                          -Description 'Once-a-month S0 rebalance heads-up email. Runs weekday evenings 18:30; the script self-checks the NYSE calendar and only emails on Strategy 0''s month-end signal day (last trading day of the month). Informational only -- no order path, no gateway, no account read.'

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
Write-Host "  Triggers: weekly Mon-Fri 18:30 (StartWhenAvailable)"
Write-Host "  Logon   : Password (runs whether-logged-on)"
Write-Host ""
Write-Host "Reverse any time with:"
Write-Host "  Unregister-ScheduledTask -TaskName $TaskName -Confirm:`$false"
exit 0
