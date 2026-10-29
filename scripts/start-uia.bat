@echo off
rem Start the WeChat 4.x UIA gateway.
rem A single-instance mutex makes repeated runs harmless.
cd /d "%~dp0.."
if not exist "logs" mkdir "logs"
echo. >> "logs\bot.out.log"
echo ===== %date% %time% start ===== >> "logs\bot.out.log"
".venv\Scripts\python.exe" run.py --mode uia >> "logs\bot.out.log" 2>&1
echo ===== %date% %time% exit code %ERRORLEVEL% ===== >> "logs\bot.out.log"
