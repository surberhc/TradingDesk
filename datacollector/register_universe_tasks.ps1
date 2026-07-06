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

    SILENT-FAILURE FIX (2026-07-06):
      An audit found ThetaTerminalWatchdogBoot did NOT exist even though a prior version of
      this script printed "OK registered". Two root causes, both fixed here:
        (a) The SYSTEM principal was built with -UserId 'SYSTEM' (a bare word) and passed
            straight to Register-ScheduledTask -Principal. That does not reliably resolve to
            NT AUTHORITY\SYSTEM. This version uses the well-known SID 'S-1-5-18' and registers
            through a New-ScheduledTask object (the same object-then-register path that made
            UniverseDownloadEod succeed).
        (b) The "verification" only read the task name back and printed it -- it asserted
            NOTHING, so a missing/partial/wrong-principal task still printed a green "OK".
            This version calls Assert-TaskRegistered after every registration: it FAILS LOUDLY
            (throws) unless the task actually exists AND carries the expected principal type
            AND the expected trigger kind. The final banner reports true success/failure and
            the script exits non-zero if any task did not verify -- it can no longer lie.
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

# --- REAL post-registration verification -------------------------------------------------
# Reads the task back from Task Scheduler and ASSERTS the things that actually matter. Throws
# (fails loudly) on any mismatch so a missing / partial / wrong-principal task can NEVER print
# a false "OK". This is the fix for the silent-failure audit finding.
#   -ExpectLogonType : e.g. 'ServiceAccount' (SYSTEM) or 'Password' (whether-logged-on 'andre')
#   -ExpectBootTrigger : require at least one AtStartup (MSFT_TaskBootTrigger) trigger
function Assert-TaskRegistered {
    param(
        [string] $Name,
        [string] $ExpectLogonType,
        [bool]   $ExpectBootTrigger
    )
    $t = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    if (-not $t) {
        throw "VERIFY FAILED: task '$Name' does NOT exist after registration."
    }
    $logon = [string] $t.Principal.LogonType
    if ($ExpectLogonType -and ($logon -ne $ExpectLogonType)) {
        throw "VERIFY FAILED: task '$Name' LogonType is '$logon', expected '$ExpectLogonType' (principal did not resolve)."
    }
    if ($ExpectBootTrigger) {
        $hasBoot = $false
        foreach ($trg in @($t.Triggers)) {
            # AtStartup triggers surface as CIM class MSFT_TaskBootTrigger.
            if ($trg.CimClass.CimClassName -eq 'MSFT_TaskBootTrigger') { $hasBoot = $true; break }
        }
        if (-not $hasBoot) {
            throw "VERIFY FAILED: task '$Name' has NO AtStartup (boot) trigger -- the boot-hole would remain open."
        }
    }
    # State should be Ready/Running for an enabled task; Disabled means it will never fire.
    if ([string]$t.State -eq 'Disabled') {
        throw "VERIFY FAILED: task '$Name' is Disabled and will never fire."
    }
    Write-Host ("  VERIFIED  {0}  (logon={1}, runlevel={2}, state={3}, boot-trigger={4})" -f `
        $Name, $logon, $t.Principal.RunLevel, $t.State, $ExpectBootTrigger) -ForegroundColor Green
}

# Registers one task from a principal + triggers, then HARD-VERIFIES it. Used for the SYSTEM
# boot task. Registers through a New-ScheduledTask object (the reliable object-then-register
# path) rather than passing -Principal straight to Register-ScheduledTask.
function Register-OneTask {
    param(
        [string] $Name,
        [string] $BatPath,
        [Microsoft.Management.Infrastructure.CimInstance] $Principal,
        [object] $Triggers,
        [string] $Description,
        [string] $ExpectLogonType,
        [bool]   $ExpectBootTrigger
    )
    # Idempotent: remove any existing task of this name first.
    $existing = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    if ($existing) {
        Unregister-ScheduledTask -TaskName $Name -Confirm:$false
        Write-Host "  (removed existing $Name before re-register)" -ForegroundColor DarkGray
    }
    $action = New-ScheduledTaskAction -Execute $BatPath
    $task   = New-ScheduledTask -Action $action -Trigger $Triggers `
                  -Principal $Principal -Settings $settings -Description $Description
    Register-ScheduledTask -TaskName $Name -InputObject $task | Out-Null
    Assert-TaskRegistered -Name $Name -ExpectLogonType $ExpectLogonType -ExpectBootTrigger $ExpectBootTrigger
}

# Track outcomes so the final banner tells the truth instead of an unconditional "DONE".
$failures = @()

Write-Host ""
Write-Host "=== Registering TradingDesk universe-pull tasks ===" -ForegroundColor Cyan
Write-Host ""

# --- 1. UniverseDownloadEod  (user 'andre', Password logon = whether-logged-on) ----------
try {
    Write-Host "Enter the Windows password for account 'andre' (for the whether-logged-on task)." -ForegroundColor Yellow
    Write-Host "It is passed straight to Task Scheduler and never stored by this script." -ForegroundColor Yellow
    $cred = Get-Credential -UserName 'andre' -Message "Windows password for 'andre' (UniverseDownloadEod)"

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
    # REAL verification: task exists, runs whether-logged-on (Password), has an AtStartup trigger.
    Assert-TaskRegistered -Name 'UniverseDownloadEod' -ExpectLogonType 'Password' -ExpectBootTrigger $true
}
catch {
    Write-Host ("  FAILED  UniverseDownloadEod: {0}" -f $_.Exception.Message) -ForegroundColor Red
    $failures += 'UniverseDownloadEod'
}

# --- 2. ThetaTerminalWatchdogBoot  (SYSTEM, AtStartup only -- the boot-hole fix) ---------
# SYSTEM needs no password; AtStartup fires after a power-outage reboot with nobody logged
# in. The watchdog is singleton-guarded (theta_watchdog.lock), so this is a safe no-op if
# the terminal watchdog is already running. NON-DESTRUCTIVE: does not touch the existing
# ThetaTerminalWatchdog task.
# NOTE: use the well-known SID 'S-1-5-18' (NT AUTHORITY\SYSTEM), NOT the bare word 'SYSTEM' --
# the bare word does not reliably resolve, which is what made the boot task silently vanish.
try {
    $sysPrincipal = New-ScheduledTaskPrincipal -UserId 'S-1-5-18' -LogonType ServiceAccount -RunLevel Limited
    Register-OneTask -Name 'ThetaTerminalWatchdogBoot' -BatPath $wdBat `
        -Principal $sysPrincipal -Triggers (New-ScheduledTaskTrigger -AtStartup) `
        -ExpectLogonType 'ServiceAccount' -ExpectBootTrigger $true `
        -Description ("Boot-time backstop for the ThetaData terminal watchdog. Fills the AtStartup " +
                      "gap in ThetaTerminalWatchdog (which has only LogOn+Daily triggers) so the " +
                      "terminal comes back up after a power-outage reboot with nobody logged in. " +
                      "Invokes run_theta_watchdog.bat; the watchdog singleton makes it a safe no-op " +
                      "if already running. Does NOT modify the existing ThetaTerminalWatchdog task.")
}
catch {
    Write-Host ("  FAILED  ThetaTerminalWatchdogBoot: {0}" -f $_.Exception.Message) -ForegroundColor Red
    $failures += 'ThetaTerminalWatchdogBoot'
}

Write-Host ""
if ($failures.Count -eq 0) {
    Write-Host "=== DONE. Both tasks registered AND verified present. ===" -ForegroundColor Cyan
    Write-Host "The expanded-universe pull now auto-resumes across reboot/logoff, and the ThetaData" -ForegroundColor Gray
    Write-Host "terminal is restarted at boot. Overlapping/boot triggers are safe no-ops (singletons)." -ForegroundColor Gray
} else {
    Write-Host ("=== INCOMPLETE. {0} task(s) did NOT verify: {1} ===" -f $failures.Count, ($failures -join ', ')) -ForegroundColor Red
    Write-Host "Re-run elevated after fixing the cause above; the script is idempotent." -ForegroundColor Red
}
Write-Host ""
Write-Host "Verify anytime:" -ForegroundColor Gray
Write-Host "  Get-ScheduledTask UniverseDownloadEod, ThetaTerminalWatchdogBoot | Format-Table TaskName, State" -ForegroundColor Gray
Write-Host "  type C:\TradingDesk-Local\warehouse\universe_dl_state\universe_dl_progress.json" -ForegroundColor Gray
Write-Host ""
Read-Host "Press Enter to close"
if ($failures.Count -gt 0) { exit 1 }
exit 0
