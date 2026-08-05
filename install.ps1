# Установка бота на Windows. Запуск: правой кнопкой -> «Выполнить с помощью PowerShell»
# либо в консоли:  powershell -ExecutionPolicy Bypass -File install.ps1
#
# Скрипт идемпотентный: повторный запуск ничего не ломает и не перезаписывает
# ни .env, ни журнал сделок.

$ErrorActionPreference = 'Stop'
Set-Location -Path $PSScriptRoot

$AppDir  = $PSScriptRoot
$DataDir = Join-Path $AppDir 'bot_data'
$Venv    = Join-Path $AppDir 'venv'

function Say  ($t) { Write-Host "`n$t" -ForegroundColor White }
function Ok   ($t) { Write-Host "  OK    $t" -ForegroundColor Green }
function Warn ($t) { Write-Host "  ВНИМ  $t" -ForegroundColor Yellow }
function Die  ($t) { Write-Host "  ОШИБКА $t" -ForegroundColor Red; exit 1 }

Say '1. Python'
$py = $null
foreach ($c in @('python', 'python3', 'py')) {
    $cmd = Get-Command $c -ErrorAction SilentlyContinue
    if (-not $cmd) { continue }
    $v = & $c -c 'import sys; print(1 if sys.version_info >= (3,10) else 0)' 2>$null
    if ($v -eq '1') { $py = $c; break }
}
if (-not $py) { Die 'нужен Python 3.10 или новее — python.org/downloads (галочка "Add to PATH")' }
Ok (& $py --version)

Say '2. Виртуальное окружение'
if (-not (Test-Path $Venv)) {
    & $py -m venv $Venv
    if (-not $?) { Die 'не удалось создать venv' }
    Ok "создано: $Venv"
} else {
    Ok "уже есть: $Venv"
}
$VenvPy = Join-Path $Venv 'Scripts\python.exe'

Say '3. Зависимости'
& $VenvPy -m pip install --quiet --upgrade pip
& $VenvPy -m pip install --quiet -r (Join-Path $AppDir 'requirements.txt')
if (-not $?) { Die 'не установились зависимости' }
Ok 'установлены из requirements.txt'

Say '4. Каталог данных'
# Данные лежат ОТДЕЛЬНО от кода: журнал сделок, состояние позиций, ключи и
# настройки оператора. Папку с кодом можно перезаписывать целиком —
# bot_data это не заденет.
if (-not (Test-Path $DataDir)) { New-Item -ItemType Directory -Path $DataDir | Out-Null }
Ok $DataDir

Say '5. Настройки'
$EnvFile = Join-Path $DataDir '.env'
$NeedKeys = $false
if (-not (Test-Path $EnvFile)) {
    Copy-Item (Join-Path $AppDir '.env.example') $EnvFile
    Warn "создан $EnvFile из шаблона — ВПИШИТЕ КЛЮЧИ БИРЖИ"
    Warn 'потом запустите install.ps1 ещё раз для проверки'
    $NeedKeys = $true
} else {
    Ok '.env на месте (не тронут)'
}

Say '6. Проверка готовности'
if ($NeedKeys) {
    Warn 'пропущена: сначала заполните .env'
    Write-Host "`nДальше: отредактируйте $EnvFile, затем запустите install.ps1"
    exit 0
}

$env:BOT_DATA_DIR = $DataDir
& $VenvPy (Join-Path $AppDir 'Live_Bot\doctor.py')
if ($LASTEXITCODE -ne 0) { Die 'проверка не пройдена — см. список выше' }

Say 'Готово'
Write-Host @"
  Запуск в окне:            .\run.ps1
  Запуск с интерфейсом:     .\run.ps1 -Desktop
  Запуск службой Windows:   .\Live_Bot\install_service.ps1
  Дашборд:                  http://localhost:8787

  Данные:  $DataDir   (обновление кода их не трогает)
"@
