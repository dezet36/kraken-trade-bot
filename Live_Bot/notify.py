"""
Всплывающие уведомления Windows о событиях бота.

ЗАЧЕМ. Приложение живёт окном, которое сворачивают. Об открытии сделки, о
закрытии и об ошибке узнаёшь, только если посмотришь — а смотреть в окно
целый день никто не будет. Telegram эту роль частично играет, но за тем же
компьютером уведомление приходит быстрее и не требует телефона.

ПОЧЕМУ БЕЗ БИБЛИОТЕК. Уведомления показываются штатным механизмом Windows
через PowerShell. Дополнительная зависимость ради трёх всплывающих окон
означала бы плюс пакет в сборку и плюс место, где может сломаться то, что
торговле не нужно. Проверено, что способ работает на Windows 10.

ЧЕГО ЗДЕСЬ НЕТ. Гарантий. Уведомление — вспомогательная вещь: если оно не
показалось (выключены в системе, режим «не беспокоить», нет PowerShell),
бот об этом даже не узнает и продолжит торговать. Всё, что действительно
важно, лежит в журнале и на дашборде.
"""

import os
import subprocess
import sys
import threading
import time

from logger import log

APP_ID = 'Microsoft.Windows.Explorer'      # уведомления без своей регистрации в системе
ENABLED = os.getenv('DESKTOP_NOTIFY', 'true').lower() != 'false'

# Не чаще одного уведомления в две секунды: при закрытии нескольких позиций
# подряд Windows складывает их в стопку, а десяток всплывающих окон подряд
# раздражает сильнее, чем помогает.
_MIN_INTERVAL = 2.0
_last_at = 0.0
_lock = threading.Lock()

_SCRIPT = """
[void][Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime]
[void][Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType=WindowsRuntime]
$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent(
        [Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$x = $t.GetElementsByTagName('text')
$x.Item(0).AppendChild($t.CreateTextNode($env:KRAKEN_TITLE)) | Out-Null
$x.Item(1).AppendChild($t.CreateTextNode($env:KRAKEN_BODY)) | Out-Null
$n = New-Object Windows.UI.Notifications.ToastNotification $t
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($env:KRAKEN_APPID).Show($n)
"""


def _allowed(event):
    """Разрешено ли это событие в канал «рабочий стол»."""
    try:
        import settings_store as settings
        return settings.notify_on(event, 'desktop')
    except Exception:                              # noqa: BLE001
        return True                                # настройка недоступна — не молчим


def show(title, body):
    """
    Показывает уведомление. Ничего не возвращает и никогда не бросает.

    Текст передаётся через переменные окружения, а не подстановкой в сценарий:
    в названии пары или в тексте ошибки может оказаться кавычка, и подстановка
    превратила бы это в сломанный, а при неудачном стечении — и в чужой код.
    """
    if not ENABLED or sys.platform != 'win32':
        return
    global _last_at
    with _lock:
        now = time.monotonic()
        if now - _last_at < _MIN_INTERVAL:
            return
        _last_at = now

    env = dict(os.environ)
    env['KRAKEN_TITLE'] = str(title)[:80]
    env['KRAKEN_BODY'] = str(body)[:250]
    env['KRAKEN_APPID'] = APP_ID
    try:
        subprocess.Popen(
            ['powershell', '-NoProfile', '-NonInteractive', '-Command', _SCRIPT],
            env=env,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, close_fds=True)
    except Exception as exc:                       # noqa: BLE001
        log(f"уведомление не показано: {exc}")


def trade_opened(strategy, pair, direction, entry, risk):
    if not _allowed('trade_opened'):
        return
    show(f'{strategy}: вход {pair}',
         f'{direction} по {entry:.8g} · риск ${risk:.2f}')


def trade_closed(strategy, pair, pnl, pnl_r, reason):
    if not _allowed('trade_closed'):
        return
    sign = '+' if pnl >= 0 else '−'
    show(f'{strategy}: закрыта {pair}',
         f'{sign}${abs(pnl):.2f} ({pnl_r:+.2f} R) · {reason}')


def error(message):
    if not _allowed('error'):
        return
    show('Kraken: ошибка', message)
