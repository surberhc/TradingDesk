@echo off
set "OUT=%~dp0RRG_sched_check.txt"
echo ===== schtasks /query /tn "RRG Daily Poll" /v /fo list ===== > "%OUT%"
schtasks /query /tn "RRG Daily Poll" /v /fo list >> "%OUT%" 2>&1
echo. >> "%OUT%"
echo ===== TaskScheduler/Operational events (RRG Daily Poll, last 40) ===== >> "%OUT%"
powershell -NoProfile -Command "Get-WinEvent -FilterHashtable @{LogName='Microsoft-Windows-TaskScheduler/Operational'} -MaxEvents 200 | Where-Object {$_.Message -like '*RRG Daily Poll*'} | Select-Object -First 40 TimeCreated, Id, Message | Format-List" >> "%OUT%" 2>&1
echo. >> "%OUT%"
echo ===== DONE ===== >> "%OUT%"
