@echo off
rem 开机自启用：切到项目根目录，带时间戳启动，输出追加到 logs\bot.out.log
chcp 65001 >nul
cd /d "%~dp0.."
if not exist "logs" mkdir "logs"

echo. >> "logs\bot.out.log"
echo ===== %date% %time% 启动 ===== >> "logs\bot.out.log"

".venv\Scripts\python.exe" run.py --mode embedded >> "logs\bot.out.log" 2>&1

echo ===== %date% %time% 退出，代码 %ERRORLEVEL% ===== >> "logs\bot.out.log"