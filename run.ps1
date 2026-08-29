# Запуск бота. Остановка — Ctrl+C.
#   .\run.ps1            — в консоли, с логом на экране
#   .\run.ps1 -Desktop   — окном приложения с дашбордом
param([switch]$Desktop)

$ErrorActionPreference = 'Stop'
Set-Location -Path $PSScriptRoot

# ОТСУТСТВИЕ venv — НЕ ПОВОД ОТКАЗЫВАТЬСЯ ЗАПУСКАТЬСЯ. Здесь стоял выход с
# «Окружение не создано. Сначала: .\install.ps1», и на машине, где бот прекрасно
# работает системным Python, этот скрипт был просто нерабочим: venv никто не
# создавал, потому что он не нужен.
#
# Берём venv, когда он есть, — в нём точно те пакеты, что ставил install.ps1.
# Нет — идём системным и говорим об этом: если пакетов не окажется, Python
# скажет сам, и это честнее, чем отказ на пороге.
$Venv = Join-Path $PSScriptRoot 'venv'
$py = Join-Path $Venv 'Scripts\python.exe'
$pyw = Join-Path $Venv 'Scripts\pythonw.exe'
if (-not (Test-Path $py)) {
    $sys = (Get-Command python -ErrorAction SilentlyContinue).Source
    if (-not $sys) {
        Write-Host 'Python не найден. Установите его или выполните .\install.ps1' -ForegroundColor Red
        exit 1
    }
    Write-Host 'Окружение venv не создано — запускаю системным Python.' -ForegroundColor Yellow
    $py = $sys
    $pyw = Join-Path (Split-Path -Parent $sys) 'pythonw.exe'
}

$env:BOT_DATA_DIR = Join-Path $PSScriptRoot 'bot_data'

# ── Окном приложения ────────────────────────────────────────────────────────
#
# ЗДЕСЬ ЗАПУСКАЛСЯ python.exe, И ОН ВСЕГДА ОТКРЫВАЕТ КОНСОЛЬ. Ключ -Desktop
# означает «окном приложения», а рядом с окном оставалось чёрное окно
# терминала — закрыть его нельзя, оно же держит процесс. Собранный exe от
# этого свободен: он собирается с --windowed.
#
# pythonw.exe — тот же интерпретатор без консоли. Запускаем отдельным
# процессом и выходим, чтобы и сам этот скрипт не держал окно.
#
# МОЛЧАЛИВЫЙ ОТКАЗ ХУЖЕ КОНСОЛИ, и это главная плата за pythonw: если
# приложение не поднимется, показать ошибку будет некому — ни консоли, ни
# окна. Поэтому stderr уводится в файл, и если процесс умер сразу, мы этот
# файл показываем сами.
if ($Desktop) {
    if (-not (Test-Path $pyw)) { $pyw = $py }   # нет pythonw — пусть с консолью
    $log = Join-Path $env:BOT_DATA_DIR 'startup-error.log'
    New-Item -ItemType Directory -Force -Path $env:BOT_DATA_DIR | Out-Null
    if (Test-Path $log) { Remove-Item $log -Force }

    $app = Start-Process -FilePath $pyw `
        -ArgumentList (Join-Path $PSScriptRoot 'Live_Bot\desktop.py') `
        -WorkingDirectory $PSScriptRoot -RedirectStandardError $log -PassThru

    # Ждём недолго: если упало на импорте или синтаксисе, это случится сразу.
    Start-Sleep -Seconds 3
    if ($app.HasExited) {
        Write-Host "Приложение не запустилось (код $($app.ExitCode))." -ForegroundColor Red
        if ((Test-Path $log) -and (Get-Item $log).Length -gt 0) {
            Write-Host '--- что сказал Python ---' -ForegroundColor Yellow
            Get-Content $log -Tail 25
        } else {
            Write-Host "Подробностей нет. Журнал: $env:BOT_DATA_DIR" -ForegroundColor Yellow
        }
        exit 1
    }
    Write-Host "Приложение запущено (PID $($app.Id)). Это окно можно закрыть." -ForegroundColor Green
    exit 0
}

# ── В консоли, с логом на экране ────────────────────────────────────────────
# Здесь консоль и есть смысл запуска, поэтому python.exe остаётся.
& $py (Join-Path $PSScriptRoot 'Live_Bot\bot.py')
