# Один файл, с которого начинается установка на Windows-сервер.
#
# Скачивает актуальный код с GitHub, ставит окружение и зависимости, заводит
# каталог данных и прогоняет проверку готовности. Больше ничего копировать на
# сервер не нужно — только этот файл.
#
# ПОЧЕМУ ОТДЕЛЬНО ОТ install.ps1. install.ps1 ставит УЖЕ скопированную папку:
# он умеет создать окружение и проверить настройки, но взять код ему неоткуда.
# Здесь наоборот: кода на сервере ещё нет, и первый шаг — принести его с
# GitHub. Дальше управление передаётся install.ps1, чтобы не держать два
# установщика, расходящихся со временем.
#
# ПОВТОРНЫЙ ЗАПУСК БЕЗОПАСЕН. Если папка уже репозиторий — код обновляется, а
# каталог данных не трогается вовсе: bot_data лежит в .gitignore, git его не
# видит.
#
# Запуск:
#     powershell -ExecutionPolicy Bypass -File bootstrap.ps1
#     powershell -ExecutionPolicy Bypass -File bootstrap.ps1 -Dir D:\kraken-bot

param(
    [string]$Dir = "C:\kraken-bot",
    [string]$Branch = "main"
)

$ErrorActionPreference = 'Stop'
$RepoUrl = 'https://github.com/dezet36/kraken-trade-bot.git'

function Ok($m)   { Write-Host "  [ok] $m"   -ForegroundColor Green }
function Info($m) { Write-Host "  $m"        -ForegroundColor DarkGray }
function Die($m)  { Write-Host "  [ОШИБКА] $m" -ForegroundColor Red; exit 1 }

# Запуск git, который НЕ роняет скрипт.
#
# Windows PowerShell заворачивает каждую строку stderr внешней программы в
# ошибку, а при $ErrorActionPreference='Stop' первая же такая строка обрывает
# выполнение. Из-за этого проверка «а доступен ли репозиторий без токена»
# вместо ответа «нет» убивала установку — до вопроса про токен дело не
# доходило. Здесь stderr просто собирается в текст, а решение принимается по
# коду возврата, как и положено.
function Try-Git ([string[]] $Arguments) {
    # git запускается ЧЕРЕЗ cmd, а не напрямую. Причина в том, как Windows
    # PowerShell обращается со stderr внешней программы: он заворачивает каждую
    # строку в объект ошибки, и дальше выбор только плохой. При 'Continue' он
    # печатает их красным сам — обычное «нет доступа к репозиторию» выглядит
    # падением скрипта на полэкрана. При 'SilentlyContinue' выбрасывает вместе
    # с текстом, и причина отказа теряется. Перенаправление 2>файл от этого не
    # спасает: строки всё равно проходят через тот же механизм.
    #
    # cmd перенаправляет потоки сам, до PowerShell. В файлах оказывается ровно
    # то, что напечатал git, на экране — ничего.
    $outFile = [IO.Path]::GetTempFileName()
    $errFile = [IO.Path]::GetTempFileName()
    try {
        $quoted = ($Arguments | ForEach-Object { '"' + $_ + '"' }) -join ' '
        cmd /c "git $quoted > `"$outFile`" 2> `"$errFile`""
        $code = $LASTEXITCODE
        $text = ([IO.File]::ReadAllText($outFile) + [IO.File]::ReadAllText($errFile)).Trim()
        return @{ Code = $code; Out = $text }
    } finally {
        Remove-Item $outFile, $errFile -Force -ErrorAction SilentlyContinue
    }
}

Write-Host ""
Write-Host "-- Установка торгового бота --------------------------------------------"
Write-Host "   репозиторий: $RepoUrl ($Branch)"
Write-Host "   каталог:     $Dir"
Write-Host ""

# -- Что должно быть на сервере ---------------------------------------------
$git = Get-Command git -ErrorAction SilentlyContinue
if (-not $git) { Die "нужен git. Скачать: https://git-scm.com/download/win" }
Ok (git --version)

$py = $null
foreach ($c in @('python3.13', 'python3.12', 'python3.11', 'python3.10', 'python', 'py')) {
    $cmd = Get-Command $c -ErrorAction SilentlyContinue
    if (-not $cmd) { continue }
    & $c -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>$null
    if ($LASTEXITCODE -eq 0) { $py = $c; break }
}
if (-not $py) { Die "нужен Python 3.10 или новее. Скачать: https://www.python.org/downloads/" }
Ok (& $py --version)

