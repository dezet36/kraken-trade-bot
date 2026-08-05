# Запуск бота. Остановка — Ctrl+C.
#   .\run.ps1            — в консоли, с логом на экране
#   .\run.ps1 -Desktop   — окном приложения с дашбордом
param([switch]$Desktop)

$ErrorActionPreference = 'Stop'
Set-Location -Path $PSScriptRoot

$Venv = Join-Path $PSScriptRoot 'venv'
if (-not (Test-Path $Venv)) {
    Write-Host 'Окружение не создано. Сначала: .\install.ps1' -ForegroundColor Red
    exit 1
}

$env:BOT_DATA_DIR = Join-Path $PSScriptRoot 'bot_data'
$py = Join-Path $Venv 'Scripts\python.exe'
$entry = if ($Desktop) { 'Live_Bot\desktop.py' } else { 'Live_Bot\bot.py' }
& $py (Join-Path $PSScriptRoot $entry)
