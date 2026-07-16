<#
    register_repo_backup_task.ps1  --  register the RepoBackupDaily scheduled task.

    *** NOT RUN BY THE BUILD THAT CREATED IT. ***
    Andrew owns scheduling. This script exists so registration is written down
    rather than improvised; nothing registered it for you. Run it yourself, from
    an ELEVATED shell, when you want the backup on a schedule.

    NOTE: an earlier version of this header said to right-click the .ps1 and pick
    "Run as administrator". That instruction was impossible to follow -- Windows 11
    puts no such entry on .ps1 files. The only right-click entry is "Run with
    PowerShell" (buried under "Show more options"), and it is NOT elevated, so it
    trips the self-check below. Use one of these instead:

      (a) Win+X -> "Terminal (Admin)" -> accept the UAC prompt, then:
              powershell -ExecutionPolicy Bypass -File
                  "C:\TradingDesk\datacollector\register_repo_backup_task.ps1"

      (b) From any existing (non-elevated) shell -- pops UAC for you:
              Start-Process powershell -Verb RunAs -ArgumentList '-NoExit',
                  '-ExecutionPolicy','Bypass','-File',
                  'C:\TradingDesk\datacollector\register_repo_backup_task.ps1'
          -NoExit keeps the new window open so you can read the verification
          banner instead of watching it flash shut.

    WHAT IT REGISTERS (idempotent -- unregisters then re-registers):
      RepoBackupDaily -> run_repo_backup.cmd
        Trigger : daily at 20:00 local, plus AtLogon, StartWhenAvailable = true
                  (so a machine that was off at 20:00 still backs up once it wakes).
        Logon   : Password (run whether-logged-on) so it survives logoff.
        Instances: IgnoreNew -- a re-fire while one is running is a safe no-op.

    IF YOU CHANGE THE CADENCE, CHANGE THE ALARM TOO. heartbeat_alarm.py's
    REPO_BACKUP_THRESHOLD_S is 26h, which assumes this DAILY trigger (24h + 2h
    grace). A less-frequent cadence will false-page; a much more frequent one will
    let a real outage sleep longer than it should. The threshold is not derived
    from the task automatically -- that is a deliberate limitation, called out
    rather than hidden.

    Your password is never hardcoded: it is prompted for (Get-Credential) and
    passed straight to Register-ScheduledTask.

    VERIFY-DON'T-CLAIM: this script asserts the task actually exists with the
    expected trigger/principal after registering, and exits non-zero if not. A
    prior task-registration script in this repo printed "OK registered" for a task
    that did not exist (see register_universe_tasks.ps1's 2026-07-06 note) --
    this one cannot lie the same way.
#>

$ErrorActionPreference = 'Stop'

$TaskName = 'RepoBackupDaily'
$Launcher = Join-Path $PSScriptRoot 'run_repo_backup.cmd'

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
                       -Message 'Password for the RepoBackupDaily scheduled task'

# --- 2. Build + register ----------------------------------------------------
$action = New-ScheduledTaskAction -Execute $Launcher

$triggers = @(
    (New-ScheduledTaskTrigger -Daily -At 8:00PM),
    (New-ScheduledTaskTrigger -AtLogOn)
)

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries

$task = New-ScheduledTask -Action $action -Trigger $triggers -Settings $settings `
                          -Description 'Verified git-bundle backup of C:\TradingDesk to Google Drive + local. Exits non-zero on any unverified result; heartbeat_alarm.py job "repo_backup" pages if no verified backup lands within 26h.'

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
if (-not ($check.Triggers | Where-Object { $_.CimClass.CimClassName -eq 'MSFT_TaskDailyTrigger' })) {
    Write-Error "FAILED: $TaskName exists but has no daily trigger."
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
Write-Host "  Triggers: daily 20:00 + AtLogon (StartWhenAvailable)"
Write-Host "  Logon   : Password (runs whether-logged-on)"
Write-Host ""
Write-Host "Run it once now to confirm end-to-end:"
Write-Host "  Start-ScheduledTask -TaskName $TaskName"
Write-Host "Then check:  C:\TradingDesk-Local\backups\repo_backup_status.json"
exit 0
