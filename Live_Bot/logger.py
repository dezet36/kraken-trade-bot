import os
from datetime import datetime

# Персистентные файлы — в BOT_DATA_DIR, если задан (сервер: каталог ВНЕ кода,
# переживает полную замену папки Live_Bot), иначе рядом с модулем (локальная
# разработка — поведение как раньше). Не импортируем config (циклический импорт:
# config.py сам импортирует log из этого модуля) — считаем env напрямую.
_DATA_DIR = os.getenv('BOT_DATA_DIR') or os.path.dirname(os.path.abspath(__file__))
os.makedirs(_DATA_DIR, exist_ok=True)
LOG_FILE  = os.path.join(_DATA_DIR, "bot_log.txt")
TRADES_CSV = os.path.join(_DATA_DIR, "trades.csv")


def log(message, level="INFO"):
    """Пишет сообщение в лог-файл и в консоль.

    ВАЖНО: логирование — best-effort. Сбой записи в файл или вывода в консоль
    НИКОГДА не должен ронять бота (раньше PermissionError на bot_log.txt валил весь цикл).
    """
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry = f"[{timestamp}] [{level}] {message}"

    # Консоль (может упасть на cp1251 из-за эмодзи — не критично)
    try:
        print(entry)
    except Exception:
        try:
            print(entry.encode("ascii", "replace").decode("ascii"))
        except Exception:
            pass

    # Файл (может быть недоступен/занят/read-only — глушим, бот продолжает работать)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(entry + "\n")
    except Exception:
        pass


def log_trade(trade_data):
    """Записывает сделку в trades.csv. Тоже best-effort — сбой не роняет бота."""
    try:
        write_header = not os.path.exists(TRADES_CSV) or os.path.getsize(TRADES_CSV) == 0
        with open(TRADES_CSV, "a", encoding="utf-8") as f:
            if write_header:
                f.write("time,pair,direction,entry,exit,pnl,zone,result,mode\n")
            f.write(
                f"{trade_data['time']},{trade_data['pair']},{trade_data['direction']},"
                f"{trade_data['entry']},{trade_data['exit']},{trade_data['pnl']},"
                f"{trade_data['zone']},{trade_data['result']},{trade_data.get('mode', 'DEMO')}\n"
            )
    except Exception:
        pass
