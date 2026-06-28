# =============================================================================
#  launch_dashboard.ps1 - one double-click launcher for the READ-ONLY dashboard.
#
#  Behavior:
#    1. If something is ALREADY serving on port 8501, just open the browser and
#       exit (do NOT start a second Streamlit server - it would grab a different
#       port or error).
#    2. Otherwise start the Streamlit server HIDDEN in the background (no console
#       window), poll port 8501 until it accepts connections (cap ~30s), then
#       open the default browser. If it never comes up, open the browser anyway
#       and exit (never hang).
#
#  Launch pattern matches C:\TradingDesk-Local\warehouse\run_spxw_1m.bat:
#  the venv's Scripts\python(w).exe is a RELAUNCHER STUB that spawns a child
#  (duplicate process). So we call the BASE interpreter directly and put the
#  venv site-packages on PYTHONPATH. pythonw.exe = no black console window.
#  Result: exactly ONE streamlit server process.
# =============================================================================

$ErrorActionPreference = 'Stop'

$Url       = 'http://localhost:8501'
$Port      = 8501
$BasePyw   = 'C:\Users\andre\AppData\Local\Programs\Python\Python312\pythonw.exe'
$VenvSite  = 'C:\TradingDesk-Local\venv\Lib\site-packages'
$AppDir    = Split-Path -Parent $MyInvocation.MyCommand.Path
$AppPy     = Join-Path $AppDir 'app.py'

# Already serving? Just open the browser and bail.
if (Test-NetConnection -ComputerName localhost -Port $Port -InformationLevel Quiet -WarningAction SilentlyContinue) {
    Start-Process $Url
    return
}

# Not serving: launch the server hidden, using the base interpreter + venv path.
$env:PYTHONPATH = "$VenvSite;$env:PYTHONPATH"

$argList = @(
    '-m', 'streamlit', 'run', "`"$AppPy`"",
    '--server.address=0.0.0.0',
    '--server.port=8501',
    '--server.headless=true',
    '--browser.gatherUsageStats=false'
)

Start-Process -FilePath $BasePyw -ArgumentList $argList -WorkingDirectory $AppDir -WindowStyle Hidden | Out-Null

# Poll until the port accepts connections, cap ~30s.
$deadline = (Get-Date).AddSeconds(30)
do {
    Start-Sleep -Milliseconds 750
    $up = Test-NetConnection -ComputerName localhost -Port $Port -InformationLevel Quiet -WarningAction SilentlyContinue
} while (-not $up -and (Get-Date) -lt $deadline)

# Open the browser regardless (don't hang if it never came up).
Start-Process $Url
