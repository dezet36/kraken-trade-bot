"""
Чтение настроек стратегии из окружения. ОДНА реализация на все стратегии.

ПОЧЕМУ ОТДЕЛЬНЫМ МОДУЛЕМ. У каждой стратегии был свой набор `_f`/`_i`/`_s` —
четыре строки, скопированные трижды. Копии разошлись: LEVELS ловила разбор
числа и откатывалась на значение по умолчанию, RSIBB и SMC — нет.

Цена расхождения оказалась несоразмерна его размеру. Число, записанное с
запятой вместо точки — обычная опечатка в русской раскладке, — роняло импорт
модуля параметров. А стратегии импортируются в bot.py на верхнем уровне,
значит `RSIBB_RISK_PCT=0,5` не запускал НИЧЕГО: ни одной стратегии, ни панели,
ни ведения уже открытых позиций. В собранном приложении — молча.

Запятая здесь принимается как десятичный разделитель, а не отвергается. Это
не поблажка: человек, набравший «0,5», имел в виду ноль целых пять десятых, и
притворяться, будто смысл непонятен, — упрямство. Непонятное же (буквы, пустая
строка, два разделителя) откатывается на значение по умолчанию с громкой
записью в журнал: молчаливый откат превратил бы настройку в декорацию.
"""

from logger import log


def _clean(raw):
    """
    Приводит записанное человеком число к тому, что понимает float().

    Запятая читается по месту, а не по правилу. Если точки нет — «0,5» — это
    десятичный разделитель русской раскладки. Если точка есть — «1,234.5» —
    запятая разделяет тысячи, и подменять её значило бы прочесть число в
    тысячу раз меньше. Оба случая встречаются в настройках, набранных руками.
    """
    text = str(raw).strip().replace(' ', '')
    if ',' in text:
        text = text.replace(',', '.') if '.' not in text else text.replace(',', '')
    return text


def reader(prefix):
    """
    Возвращает (_f, _i, _b, _s) для стратегии с этим префиксом.

    Префикс обязателен и разделяет стратегии: `LEVELS_RISK_PCT` и
    `RSIBB_RISK_PCT` — разные настройки, и общее имя `RISK_PCT` меняло бы обе
    сразу. Требование «каждая стратегия работает отдельно по своим параметрам»
    держится именно на нём.
    """
    import os

    def _f(name, default):
        raw = os.getenv(f'{prefix}_{name}')
        if raw is None:
            return float(default)
        try:
            return float(_clean(raw))
        except (TypeError, ValueError):
            log(f'⚠️ {prefix}_{name}={raw!r} — не число, беру {default}')
            return float(default)

    def _i(name, default):
        raw = os.getenv(f'{prefix}_{name}')
        if raw is None:
            return int(default)
        try:
            return int(float(_clean(raw)))
        except (TypeError, ValueError):
            log(f'⚠️ {prefix}_{name}={raw!r} — не число, беру {default}')
            return int(default)

    def _b(name, default):
        raw = os.getenv(f'{prefix}_{name}')
        if raw is None:
            return bool(default)
        return str(raw).strip().lower() in ('1', 'true', 'yes', 'да', 'on')

    def _s(name, default):
        raw = os.getenv(f'{prefix}_{name}')
        return default if raw is None else str(raw).strip()

    return _f, _i, _b, _s
