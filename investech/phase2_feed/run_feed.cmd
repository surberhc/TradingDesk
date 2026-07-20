@echo off
cd /d "%~dp0"
if not exist logs mkdir logs
"C:\Python314\python.exe" main.py >> "logs\feed.log" 2>&1
