<#
    register_universe_tasks.ps1  --  ONE-CLICK task registration for the expanded-universe
    options pull + the ThetaData terminal boot-hole fix.

    HOW TO RUN (Andrew): right-click this file -> "Run as administrator".
    (Or, from an elevated PowerShell: powershell -ExecutionPolicy Bypass -File
     "C:\TradingDesk-Local\warehouse\register_universe_tasks.ps1")

    WHAT IT REGISTERS (idempotent -- safe to re-run; unregisters-then-registers each):
      1. UniverseDownloadEod       -> run_universe_download_eod.bat  (user 'andre', Password
                                       logon = run whether-logged-on, survives logoff)
      2. ThetaTerminalWatchdogBoot -> run_theta_watchdog.bat         (SYSTEM, AtStartup =
                                       fills the terminal boot-hole after a power-outage
                                       reboot with nobody logged in)

    Both mirror the PROVEN CanslimIbkrPriceGapfill task:
      RunLevel = Limited, triggers = AtStartup + AtLogon + every-15-min repeat (10 yr),
      StartWhenAvailable = true, ExecutionTimeLimit = unlimited, MultipleInstances = IgnoreNew.
    The download launcher and the terminal watchdog are each SINGLETON-guarded, so the
    overlapping/boot triggers can never spawn a duplicate -- a re-fire is a safe no-op.

    NON-DESTRUCTIVE: this does NOT touch the existing ThetaTerminalWatchdog task; it only
    ADDS the separate ThetaTerminalWatchdogBoot task. Your password is never hardcoded --
    it is prompted for (Get-Credential) and passed straight to Register-ScheduledTask.
#>

$ErrorActionPreference = 'Stop'

# --- 0. Elevation self-check ------------------------------------------------------------
$isAdmin = ([Security.Principal.WindowsPrincipal] `
            [Security.Principal.WindowsIdentity]::GetCurrent() `
           ).IsInRole([Security.Principal.WindowsBuiltinRole]::Administrator)
if (-not $isAdmin) {
    Write-Host ""
    Write-Host "  NOT ELEVATED. Right-click this file -> 'Run as administrator' and try again." -ForegroundColor Red
    Write-Host "  (Registering a whether-logged-on / SYSTEM task requires an elevated shell.)" -ForegroundColor Red
    Write-Host ""
    Read-Host "Press Enter to close"
    exit 1
}

# --- Paths (verify the launchers exist before we register anything) ---------------------
$dlBat    = 'C:\TradingDesk-Local\warehouse\run_universe_download_eod.bat'
$wdBat    = 'C:\TradingDesk-Local\warehouse\run_theta_watchdog.bat'
foreach ($p in @($dlBat, $wdBat)) {
    if (-not (Test-Path $p)) {
        Write-Host "  MISSING launcher: $p  -- aborting (nothing registered)." -ForegroundColor Red
        Read-Host "Press Enter to close"; exit 1
    }
}

