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
git ls-remote $RepoUrl HEAD 2>$null | Out-Null
if ($LASTEXITCODE -eq 0) {
    $needToken = $false
    Ok "репозиторий доступен без токена"
} elseif (Test-Path $credFile) {
    git -c "credential.helper=store --file=$credGit" ls-remote $RepoUrl HEAD 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) {
        $needToken = $false
        Ok "токен уже сохранён с прошлой установки"
    }
}

if ($needToken) {
    Write-Host ""
    Write-Host "  Репозиторий закрытый — нужен токен доступа GitHub."
    Write-Host ""
    Write-Host "  Где взять: github.com -> Settings -> Developer settings ->"
    Write-Host "             Personal access tokens -> Fine-grained tokens -> Generate"
    Write-Host "             Repository access: только kraken-trade-bot"
    Write-Host "             Permissions: Contents -> Read-only"
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

    git -c "credential.helper=store --file=$credGit" ls-remote $RepoUrl HEAD 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Remove-Item $credFile -Force -ErrorAction SilentlyContinue
        Die "токен не подошёл. Проверьте, что у него есть доступ к kraken-trade-bot (Contents: Read-only)"
    }
    Ok "токен принят"
}

$auth = @()
if (Test-Path $credFile) { $auth = @('-c', "credential.helper=store --file=$credGit") }

# -- Код ---------------------------------------------------------------------
# Клонируем целиком, без разреженной выкладки: весь репозиторий — пара
# мегабайт, а список файлов пришлось бы держать в двух местах. Стоит добавить
# файл в корень и забыть про список — и на сервере его молча не окажется,
# причём видно это станет не при установке, а при первом запуске.
if (Test-Path (Join-Path $Dir '.git')) {
    Info "папка уже существует — обновляю код"
    & git @auth -C $Dir fetch --quiet origin $Branch
    if ($LASTEXITCODE -ne 0) { Die "не удалось получить обновления" }
    # Только перемотка вперёд: локальные правки на сервере молча затирать нельзя.
    git -C $Dir merge --ff-only "origin/$Branch" --quiet
    if ($LASTEXITCODE -ne 0) {
        Die "на сервере есть локальные изменения кода. Уберите их (git -C $Dir status) и запустите снова"
    }
    Ok "код обновлён до $(git -C $Dir rev-parse --short HEAD)"
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
    & git @auth clone --quiet --branch $Branch $RepoUrl $tmp
    if ($LASTEXITCODE -ne 0) { Die "не удалось склонировать репозиторий" }
    New-Item -ItemType Directory -Force -Path $Dir | Out-Null
    Get-ChildItem -Force $tmp | Move-Item -Destination $Dir -Force
    Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
    Ok "код скачан, версия $(git -C $Dir rev-parse --short HEAD)"
}

# Токен нужен и кнопке «Обновить» на дашборде: она делает обычный git fetch из
# того же каталога и без сохранённого доступа упрётся в ту же стену.
if (Test-Path $credFile) {
    git -C $Dir config credential.helper "store --file=$credGit"
    Ok "доступ сохранён — кнопка «Обновить» на дашборде будет работать"
}

# -- Дальше обычный установщик ----------------------------------------------
Set-Location $Dir
Write-Host ""
Write-Host "-- Установка окружения -------------------------------------------------"
Write-Host ""
& powershell -ExecutionPolicy Bypass -File (Join-Path $Dir 'install.ps1')
exit $LASTEXITCODE
