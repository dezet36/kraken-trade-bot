# Kraken Bot — установка через schtasks.exe (совместимо с любой Windows)
# Запускать от имени Администратора: powershell -ExecutionPolicy Bypass -File install_service.ps1

# ── Проверка прав администратора ───────────────────────────────────────────────
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]"Administrator")
if (-not $isAdmin) {
    Write-Host "ОШИБКА: Запусти PowerShell от имени Администратора!" -ForegroundColor Red
    Write-Host "Закрой это окно, найди PowerShell в меню Пуск, нажми правой кнопкой -> 'Запуск от имени администратора'" -ForegroundColor Yellow
    pause
    exit 1
}

$TaskName = "KrakenBot"
$BotDir   = Split-Path -Parent $MyInvocation.MyCommand.Path

# ── Найти реальный Python ──────────────────────────────────────────────────────
Write-Host "Ищу Python..." -ForegroundColor Cyan
$PythonExe = & python -c "import sys; print(sys.executable)" 2>$null
if (-not $PythonExe -or -not (Test-Path $PythonExe)) {
    $candidates = @(
        "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe",
        "C:\Python313\python.exe",
        "C:\Python312\python.exe",
        "C:\Python311\python.exe"
    )
    foreach ($p in $candidates) {
        if (Test-Path $p) { $PythonExe = $p; break }
    }
}
if (-not $PythonExe -or -not (Test-Path $PythonExe)) {
    Write-Host "ERROR: Python не найден." -ForegroundColor Red
    exit 1
}
Write-Host "Python: $PythonExe" -ForegroundColor Green
Write-Host "Папка бота: $BotDir" -ForegroundColor Green

# ── Установить зависимости ─────────────────────────────────────────────────────
Write-Host "`nУстановка зависимостей..." -ForegroundColor Cyan
& $PythonExe -m pip install -r "$BotDir\requirements.txt"
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: pip install завершился с ошибкой" -ForegroundColor Red
    exit 1
}
Write-Host "Зависимости установлены" -ForegroundColor Green

# ── Создать bat с петлёй автоперезапуска ──────────────────────────────────────
$batPath = "$BotDir\run_bot.bat"
$batContent = @"
@echo off
:loop
cd /d "$BotDir"
echo [%date% %time%] Starting Kraken Bot... >> "$BotDir\service_stdout.log"
"$PythonExe" bot.py >> "$BotDir\service_stdout.log" 2>> "$BotDir\service_stderr.log"
echo [%date% %time%] Bot stopped, restarting in 15s... >> "$BotDir\service_stdout.log"
timeout /t 15 /nobreak > nul
goto loop
"@
Set-Content -Path $batPath -Value $batContent -Encoding ASCII
Write-Host "Лончер создан: $batPath" -ForegroundColor Green

# ── Удалить старое задание ─────────────────────────────────────────────────────
schtasks /delete /tn $TaskName /f 2>$null | Out-Null

# ── Зарегистрировать задание через schtasks.exe ───────────────────────────────
Write-Host "`nРегистрирую задание планировщика..." -ForegroundColor Cyan
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
    Write-Host "`nKraken Bot запущен!" -ForegroundColor Green
} else {
    Write-Host "`nЗадача создана. Статус:" -ForegroundColor Yellow
    schtasks /query /tn $TaskName /fo LIST
}

Write-Host "`n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
Write-Host "Логи:       $BotDir\service_stdout.log"
Write-Host "Ошибки:     $BotDir\service_stderr.log"
Write-Host "`nУправление:"
Write-Host "  Остановить:   schtasks /end /tn KrakenBot"
Write-Host "  Запустить:    schtasks /run /tn KrakenBot"
Write-Host "  Статус:       schtasks /query /tn KrakenBot /fo LIST"
Write-Host "  Удалить:      schtasks /delete /tn KrakenBot /f"
Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
