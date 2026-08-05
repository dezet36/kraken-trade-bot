# Kraken Bot — установка через schtasks.exe (совместимо с любой Windows, без Docker)
# Запускать от имени Администратора:
#   powershell -ExecutionPolicy Bypass -File install_service.ps1 -Script bot.py
#   powershell -ExecutionPolicy Bypass -File install_service.ps1 -Script platform_bot.py
#
# Ставит venv и BOT_DATA_DIR ОДНИМ УРОВНЕМ ВЫШЕ Live_Bot (рядом с ней) — так папку
# Live_Bot можно перезаписывать целиком при обновлении кода, ничего не теряя:
# venv/ и bot_data/ физически отдельные каталоги, которых копирование не коснётся.

param(
    [ValidateSet("bot.py", "platform_bot.py")]
    [string]$Script = "bot.py"
)

# ── Проверка прав администратора ───────────────────────────────────────────────
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]"Administrator")
if (-not $isAdmin) {
    Write-Host "ОШИБКА: Запусти PowerShell от имени Администратора!" -ForegroundColor Red
    Write-Host "Закрой это окно, найди PowerShell в меню Пуск, нажми правой кнопкой -> 'Запуск от имени администратора'" -ForegroundColor Yellow
    pause
    exit 1
}

$TaskName  = if ($Script -eq "platform_bot.py") { "KrakenPlatformBot" } else { "KrakenBot" }
$BotDir    = Split-Path -Parent $MyInvocation.MyCommand.Path   # ...\Live_Bot
$RootDir   = Split-Path -Parent $BotDir                        # на уровень выше Live_Bot
$VenvDir   = Join-Path $RootDir "venv"
$DataDir   = Join-Path $RootDir "bot_data"
$ReqFile   = Join-Path $RootDir "requirements.txt"

Write-Host "Папка бота:   $BotDir" -ForegroundColor Green
Write-Host "venv:         $VenvDir" -ForegroundColor Green
Write-Host "Данные:       $DataDir" -ForegroundColor Green
Write-Host "Запуск:       $Script" -ForegroundColor Green

# ── Найти системный Python (для создания venv) ────────────────────────────────
Write-Host "`nИщу Python..." -ForegroundColor Cyan
$SystemPython = & python -c "import sys; print(sys.executable)" 2>$null
if (-not $SystemPython -or -not (Test-Path $SystemPython)) {
    $candidates = @(
        "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe",
        "C:\Python313\python.exe",
        "C:\Python312\python.exe",
        "C:\Python311\python.exe"
    )
    foreach ($p in $candidates) {
        if (Test-Path $p) { $SystemPython = $p; break }
    }
}
if (-not $SystemPython -or -not (Test-Path $SystemPython)) {
    Write-Host "ERROR: Python не найден." -ForegroundColor Red
    exit 1
}
Write-Host "Python: $SystemPython" -ForegroundColor Green

# ── venv + зависимости (изолированно от других ботов на сервере) ──────────────
if (-not (Test-Path $VenvDir)) {
    Write-Host "`nСоздаю venv..." -ForegroundColor Cyan
    & $SystemPython -m venv $VenvDir
}
$PythonExe = Join-Path $VenvDir "Scripts\python.exe"

Write-Host "`nУстановка зависимостей..." -ForegroundColor Cyan
& $PythonExe -m pip install -r $ReqFile
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: pip install завершился с ошибкой" -ForegroundColor Red
    exit 1
}
Write-Host "Зависимости установлены" -ForegroundColor Green

# ── Данные: создать bot_data/, перенести .env из Live_Bot если он там ─────────
if (-not (Test-Path $DataDir)) {
    New-Item -ItemType Directory -Path $DataDir | Out-Null
}
$OldEnv = Join-Path $BotDir ".env"
$NewEnv = Join-Path $DataDir ".env"
if ((Test-Path $OldEnv) -and -not (Test-Path $NewEnv)) {
    Write-Host "Переношу .env в bot_data\ (разово)..." -ForegroundColor Cyan
    Move-Item $OldEnv $NewEnv
}
if (-not (Test-Path $NewEnv)) {
    Write-Host "ВНИМАНИЕ: нет $NewEnv — положи туда .env с ключами перед запуском." -ForegroundColor Yellow
}

# ── BOT_DATA_DIR — на уровне машины, чтобы видел и ручной запуск, и задание ───
[Environment]::SetEnvironmentVariable("BOT_DATA_DIR", $DataDir, "Machine")
Write-Host "BOT_DATA_DIR (машинный) = $DataDir" -ForegroundColor Green

# ── Создать bat с петлёй автоперезапуска ──────────────────────────────────────
$batPath = Join-Path $BotDir "run_bot.bat"
$logDir  = $DataDir
$batContent = @"
@echo off
set BOT_DATA_DIR=$DataDir
:loop
cd /d "$BotDir"
echo [%date% %time%] Starting $Script... >> "$logDir\service_stdout.log"
"$PythonExe" $Script >> "$logDir\service_stdout.log" 2>> "$logDir\service_stderr.log"
echo [%date% %time%] Bot stopped, restarting in 15s... >> "$logDir\service_stdout.log"
timeout /t 15 /nobreak > nul
goto loop
"@
Set-Content -Path $batPath -Value $batContent -Encoding ASCII
Write-Host "Лончер создан: $batPath" -ForegroundColor Green

# ── Удалить старое задание ─────────────────────────────────────────────────────
schtasks /delete /tn $TaskName /f 2>$null | Out-Null

# ── Зарегистрировать задание через schtasks.exe ───────────────────────────────
Write-Host "`nРегистрирую задание планировщика ($TaskName)..." -ForegroundColor Cyan
$currentUser = "$env:USERDOMAIN\$env:USERNAME"
schtasks /create /tn $TaskName /tr "`"$batPath`"" /sc ONSTART /ru $currentUser /it /f

if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: не удалось зарегистрировать задание" -ForegroundColor Red
    exit 1
}
Write-Host "Задание зарегистрировано" -ForegroundColor Green

# ── Запустить прямо сейчас ─────────────────────────────────────────────────────
Write-Host "`nЗапускаю бота..." -ForegroundColor Cyan
schtasks /run /tn $TaskName

Start-Sleep -Seconds 5
$result = schtasks /query /tn $TaskName /fo LIST 2>$null
if ($result -match "Running|Выполняется") {
    Write-Host "`n$Script запущен ($TaskName)!" -ForegroundColor Green
} else {
    Write-Host "`nЗадача создана. Статус:" -ForegroundColor Yellow
    schtasks /query /tn $TaskName /fo LIST
}

Write-Host "`n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
Write-Host "Данные:     $DataDir"
Write-Host "Логи:       $logDir\service_stdout.log"
Write-Host "Ошибки:     $logDir\service_stderr.log"
Write-Host "`nУправление:"
Write-Host "  Остановить:   schtasks /end /tn $TaskName"
Write-Host "  Запустить:    schtasks /run /tn $TaskName"
Write-Host "  Статус:       schtasks /query /tn $TaskName /fo LIST"
Write-Host "  Удалить:      schtasks /delete /tn $TaskName /f"
Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
