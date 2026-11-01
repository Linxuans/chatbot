# 把桌面控制台打包成单文件 exe
# 产物: dist\WxAgentGUI.exe —— 必须放在项目根目录（与 .env、.venv 同级）运行
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

$env:TMP = Join-Path $PWD ".tmp"
$env:TEMP = $env:TMP

& .\.venv\Scripts\python.exe -m PyInstaller `
    --noconfirm --clean --onefile --windowed `
    --name WxAgentGUI `
    --paths src `
    --distpath dist `
    --workpath .tmp\pybuild `
    --specpath .tmp `
    gui.pyw

Write-Host ""
Write-Host "构建完成: dist\WxAgentGUI.exe"
Write-Host "请把 exe 复制到项目根目录再运行（它需要找到 .env 和 .venv）。"