"""
Докачка двух недостающих периодов: 2023-07 .. 2024-06 и 2024-07 .. 2025-04.

ЗАЧЕМ. У проекта было ровно два проверочных периода — падение 2022-23 и рост
2025-26. Двух точек хватает, чтобы отсеять случайную находку (правило «плюс на
обоих периодах» сегодня четырежды спасло от ложного результата), но НЕ хватает,
чтобы отличить свойство режима от свойства отрезка.

Это упёрлось в конкретный вопрос. Относительная сила показала устойчивый
эффект на бычьем периоде (воспроизвелся в обеих его половинах) и его
отсутствие на медвежьем. Объяснение «моментум работает в росте» правдоподобно,
но проверить его нечем: один бык и один медведь — это по одному наблюдению на
режим, а фильтр режима, приделанный по n=1, есть подгонка по определению.

Два новых периода дают четыре точки и делают такие гипотезы проверяемыми. Это
разблокирует не одну идею, а все будущие замеры.

ЧТО ЗА ПЕРИОДЫ И ПОЧЕМУ ИМЕННО ОНИ
    2023-07 .. 2024-06  восстановление второй половины 2023, затем рост с
                        халвингом в апреле 2024. Другой бычий рынок, не тот,
                        на котором всё настраивалось.
    2024-07 .. 2025-04  спад лета 2024, осенний рост, коррекция начала 2025.
                        Смешанный отрезок — самый неудобный и потому полезный.

Вместе с имеющимися получается: падение, восстановление, рост с халвингом,
смешанный, рост 2025-26. Пять режимов вместо двух.

ПАРЫ БЕРУТСЯ ТЕ, ЧТО СУЩЕСТВОВАЛИ В 2023 ГОДУ. Молодые инструменты (HYPE, SUI,
ZEC в нынешнем виде, PUMPFUN) тогда не торговались, и требовать их значило бы
получить пустые файлы вместо данных.

ФОРМАТ СОВПАДАЕТ с остальными кэшами, поэтому все существующие замеры
подхватят новые периоды без единой правки — достаточно указать каталог.

Запуск:
    python research/fetch_middle_periods.py
    python research/fetch_middle_periods.py --check   # только проверить глубину
"""

import os
import pickle
import sys
import time
from datetime import datetime, timezone

import ccxt

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PERIODS = {
    'mid1': ('backtest_cache_mid1', datetime(2023, 7, 1, tzinfo=timezone.utc),
             datetime(2024, 7, 1, tzinfo=timezone.utc),
             'восстановление и халвинг'),
    'mid2': ('backtest_cache_mid2', datetime(2024, 7, 1, tzinfo=timezone.utc),
             datetime(2025, 5, 1, tzinfo=timezone.utc),
             'спад, осенний рост, коррекция'),
}

# Пары, торговавшиеся на Bybit linear в середине 2023 года.
PAIRS = [
    'BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'XRPUSDT', 'BNBUSDT', 'DOGEUSDT',
    'ADAUSDT', 'AVAXUSDT', 'LINKUSDT', 'LTCUSDT', 'ARBUSDT', 'DOTUSDT',
    'XLMUSDT', 'NEARUSDT', 'UNIUSDT', 'AAVEUSDT', 'COTIUSDT', 'BICOUSDT',
    'SHIB1000USDT',
]

# 5m нужен движку для исполнения, 1h — для сигналов. 4h получается агрегацией,
# качать его отдельно незачем.
TIMEFRAMES = ['1h', '5m']
_TF_MS = {'5m': 5 * 60_000, '1h': 3_600_000, '4h': 4 * 3_600_000}


def exchange():
    return ccxt.bybit({'enableRateLimit': True,
                       'options': {'defaultType': 'linear'}})