$parent = Split-Path -Parent $Dir
if (-not (Test-Path $parent)) {
    try { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
    catch { Die "нет доступа к $parent — запустите от администратора или укажите другой каталог" }
}

# -- Доступ к репозиторию ----------------------------------------------------
# Репозиторий закрытый, нужен токен. Сначала пробуем без него: если репозиторий
# когда-нибудь откроют, лишний вопрос будет только мешать.
$credFile = Join-Path $Dir 'bot_data\.git-credentials'
$credGit  = $credFile -replace '\\', '/'
$needToken = $true

$env:GIT_TERMINAL_PROMPT = '0'

# Пустой credential.helper сбрасывает список унаследованных — и он стоит
# ВСЕГДА, даже когда своего токена ещё нет.
#
# Иначе на Windows в дело вмешивается диспетчер учётных данных: он отвечает
# первым, лезет в браузер за подтверждением и отдаёт доступ того аккаунта,
# который там залогинен. Если у этого аккаунта прав на репозиторий нет, git
# получает отказ — но отказ приходит уже ПОСЛЕ успешной аутентификации,
# поэтому выглядит как «Write access to repository not granted», то есть как
# проблема прав, а не как «взят не тот ключ». Хуже того, проверка «а доступен
# ли репозиторий вообще без токена» с таким помощником отвечает «доступен» —
# и установка идёт клонировать то, что склонировать не сможет.
#
# На сервере всплывающее окно браузера — отдельная беда: там его некому
# закрыть. Сброшенный список решает и это.
function Auth-Args ($file) {
    # Имя намеренно не $args: так зовётся автоматическая переменная PowerShell.
    $flags = @('-c', 'credential.helper=')
    if ($file -and (Test-Path $file)) {
        $asGit = $file -replace '\\', '/'
        $flags += @('-c', "credential.helper=store --file=$asGit")
    }
    return $flags
}

# Проверка «репозиторий открытый?» — строго без каких-либо учётных данных.
if ((Try-Git ((Auth-Args $null) + @('ls-remote', $RepoUrl, 'HEAD'))).Code -eq 0) {
    $needToken = $false
    Ok "репозиторий доступен без токена"
} elseif (Test-Path $credFile) {
    if ((Try-Git ((Auth-Args $credFile) + @('ls-remote', $RepoUrl, 'HEAD'))).Code -eq 0) {
        $needToken = $false
        Ok "токен уже сохранён с прошлой установки"
    }
}

if ($needToken) {
    # Спрашивать можно только у живой консоли. Без неё Read-Host не вернёт
    # ничего и установка повиснет насмерть — молча, потому что вопрос никто
    # не увидит. Лучше внятный отказ.
    if (-not ([Environment]::UserInteractive) -or [Console]::IsInputRedirected) {
        Die ("нужен токен GitHub, но спросить некого — нет консоли. " +
             "Запустите bootstrap.bat двойным щелчком либо положите токен в $credFile " +
             "строкой вида https://x-access-token:ТОКЕН@github.com")
    }
    Write-Host ""
    Write-Host "  Репозиторий закрытый — нужен токен доступа GitHub."
    Write-Host ""
    Write-Host "  Где взять: github.com -> Settings -> Developer settings ->"
    Write-Host "             Personal access tokens -> Fine-grained tokens -> Generate"
    Write-Host ""
    Write-Host "  ВАЖНО, здесь ошибаются чаще всего:" -ForegroundColor Yellow
    Write-Host "    Repository access -> Only select repositories -> kraken-trade-bot"
    Write-Host "    Значение по умолчанию (Public Repositories) НЕ ПОДОЙДЁТ:"
    Write-Host "    репозиторий закрытый, и такой токен его просто не увидит."
    Write-Host ""
    Write-Host "    Permissions -> Repository permissions -> Contents -> Read-only"
    Write-Host ""
    Write-Host "  Токен сохранится в $credFile и понадобится ещё раз при"
    Write-Host "  обновлениях — вводить его каждый раз не придётся."
    Write-Host ""
    $secure = Read-Host "  Токен" -AsSecureString
    $token = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
        [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
    if (-not $token) { Die "токен не введён" }

    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $credFile) | Out-Null
    # Без BOM: git читает файл построчно и на BOM спотыкается.
    [IO.File]::WriteAllText($credFile, "https://x-access-token:$token@github.com`n",
                            (New-Object Text.UTF8Encoding $false))
    # Доступ только владельцу: в файле лежит рабочий ключ к репозиторию.
    # Правило собирается отдельной строкой: в аргументе вида «имя:(R,W)»
    # PowerShell спотыкается о скобки прямо в командной строке.
    $rule = '{0}:(R,W)' -f $env:USERNAME
    & icacls $credFile /inheritance:r /grant:r $rule | Out-Null

    $check = Try-Git ((Auth-Args $credFile) + @('ls-remote', $RepoUrl, 'HEAD'))
    if ($check.Code -ne 0) {
        Remove-Item $credFile -Force -ErrorAction SilentlyContinue
        Write-Host ""
        Write-Host "  Ответ GitHub: $($check.Out.Trim())" -ForegroundColor DarkGray
        Write-Host ""
        # Ответ «403 / Write access not granted» сбивает с толку: звучит как
        # нехватка прав на запись, хотя мы всего лишь читаем. На деле GitHub так
        # отвечает, когда токен опознан, но ЭТОТ репозиторий в его список не
        # входит — то есть выбран не тот Repository access.
        if ($check.Out -match '403|not granted|not found') {
            Die ("токен опознан, но kraken-trade-bot ему не виден. " +
                 "В настройках токена Repository access должен быть " +
                 "'Only select repositories' с выбранным kraken-trade-bot — " +
                 "значение по умолчанию 'Public Repositories' закрытый репозиторий не покажет")
        }
        Die "токен не подошёл"
    }
    Ok "токен принят"
}

$auth = Auth-Args $credFile

# -- Код ---------------------------------------------------------------------
# Клонируем целиком, без разреженной выкладки: весь репозиторий — пара
# мегабайт, а список файлов пришлось бы держать в двух местах. Стоит добавить
# файл в корень и забыть про список — и на сервере его молча не окажется,
# причём видно это станет не при установке, а при первом запуске.
if (Test-Path (Join-Path $Dir '.git')) {
    Info "папка уже существует — обновляю код"
    $r = Try-Git ($auth + @('-C', $Dir, 'fetch', '--quiet', 'origin', $Branch))
    if ($r.Code -ne 0) { Write-Host $r.Out -ForegroundColor DarkGray
                         Die "не удалось получить обновления" }
    # Только перемотка вперёд: локальные правки на сервере молча затирать нельзя.
    $r = Try-Git @('-C', $Dir, 'merge', '--ff-only', "origin/$Branch", '--quiet')
    if ($r.Code -ne 0) {
        Die "на сервере есть локальные изменения кода. Уберите их (git -C $Dir status) и запустите снова"
    }
    Ok "код обновлён до $((Try-Git @('-C', $Dir, 'rev-parse', '--short', 'HEAD')).Out.Trim())"
} else {
    $junk = @()
    if (Test-Path $Dir) {
        $junk = Get-ChildItem -Force $Dir | Where-Object { $_.Name -ne 'bot_data' }
    }
    if ($junk.Count -gt 0) {
        Die "$Dir не пуст и не является репозиторием. Укажите другой каталог или уберите этот"
    }
    Info "скачиваю код с GitHub..."
    # bot_data мог быть создан выше ради токена — в непустую папку git не
    # клонирует, поэтому клонируем рядом и переносим.
    $tmp = Join-Path ([IO.Path]::GetTempPath()) ("kraken-" + [Guid]::NewGuid().ToString('N'))
    $r = Try-Git ($auth + @('clone', '--quiet', '--branch', $Branch, $RepoUrl, $tmp))
    if ($r.Code -ne 0) { Write-Host $r.Out -ForegroundColor DarkGray
                         Die "не удалось склонировать репозиторий" }
    New-Item -ItemType Directory -Force -Path $Dir | Out-Null
    Get-ChildItem -Force $tmp | Move-Item -Destination $Dir -Force
    Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
    Ok "код скачан, версия $((Try-Git @('-C', $Dir, 'rev-parse', '--short', 'HEAD')).Out.Trim())"
}

# Токен нужен и кнопке «Обновить» на дашборде: она делает обычный git fetch из
# того же каталога и без сохранённого доступа упрётся в ту же стену.
#
# Сначала пустое значение, потом наше: --add дописывает helper в список, а не
# заменяет его, и унаследованный из глобального конфига диспетчер учётных
# данных Windows отвечал бы первым — своим, чужим и негодным доступом.
if (Test-Path $credFile) {
    $null = Try-Git @('-C', $Dir, 'config', '--local', 'credential.helper', '')
    $null = Try-Git @('-C', $Dir, 'config', '--local', '--add',
                      'credential.helper', "store --file=$credGit")
    Ok "доступ сохранён — кнопка «Обновить» на дашборде будет работать"
}

# -- Дальше обычный установщик ----------------------------------------------
Set-Location $Dir
Write-Host ""
Write-Host "-- Установка окружения -------------------------------------------------"
Write-Host ""
& powershell -ExecutionPolicy Bypass -File (Join-Path $Dir 'install.ps1')
exit $LASTEXITCODE
