@echo off
REM ===========================================================================
REM  run_dashboard.bat - launch the READ-ONLY Trading Desk dashboard.
REM
REM  Binds to 0.0.0.0 so you can reach it from your PHONE on the same Wi-Fi.
REM  This dashboard is read-only: it shows things, it never places/arms/transmits
REM  an order and never changes any config or data.
REM
REM  After it starts, open in a browser:
REM     On this PC:  http://localhost:8501
REM     On phone:    http://<this-PC-LAN-IP>:8501   (e.g. http://192.168.4.20:8501)
REM  Find this PC's LAN IP with:  ipconfig   (look for IPv4 Address)
REM ===========================================================================

set "VENV_PY=C:\TradingDesk-Local\venv\Scripts\python.exe"
set "APP=%~dp0app.py"

"%VENV_PY%" -m streamlit run "%APP%" ^
  --server.address=0.0.0.0 ^
  --server.port=8501 ^
  --server.headless=true ^
  --browser.gatherUsageStats=false

pause
