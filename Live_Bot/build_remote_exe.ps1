# Сборка KrakenRemote.exe — окна панели, подключённого к боту на сервере.
#
# Отличается от build_exe.ps1 тем, чего здесь НЕТ. В эту сборку не входят ни
# сам бот, ни ccxt, ни apscheduler, ни страница панели: страницу отдаёт сервер,
# а торгует он же. Программа состоит из туннеля и окна.
#
# Из-за этого файл получается в несколько раз меньше основного, а обновлять его
# приходится редко: меняется панель на сервере, а не окно на столе.
#
# Запуск:  powershell -ExecutionPolicy Bypass -File Live_Bot\build_remote_exe.ps1
# Нужен:   pip install pyinstaller pywebview

$ErrorActionPreference = 'Stop'

$botDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$root   = Split-Path -Parent $botDir
$dist   = Join-Path $root 'dist'

python -c "import PyInstaller" 2>$null
if (-not $?) { throw "PyInstaller не установлен. Выполни: pip install pyinstaller" }

# ЗАПУЩЕННОЕ ПРИЛОЖЕНИЕ ДЕРЖИТ СВОЙ ФАЙЛ, и сборка не сможет его переписать.
# Windows не даёт заменить работающий .exe: PyInstaller доходит до последнего
# шага, упирается в блокировку и возвращает код 1 — а причина теряется среди
# сотен строк INFO. Отказ должен приходить сразу и называть причину.
$running = @(Get-Process KrakenRemote -ErrorAction SilentlyContinue)
if ($running.Count -gt 0) {
    $ids = ($running | ForEach-Object { $_.Id }) -join ', '
    throw ("KrakenRemote.exe запущен (pid $ids) и держит свой файл — сборка " +
           "не сможет его переписать. Закрой окно и повтори.")
}

Push-Location $botDir
try {
    # --windowed: без консольного окна. У программы есть своё окно, а консоль
    #   рядом с ним выглядит поломкой и на Windows остаётся висеть пустой.
    #
    # --hidden-import: pywebview подгружает движок окна по имени в рантайме,
    #   PyInstaller такого не видит. Без winforms и clr собранная программа
    #   запускается и молча не показывает окна — самый неприятный вид отказа.
    #
    # tkinter нужен окну настроек: панель нарисовать их не может, она на
    #   сервере, к которому мы ещё не подключились.
    #
    # СТРАНИЦЫ ПАНЕЛИ ЗДЕСЬ НЕТ НАМЕРЕННО. Она приходит с сервера, и вшивать
    # её копию в сборку значило бы показывать старую разметку поверх свежих
    # данных после каждого обновления бота — расхождение, которое ничем себя
    # не выдаёт.
    # СТРОГИЙ РЕЖИМ ОШИБОК НА ВРЕМЯ СБОРКИ СНИМАЕТСЯ, И ЭТО НЕ НЕБРЕЖНОСТЬ.
    #
    # PyInstaller пишет ход работы в поток ОШИБОК, а не вывода. При
    # $ErrorActionPreference = 'Stop' PowerShell считает фатальной первую же
    # строку «INFO: PyInstaller 6.21.0» и обрывает сборку, не начав её.
    #
    # Ровно так и вышло при первом запуске этого файла: приём был давно найден
    # в build_exe.ps1, а сюда его не перенесли. Сборка падала, ничего не
    # объясняя, и выглядело это как поломка PyInstaller.
    #
    # Успех проверяем не отсутствием ошибок, а кодом возврата и наличием файла.
    $before = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'

    pyinstaller `
        --noconfirm --clean --onefile --windowed `
        --name KrakenRemote `
        --icon "$botDir\app_icon.ico" `
        --distpath "$dist" `
        --workpath "$root\build" `
        --specpath "$root\build" `
        --add-data "$botDir\app_icon.ico;." `
        --hidden-import webview.platforms.winforms `
        --hidden-import clr_loader `
        --hidden-import clr `
        --hidden-import tkinter `
        --hidden-import tkinter.ttk `
        --hidden-import tkinter.filedialog `
        --hidden-import tkinter.messagebox `
        --hidden-import remote `
        remote_app.py

    $code = $LASTEXITCODE
    $ErrorActionPreference = $before
    if ($code -ne 0) { throw "PyInstaller вернул код $code — сборка не удалась" }
}
finally {
    Pop-Location
}

$exe = Join-Path $dist 'KrakenRemote.exe'
if (-not (Test-Path $exe)) { throw "Сборка не создала $exe" }

$size = [math]::Round((Get-Item $exe).Length / 1MB, 1)
Write-Host ""
Write-Host "Готово: $exe ($size МБ)" -ForegroundColor Green
Write-Host ""
Write-Host "Настройки соединения программа спросит при первом запуске и" -ForegroundColor Gray
Write-Host "сохранит рядом с собой в remote.json. Пароли там не хранятся:" -ForegroundColor Gray
Write-Host "подключение идёт по ключу SSH." -ForegroundColor Gray
