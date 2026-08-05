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

function Test-Interactive {
    # Спрашивать можно только у живой консоли. Под планировщиком задач или в
    # конвейере Read-Host не вернёт ничего и установка встанет насмерть —
    # молча, потому что вопрос никто не увидит.
    return [Environment]::UserInteractive -and -not [Console]::IsInputRedirected
}

function Ask-Choice ($title, $options, $default) {
    while ($true) {
        $answer = Read-Host "  $title [$($options -join '/')], по умолчанию $default"
        if (-not $answer) { return $default }
        $match = $options | Where-Object { $_ -ieq $answer }
        if ($match) { return $match }
        Write-Host "    надо одно из: $($options -join ', ')" -ForegroundColor Yellow
    }
}

function Ask-Secret ($title) {
    while ($true) {
        $secure = Read-Host "  $title" -AsSecureString
        $value = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
            [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
        # Ключ, скопированный из браузера, часто приезжает с пробелом на конце,
        # и биржа отвечает «неверная подпись» — искать причину потом долго.
        $value = ($value -replace '^\s+|\s+$', '')
        if ($value) { return $value }
        Write-Host '    пусто — введите значение' -ForegroundColor Yellow
    }
}

function Set-EnvValue ($path, $key, $value) {
    # Значение подставляется в существующую строку, а не дописывается в конец:
    # иначе в файле окажутся два TRADING_MODE, и какой из них подействует —
    # вопрос порядка чтения, а не намерения.
    $lines = [IO.File]::ReadAllLines($path)
    $done = $false
    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($lines[$i] -match "^\s*$([regex]::Escape($key))\s*=") {
            $lines[$i] = "$key=$value"
            $done = $true
        }
    }
    if (-not $done) { $lines += "$key=$value" }
    [IO.File]::WriteAllLines($path, $lines, (New-Object Text.UTF8Encoding $false))
}

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
    # Спрашиваем ключи здесь же. Отправлять человека править .env в блокноте —
    # это лишний шаг, на котором проще всего ошибиться: не тот файл, лишние
    # пробелы, кавычки вокруг ключа. Запишем сами.
    if (Test-Interactive) {
        Write-Host ''
        Write-Host '  Нужны ключи биржи. Даже в режиме фантома: котировки берутся с биржи.'
        Write-Host '  Для PAPER и DEMO подойдут ключи демо-счёта.'
        Write-Host ''

        $exchange = Ask-Choice 'Биржа' @('bybit', 'bingx') 'bybit'
        $mode = Ask-Choice 'Режим' @('PAPER', 'DEMO', 'LIVE') 'PAPER'
        if ($mode -eq 'LIVE') {
            Warn 'LIVE — реальные деньги. Бот дополнительно переспросит при запуске.'
        }

        $prefix = $exchange.ToUpper()
        $apiKey = Ask-Secret "$prefix API key"
        $apiSecret = Ask-Secret "$prefix secret key"

        Set-EnvValue $EnvFile 'EXCHANGE' $exchange
        Set-EnvValue $EnvFile 'TRADING_MODE' $mode
        Set-EnvValue $EnvFile "${prefix}_API_KEY" $apiKey
        Set-EnvValue $EnvFile "${prefix}_SECRET_KEY" $apiSecret
        Ok "записаны в $EnvFile"
    } else {
        Warn "создан $EnvFile из шаблона — ВПИШИТЕ КЛЮЧИ БИРЖИ"
        Warn 'потом запустите install.ps1 ещё раз для проверки'
        $NeedKeys = $true
    }
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

# Проверка пройдена — предложить сразу и запустить. Иначе установка кончается
# списком команд, которые надо где-то набрать, а человек просил обратного.
if (Test-Interactive) {
    Write-Host ''
    $how = Ask-Choice 'Запустить сейчас' @('служба', 'окно', 'нет') 'служба'
    if ($how -eq 'служба') {
        # Служба переживает перезагрузку сервера и поднимает бота после падения.
        & powershell -NoProfile -ExecutionPolicy Bypass `
            -File (Join-Path $AppDir 'Live_Bot\install_service.ps1')
    } elseif ($how -eq 'окно') {
        & powershell -NoProfile -ExecutionPolicy Bypass `
            -File (Join-Path $AppDir 'run.ps1')
    }
}
