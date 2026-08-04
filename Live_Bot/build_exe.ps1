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

Write-Host "Собираю Kraken.exe (несколько минут: ccxt, pandas и matplotlib весят много)..."

# --windowed: без консольного окна.
# --add-data: страница дашборда и значок попадают внутрь сборки.
# --hidden-import: ccxt и apscheduler подгружают модули биржи по имени в рантайме,
#   статический анализ PyInstaller их не видит и без явного указания
#   собранная программа падает при первом обращении к бирже.
python -m PyInstaller `
    --noconfirm --clean --onefile --windowed `
    --name Kraken `
    --icon "$botDir\app_icon.ico" `
    --distpath "$dist" `
    --workpath "$root\build" `
    --specpath "$root\build" `
    --add-data "$botDir\dashboard.html;." `
    --add-data "$botDir\app_icon.ico;." `
    --hidden-import ccxt.bybit `
    --hidden-import ccxt.bingx `
    --hidden-import apscheduler.schedulers.blocking `
    --hidden-import apscheduler.executors.pool `
    --hidden-import apscheduler.triggers.interval `
    "$botDir\desktop.py"

if (-not (Test-Path "$dist\Kraken.exe")) { throw "Сборка не создала $dist\Kraken.exe" }

Write-Host ""
Write-Host "Готово: $dist\Kraken.exe"
Write-Host ""
Write-Host "ВАЖНО: положи рядом с Kraken.exe файл .env с настройками —"
Write-Host "данные бота (журнал, состояние, лог) пишутся в папку рядом с .exe."
