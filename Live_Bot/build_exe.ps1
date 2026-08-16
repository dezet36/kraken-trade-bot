# Сборка одного исполняемого файла Kraken.exe (необязательно).
#
# Ярлык, созданный install_desktop.ps1, уже запускает приложение по иконке и
# ничего собирать не требует. Сборка нужна, только если программу переносят на
# машину без установленного Python.
#
# Запуск:  powershell -ExecutionPolicy Bypass -File Live_Bot\build_exe.ps1
# Нужен:   pip install pyinstaller

$ErrorActionPreference = 'Stop'

$botDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$root   = Split-Path -Parent $botDir
$dist   = Join-Path $root 'dist'

python -c "import PyInstaller" 2>$null
if (-not $?) { throw "PyInstaller не установлен. Выполни: pip install pyinstaller" }

# ── Версия внутрь сборки ──────────────────────────────────────────────────────
# Собранное приложение обновляется, сравнивая свою версию с последним выпуском
# на GitHub. Дата файла для этого не годится: копирование и распаковка её
# меняют. Имя берём из git-тега, а без тега — из короткого хеша с пометкой, что
# сборка внесистемная: такую обновлятор трогать не станет.
$version = $env:KRAKEN_VERSION
if (-not $version) {
    $version = (git -C $root describe --tags --exact-match 2>$null)
}
if (-not $version) {
    $sha = (git -C $root rev-parse --short HEAD 2>$null)
    $version = if ($sha) { "dev-$sha" } else { "" }
}
$versionFile = Join-Path $botDir 'VERSION'
[IO.File]::WriteAllText($versionFile, "$version`n", (New-Object Text.UTF8Encoding $false))
Write-Host "Версия сборки: $version"

Write-Host "Собираю Kraken.exe (несколько минут: ccxt, pandas и matplotlib весят много)..."

# --windowed: без консольного окна.
# --add-data: страница дашборда и значок попадают внутрь сборки.
# --hidden-import: ccxt и apscheduler подгружают модули биржи по имени в рантайме,
#   статический анализ PyInstaller их не видит и без явного указания
#   собранная программа падает при первом обращении к бирже.
#
# СТРОГИЙ РЕЖИМ ОШИБОК НА ВРЕМЯ СБОРКИ СНИМАЕТСЯ, И ЭТО НЕ НЕБРЕЖНОСТЬ.
#
# PyInstaller пишет ход работы в поток ОШИБОК, а не вывода — так устроен он, а
# не мы. При $ErrorActionPreference = 'Stop' PowerShell считает фатальной первую
# же строку «INFO: PyInstaller 6.21.0» и обрывает сборку, не начав её.
#
# Сборка при этом молча оставалась прежней: файл на месте, дата старая. Так и
# вышло — Kraken.exe отстал на 138 коммитов и десять дней, а человек запускал
# его и не понимал, почему нет ни одной правки.
#
# Успех проверяем не отсутствием ошибок, а тем, что положено проверять: кодом
# возврата и наличием файла.
$before = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
python -m PyInstaller `
    --noconfirm --clean --onefile --windowed `
    --name Kraken `
    --icon "$botDir\app_icon.ico" `
    --distpath "$dist" `
    --workpath "$root\build" `
    --specpath "$root\build" `
    --add-data "$botDir\dashboard.html;." `
    --add-data "$botDir\app_icon.ico;." `
    --add-data "$versionFile;." `
    --add-data "$(Join-Path $root '.env.example');." `
    --hidden-import aiohttp `
    --hidden-import polymarket.stream `
    --hidden-import ccxt.bybit `
    --hidden-import ccxt.bingx `
    --hidden-import apscheduler.schedulers.blocking `
    --hidden-import apscheduler.executors.pool `
    --hidden-import apscheduler.triggers.interval `
    --collect-all webview `
    --hidden-import webview.platforms.winforms `
    --hidden-import clr_loader `
    --hidden-import first_run `
    --hidden-import updater_app `
    --hidden-import tkinter `
    --hidden-import tkinter.ttk `
    --exclude-module tkinter.test `
    --exclude-module research `
    "$botDir\desktop.py"

$code = $LASTEXITCODE
$ErrorActionPreference = $before
if ($code -ne 0) { throw "PyInstaller вернул код $code — сборка не удалась" }

if (-not (Test-Path "$dist\Kraken.exe")) { throw "Сборка не создала $dist\Kraken.exe" }

# СВЕЖЕСТЬ ФАЙЛА ПРОВЕРЯЕТСЯ ОТДЕЛЬНО. Прежний Kraken.exe остаётся на месте,
# если сборка не дошла до записи, — и «файл есть» перестаёт быть признаком
# успеха. Ровно так десятидневная сборка выдавала себя за новую.
$built = (Get-Item "$dist\Kraken.exe").LastWriteTime
if ($built -lt (Get-Date).AddMinutes(-30)) {
    throw "Kraken.exe не обновился (файл от $built) — сборка не записала его"
}

Write-Host ""
Write-Host "Готово: $dist\Kraken.exe"
Write-Host ""
Write-Host "ВАЖНО: положи рядом с Kraken.exe файл .env с настройками —"
Write-Host "данные бота (журнал, состояние, лог) пишутся в папку рядом с .exe."