# --- Shared settings (mirror CanslimIbkrPriceGapfill) -----------------------------------
# Triggers: AtStartup + AtLogon + a time trigger repeating every 15 min for ~10 years.
function New-DeskTriggers {
    $boot  = New-ScheduledTaskTrigger -AtStartup
    $logon = New-ScheduledTaskTrigger -AtLogOn
    $rep   = New-ScheduledTaskTrigger -Once -At (Get-Date) `
                 -RepetitionInterval (New-TimeSpan -Minutes 15) `
                 -RepetitionDuration (New-TimeSpan -Days 3650)
    return @($boot, $logon, $rep)
}
$settings = New-ScheduledTaskSettingsSet `
                -StartWhenAvailable `
                -AllowStartIfOnBatteries `
                -DontStopIfGoingOnBatteries `
                -MultipleInstances IgnoreNew `
                -ExecutionTimeLimit ([TimeSpan]::Zero)   # Zero = unlimited

function Register-OneTask {
    param(
        [string] $Name,
        [string] $BatPath,
        [Microsoft.Management.Infrastructure.CimInstance] $Principal,
        [object] $Triggers,
        [string] $Description
    )
    # Idempotent: remove any existing task of this name first.
    $existing = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    if ($existing) {
        Unregister-ScheduledTask -TaskName $Name -Confirm:$false
        Write-Host "  (removed existing $Name before re-register)" -ForegroundColor DarkGray
    }
    $action = New-ScheduledTaskAction -Execute $BatPath
    Register-ScheduledTask -TaskName $Name -Action $action -Trigger $Triggers `
        -Principal $Principal -Settings $settings -Description $Description | Out-Null
    $info = Get-ScheduledTask -TaskName $Name
    Write-Host ("  OK  registered {0}  (logon={1}, runlevel={2})" -f `
        $Name, $info.Principal.LogonType, $info.Principal.RunLevel) -ForegroundColor Green
}

Write-Host ""
Write-Host "=== Registering TradingDesk universe-pull tasks ===" -ForegroundColor Cyan
Write-Host ""

# --- 1. UniverseDownloadEod  (user 'andre', Password logon = whether-logged-on) ----------
Write-Host "Enter the Windows password for account 'andre' (for the whether-logged-on task)." -ForegroundColor Yellow
Write-Host "It is passed straight to Task Scheduler and never stored by this script." -ForegroundColor Yellow
$cred = Get-Credential -UserName 'andre' -Message "Windows password for 'andre' (UniverseDownloadEod)"

# Principal built from the credential so the task runs whether-logged-on under 'andre'.
$dlPrincipal = New-ScheduledTaskPrincipal -UserId $cred.UserName -LogonType Password -RunLevel Limited

# Register with the password (Register-ScheduledTask -User/-Password consumes the credential).
$existing = Get-ScheduledTask -TaskName 'UniverseDownloadEod' -ErrorAction SilentlyContinue
if ($existing) {
    Unregister-ScheduledTask -TaskName 'UniverseDownloadEod' -Confirm:$false
    Write-Host "  (removed existing UniverseDownloadEod before re-register)" -ForegroundColor DarkGray
}
$dlAction   = New-ScheduledTaskAction -Execute $dlBat
$dlTriggers = New-DeskTriggers
$dlTask     = New-ScheduledTask -Action $dlAction -Trigger $dlTriggers -Settings $settings `
                -Description ("Expanded-universe options downloader (Priority-1 EOD, 90 new roots, K=4). " +
                              "Singleton-guarded, idempotent, resumable launcher. Triggers AtStartup + " +
                              "AtLogon + every 15 min so it auto-resumes after reboot/logoff; overlapping " +
                              "triggers no-op via the cross-process lock. Runs whether logged on.")
Register-ScheduledTask -TaskName 'UniverseDownloadEod' -InputObject $dlTask `
    -User $cred.UserName -Password $cred.GetNetworkCredential().Password | Out-Null
$dlInfo = Get-ScheduledTask -TaskName 'UniverseDownloadEod'
Write-Host ("  OK  registered UniverseDownloadEod  (logon={0}, runlevel={1})" -f `
    $dlInfo.Principal.LogonType, $dlInfo.Principal.RunLevel) -ForegroundColor Green

# --- 2. ThetaTerminalWatchdogBoot  (SYSTEM, AtStartup only -- the boot-hole fix) ---------
# SYSTEM needs no password; AtStartup fires after a power-outage reboot with nobody logged
# in. The watchdog is singleton-guarded (theta_watchdog.lock), so this is a safe no-op if
# the terminal watchdog is already running. NON-DESTRUCTIVE: does not touch the existing
# ThetaTerminalWatchdog task.
$sysPrincipal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Limited
Register-OneTask -Name 'ThetaTerminalWatchdogBoot' -BatPath $wdBat `
    -Principal $sysPrincipal -Triggers (New-ScheduledTaskTrigger -AtStartup) `
    -Description ("Boot-time backstop for the ThetaData terminal watchdog. Fills the AtStartup " +
                  "gap in ThetaTerminalWatchdog (which has only LogOn+Daily triggers) so the " +
                  "terminal comes back up after a power-outage reboot with nobody logged in. " +
                  "Invokes run_theta_watchdog.bat; the watchdog singleton makes it a safe no-op " +
                  "if already running. Does NOT modify the existing ThetaTerminalWatchdog task.")

Write-Host ""
Write-Host "=== DONE. Both tasks registered. ===" -ForegroundColor Cyan
Write-Host "The expanded-universe pull now auto-resumes across reboot/logoff, and the ThetaData" -ForegroundColor Gray
Write-Host "terminal is restarted at boot. Overlapping/boot triggers are safe no-ops (singletons)." -ForegroundColor Gray
Write-Host ""
Write-Host "Verify anytime:" -ForegroundColor Gray
Write-Host "  Get-ScheduledTask UniverseDownloadEod, ThetaTerminalWatchdogBoot | Format-Table TaskName, State" -ForegroundColor Gray
Write-Host "  type C:\TradingDesk-Local\warehouse\universe_dl_state\universe_dl_progress.json" -ForegroundColor Gray
Write-Host ""
Read-Host "Press Enter to close"
