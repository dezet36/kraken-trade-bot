# Автозапуск Kraken.exe при входе в систему.
#
# ЗАЧЕМ. Замер по журналу за 12 суток: бот работал 55 часов из 290 — 19%
# времени, 23 перерыва, два из них по 40 и 94 часа. Приложение оконное, живёт
# ровно столько, сколько открыто окно, а автозапуска не было: ни задачи в
# планировщике, ни записи в автозагрузке.
#
# Цена этого — не абстрактная. Измеренная частота сетапов: LEVELS 0.9 в сутки,
# RSIBB 2.6 в сутки на 21 паре. При 19% времени это один раз в 6 суток и один
# раз в 2 суток соответственно — отсюда ноль сделок у обеих за всю историю
# наблюдений. Поднять время работы значит увеличить число сделок примерно
# впятеро, не трогая ни одной строки в логике стратегий.
#
# ПОЧЕМУ НЕ install_service.ps1. Тот ставит venv и уводит данные в bot_data\,
# то есть создаёт ТРЕТЬЮ папку данных рядом с Live_Bot\ и dist\. Журнал сделок
# уже был расколот надвое, и разбор по неполному журналу однажды дал уверенный
# неверный ответ. Здесь папка данных не трогается: exe пишет рядом с собой.
#
# Запуск:   powershell -ExecutionPolicy Bypass -File Live_Bot\install_autostart.ps1
# Удалить:  powershell -ExecutionPolicy Bypass -File Live_Bot\install_autostart.ps1 -Remove

param([switch]$Remove)

$ErrorActionPreference = 'Stop'
$TaskName = 'KrakenDesktop'

$botDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$root   = Split-Path -Parent $botDir
$exe    = Join-Path $root 'dist\Kraken.exe'

if ($Remove) {
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($existing) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Автозапуск снят: задача $TaskName удалена."
    } else {
        Write-Host "Задачи $TaskName нет — снимать нечего."
    }
    exit 0
}

if (-not (Test-Path $exe)) {
    throw ("Не найден $exe. Собери приложение: " +
           "powershell -ExecutionPolicy Bypass -File Live_Bot\build_exe.ps1")
}

# ЗАДЕРЖКА ПОСЛЕ ВХОДА — НЕ ПЕРЕСТРАХОВКА. Бот на старте идёт к бирже за
# свечами по двум десяткам пар. Сразу после входа в систему сеть чаще всего
# ещё поднимается, и первый цикл уходит в отказы подключения. Минуты хватает.
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$trigger.Delay = 'PT1M'

$action = New-ScheduledTaskAction -Execute $exe -WorkingDirectory (Split-Path $exe)

# Ноутбук на батарее — обычное дело, и останавливать из-за этого торговлю
# нельзя: позиции в рынке ведутся, пока работает бот.
#
# Предела времени выполнения нет: задача по смыслу бессрочная, а значение по
# умолчанию (трое суток) убивало бы её посреди недели.
#
# Перезапуск при сбое — на случай падения процесса. Само окно, закрытое
# человеком, перезапуском не считается: приложение завершается штатно, и
# планировщик такое не перезапускает.
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 2)

# Обычные права: приложению не нужен администратор, а задача с повышением
# требует подтверждения UAC при каждом входе.
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive -RunLevel Limited

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) { Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false }

Register-ScheduledTask -TaskName $TaskName -Trigger $trigger -Action $action `
    -Settings $settings -Principal $principal `
    -Description 'Торговый бот Kraken: запуск при входе в систему' | Out-Null

Write-Host ""
Write-Host "Автозапуск установлен."
Write-Host "  задача:      $TaskName"
Write-Host "  приложение:  $exe"
Write-Host "  запуск:      при входе в систему, через минуту"
Write-Host ""
Write-Host "Два экземпляра не поднимутся: замок привязан к папке данных, и"
Write-Host "второй запуск покажет уже открытое окно вместо нового бота."
Write-Host ""
Write-Host "Снять:  powershell -ExecutionPolicy Bypass -File Live_Bot\install_autostart.ps1 -Remove"
