# Создаёт ярлык приложения на рабочем столе и в меню «Пуск».
#
# Ярлык запускает pythonw.exe — интерпретатор БЕЗ консольного окна, поэтому
# видно только окно программы, как у обычного приложения.
#
# Запуск:  powershell -ExecutionPolicy Bypass -File Live_Bot\install_desktop.ps1
# Удалить: тот же файл с ключом -Remove

param([switch]$Remove)

$ErrorActionPreference = 'Stop'

$botDir   = Split-Path -Parent $MyInvocation.MyCommand.Path
$entry    = Join-Path $botDir 'desktop.py'
$iconPath = Join-Path $botDir 'app_icon.ico'
$name     = 'Kraken — торговый бот'

$desktop   = [Environment]::GetFolderPath('Desktop')
$startMenu = Join-Path ([Environment]::GetFolderPath('StartMenu')) 'Programs'
$targets   = @((Join-Path $desktop "$name.lnk"), (Join-Path $startMenu "$name.lnk"))

if ($Remove) {
    foreach ($t in $targets) {
        if (Test-Path $t) { Remove-Item $t -Force; Write-Host "Удалён: $t" }
    }
    Write-Host "Ярлыки удалены. Сам бот и его данные не тронуты."
    exit 0
}

# pythonw рядом с python — берём тот же интерпретатор, которым запускают бота,
# иначе ярлык уйдёт в другую установку Python без нужных пакетов.
$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $python) { throw "Python не найден в PATH. Установи Python и повтори." }
$pythonw = Join-Path (Split-Path -Parent $python) 'pythonw.exe'
if (-not (Test-Path $pythonw)) {
    Write-Host "pythonw.exe не найден — ярлык будет открывать окно консоли рядом с приложением."
    $pythonw = $python
}

if (-not (Test-Path $entry))    { throw "Не найден $entry" }
if (-not (Test-Path $iconPath)) { throw "Не найден значок $iconPath" }

$shell = New-Object -ComObject WScript.Shell
foreach ($t in $targets) {
    $dir = Split-Path -Parent $t
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force $dir | Out-Null }

    $lnk = $shell.CreateShortcut($t)
    $lnk.TargetPath       = $pythonw
    $lnk.Arguments        = '"' + $entry + '"'
    $lnk.WorkingDirectory = $botDir
    $lnk.IconLocation     = $iconPath
    $lnk.Description      = 'Торговый бот и дашборд сравнения стратегий'
    $lnk.Save()
    Write-Host "Создан: $t"
}

Write-Host ""
Write-Host "Готово. Запускай двойным кликом по значку на рабочем столе."
Write-Host "Режим торговли задаётся в .env (TRADING_MODE=PAPER — фантомные сделки)."
