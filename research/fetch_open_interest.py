"""
Загрузка ИСТОРИИ открытого интереса под кэши замеров.

ПОЧЕМУ ЭТОТ ФАЙЛ ПОЯВИЛСЯ. Дважды в этом проекте было записано, что истории
позиционирования не существует и остаётся только копить её живьём, вернувшись к
вопросу через два-три месяца. Это оказалось неверно, и неверно по ленивой
причине: проверялся параметр `since`, биржа его для открытого интереса молча
игнорирует и отдаёт последние двести записей. Отсюда и вывод «глубже восьми
суток данных нет».

Стена оказалась не у биржи, а у запроса. Шаг делается не началом окна, а его
КОНЦОМ: `params={'until': ...}` сдвигает окно в прошлое, и оно сдвигается —
проверено до 2022-03 на Bybit и на BingX. То есть открытый интерес доступен на
всех четырёх периодах замеров, и ждать не нужно ничего.

ЧТО ЭТО ДАЁТ. Открытый интерес — единственный доступный способ отличить два
события, которые в свечах выглядят одинаково:

    цена вверх, интерес РАСТЁТ    входят новые деньги, позиция набирается
    цена вверх, интерес ПАДАЕТ    закрывают шорты, топливо кончается

Ровно это различие теория пробоя называет главным, и ровно его нельзя было
проверить, пока считались одни свечи. Диагностика пробоя (break_diagnosis.py)
показала, что НИ ОДИН свечной признак не разделяет исход устойчиво — знак
скачет между периодами. Открытый интерес — первый признак иной природы.

ЧЕГО ЗАГРУЗИТЬ НЕЛЬЗЯ, И ЭТО ПРОВЕРЕНО ТОЧНО ТАК ЖЕ:
    соотношение лонг/шорт   стена на 8.3 суток, `until` не действует
    дельта агрессора        нужна лента сделок, история не отдаётся вовсе
Фандинг, в отличие от них, отдаётся за год и глубже обычным `since`.

Кладётся рядом с кэшем свечей, файлом на пару, тем же форматом, что у свечей.

Запуск:
    python research/fetch_open_interest.py
"""

import os
import sys
import time

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'Live_Bot'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import exchange  # noqa: E402
from common import (BEAR_CACHE, BEAR_PAIRS, BULL_CACHE, BULL_PAIRS,  # noqa: E402
                    RISING_CACHES, RISING_PAIRS)

HOUR = 3_600_000
BATCH = 200          # предел биржи на один запрос
PAUSE = 0.12         # бережно к лимиту запросов
PAIRS_LIMIT = 20

PERIODS = [
    (BEAR_CACHE, BEAR_PAIRS),
    (RISING_CACHES[0], RISING_PAIRS),
    (RISING_CACHES[1], RISING_PAIRS),
    (BULL_CACHE, BULL_PAIRS),
]


def cache_range(cache_dir, pair):
    """Границы времени свечей пары в кэше, чтобы качать ровно столько же."""
    path = os.path.join(cache_dir, f'{pair}_1h.pkl')
    if not os.path.exists(path):
        return None
    # В кэше лежат сырые строки ccxt: [метка, откр, макс, мин, закр, объём].
    bars = pd.read_pickle(path)
    if not bars:
        return None
    return int(bars[0][0]), int(bars[-1][0])


def download(client, symbol, start_ms, end_ms):
    """Шагаем от конца к началу, пока не закроем окно кэша."""
    seen = {}
    edge = end_ms
    while edge > start_ms:
        try:
            rows = client.fetch_open_interest_history(
                symbol, '1h', limit=BATCH, params={'until': edge})
        except Exception as exc:
            print(f'      сорвалось: {str(exc)[:60]}')
            break
        if not rows:
            break
        stamps = [r.get('timestamp') for r in rows if r.get('timestamp')]
        if not stamps:
            break
        for r in rows:
            ts = r.get('timestamp')
            value = r.get('openInterestAmount')
            if value is None:
                value = r.get('openInterestValue')
            if ts is None or value is None:
                continue
            seen[int(ts)] = float(value)
        oldest = min(stamps)
        # Окно не сдвинулось — биржа упёрлась в свой предел хранения. Молча
        # крутиться на месте нельзя: так рождаются «бесконечные» загрузки.
        if oldest >= edge - HOUR:
            break
        edge = oldest - 1
        time.sleep(PAUSE)
    return seen


def main():
    name = exchange.active_exchange_name()
    client = exchange.make_market_client(name)
    client.load_markets()
    print(f'источник: {name}')

    for cache_dir, pairs in PERIODS:
        out_dir = os.path.join(cache_dir, 'open_interest')
        os.makedirs(out_dir, exist_ok=True)
        print(f'\n=== {os.path.basename(cache_dir)} ===')
        for pair in pairs[:PAIRS_LIMIT]:
            out_path = os.path.join(out_dir, f'{pair}_1h.csv')
            if os.path.exists(out_path):
                print(f'  {pair:<12} уже есть')
                continue
            span = cache_range(cache_dir, pair)
            if span is None:
                print(f'  {pair:<12} свечей в кэше нет')
                continue
            symbol = exchange.market_symbol(pair, client)
            if symbol is None:
                print(f'  {pair:<12} нет такого рынка')
                continue
            start, end = span
            seen = download(client, symbol, start, end)
            covering = {t: v for t, v in seen.items() if start <= t <= end}
            if not covering:
                print(f'  {pair:<12} истории нет')
                continue
            frame = (pd.DataFrame({'timestamp': list(covering),
                                   'open_interest': list(covering.values())})
                     .sort_values('timestamp'))
            frame['timestamp'] = pd.to_datetime(frame['timestamp'], unit='ms',
                                                utc=True)
            frame.to_csv(out_path, index=False)
            hours = (end - start) / HOUR
            print(f'  {pair:<12} записей {len(frame):>5} '
                  f'покрытие {len(frame) / max(hours, 1) * 100:>5.1f}%')


if __name__ == '__main__':
    main()
