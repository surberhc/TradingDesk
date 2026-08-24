@echo off
REM Launch the rebuilt, isolated Trading Desk dashboard on port 8502 (READ-ONLY).
REM Independent of the live app.py on port 8501.
cd /d "%~dp0"
"C:\TradingDesk-Local\venv\Scripts\python.exe" -m streamlit run desk_app.py --server.port 8502 --server.headless true --server.address 127.0.0.1