def fetch_range(ex, cache_dir, symbol, timeframe, since_ms, until_ms, label=''):
    """
    Качает диапазон с продолжением после сбоев. Готовый файл не перекачивает.

    Возврат None означает «этой пары тогда не было» либо «биржа так и не
    отдала»: и то и другое не повод останавливать всю загрузку.
    """
    cache_file = os.path.join(cache_dir, f'{symbol}_{timeframe}.pkl')
    if os.path.exists(cache_file):
        with open(cache_file, 'rb') as fh:
            data = pickle.load(fh)
        print(f'  [есть] {symbol} {timeframe}: {len(data)}', flush=True)
        return data

    tf_ms = _TF_MS[timeframe]
    seen, out = set(), []
    cursor, fails = since_ms, 0
    print(f'  [качаю] {label} {symbol} {timeframe}...', end='', flush=True)

    while cursor < until_ms:
        try:
            batch = ex.fetch_ohlcv(symbol, timeframe, since=cursor, limit=1000)
            fails = 0
        except ccxt.BadSymbol:
            print(' символа нет', flush=True)
            return None
        except Exception as exc:                   # noqa: BLE001
            fails += 1
            if fails > 8:
                print(f' сдаюсь ({str(exc)[:40]})', flush=True)
                return None
            time.sleep(3)
            continue

        if not batch:
            break
        added = 0
        for candle in batch:
            if candle[0] not in seen and candle[0] < until_ms:
                seen.add(candle[0])
                out.append(candle)
                added += 1
        # Ноль новых свечей означает, что биржа отдаёт одно и то же окно:
        # дальше двигаться некуда, и цикл иначе стал бы вечным.
        if added == 0:
            break
        cursor = batch[-1][0] + tf_ms
        time.sleep(0.2)

    out.sort(key=lambda c: c[0])
    if not out:
        print(' пусто', flush=True)
        return None

    first = datetime.fromtimestamp(out[0][0] / 1000, timezone.utc).date()
    last = datetime.fromtimestamp(out[-1][0] / 1000, timezone.utc).date()
    print(f' {len(out)} ({first} .. {last})', flush=True)
    os.makedirs(cache_dir, exist_ok=True)
    with open(cache_file, 'wb') as fh:
        pickle.dump(out, fh)
    return out


def check_depth(ex):
    """
    Докуда биржа вообще отдаёт свечи. Спрашивается ДО загрузки.

    Если 5m за 2023 год недоступен, качать сутки впустую незачем — лучше
    узнать это одним запросом.
    """
    print('ПРОВЕРКА ГЛУБИНЫ (BTCUSDT)')
    print('-' * 72)
    for tf in TIMEFRAMES:
        for name, (_dir, start, _end, _note) in PERIODS.items():
            since = int(start.timestamp() * 1000)
            try:
                batch = ex.fetch_ohlcv('BTCUSDT', tf, since=since, limit=5)
                if not batch:
                    print(f'  {tf:>4} с {start:%Y-%m-%d}: пусто')
                    continue
                got = datetime.fromtimestamp(batch[0][0] / 1000, timezone.utc)
                lag = (got - start).days
                mark = 'ок' if abs(lag) <= 2 else f'сдвиг {lag} дн.'
                print(f'  {tf:>4} с {start:%Y-%m-%d}: отдал с {got:%Y-%m-%d}  {mark}')
            except Exception as exc:               # noqa: BLE001
                print(f'  {tf:>4} с {start:%Y-%m-%d}: ошибка {str(exc)[:40]}')
            time.sleep(0.3)


def main():
    ex = exchange()
    if '--check' in sys.argv:
        check_depth(ex)
        return

    check_depth(ex)
    print()
    for name, (folder, start, end, note) in PERIODS.items():
        cache_dir = os.path.join(ROOT, 'research', folder)
        print()
        print('=' * 72)
        print(f'{name}: {start:%Y-%m} .. {end:%Y-%m}  — {note}')
        print(f'каталог: {folder}')
        print('=' * 72)
        since = int(start.timestamp() * 1000)
        until = int(end.timestamp() * 1000)
        ok = 0
        for pair in PAIRS:
            got = all(fetch_range(ex, cache_dir, pair, tf, since, until, name)
                      is not None for tf in TIMEFRAMES)
            ok += bool(got)
        print(f'готово: {ok} пар из {len(PAIRS)}')

    print()
    print('Периоды подключаются к замерам указанием каталога в SMC_CACHE_DIR —')
    print('формат тот же, править существующие скрипты не нужно.')


if __name__ == '__main__':
    main()
