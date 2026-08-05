"""
Докачка новых пар в оба кэша — чтобы расширение пула можно было ЗАМЕРИТЬ.

Пул из 16 пар даёт стратегии уровней около 20-30 сделок в месяц. Расширение
пула — единственный рычаг количества, который не стоит качества (все
остальные проверены и отвергнуты). Но добавлять пары вслепую нельзя:
проверять их надо тем же двусторонним критерием, что и всё остальное.

КАКИЕ ПАРЫ И ПОЧЕМУ ИМЕННО ЭТИ

Список по обороту на бирже оказался ловушкой. В верхушке — токенизированные
акции и металлы (SPCX, SOXL, XAU, AMZN, META, AAPL, XAG). Это не крипта:
другие часы торгов, разрывы на выходных, другая природа движения. Стратегии
спроектированы и замерены на крипте, и подмешивать туда акции нельзя.

Вторая ловушка — свежие токены с большим оборотом и коротким прошлым
(HOME, CYS, PUMPFUN, BANK, BLESS, UB, SKYAI1, GRVT). Замерить их не на чем.

Остаются пары, у которых есть И оборот от $20 млн, И история, покрывающая
ОБА проверочных периода:

    NEAR   с 2021-10     UNI   с 2021-11     AAVE  с 2021-06
    COTI   с 2021-12     BICO  с 2021-12

Только их и качаем. Пары с историей от 2023 года (ONDO, ENA, 1000PEPE, WLD,
TAO, LDO, HYPE, 1000RATS, HFT) можно проверить лишь на бычьем периоде —
одностороннюю проверку эта работа не принимает.

Запуск:
    python research/fetch_new_pairs.py
"""

import os
import pickle
import sys
import time
from datetime import datetime, timezone

import ccxt

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fetch_bear_data import fetch_range  # noqa: E402

BULL_CACHE = os.path.join(ROOT, 'research', 'backtest_cache_12m')
BEAR_CACHE = os.path.join(ROOT, 'research', 'backtest_cache_bear')

# Пары, которые можно проверить на ОБОИХ периодах.
NEW_PAIRS = ['NEARUSDT', 'UNIUSDT', 'AAVEUSDT', 'COTIUSDT', 'BICOUSDT']

# Пары моложе медвежьего периода: замерить их можно только на бычьем.
# Добавлены по решению оператора. Оговорка остаётся в силе и записана здесь,
# а не только в переписке: за эту сессию односторонняя проверка трижды
# оказывалась ложной — конфигурация с лучшим результатом на быке дважды
# была почти худшей на медведе.
YOUNG_PAIRS = ['ONDOUSDT', 'ENAUSDT', '1000PEPEUSDT', 'WLDUSDT', 'TAOUSDT',
               'LDOUSDT', 'HYPEUSDT', '1000RATSUSDT', 'HFTUSDT']
# Дневные и 15-минутные свечи бэктест собирает из часовых и пятиминутных
# сам (backtest_smc.load_pair), качать их отдельно не нужно.
TIMEFRAMES = ['1h', '4h', '5m']

PERIODS = (
    ('бычий', BULL_CACHE,
     datetime(2025, 5, 1, tzinfo=timezone.utc),
     datetime(2026, 7, 20, tzinfo=timezone.utc)),
    ('медвежий', BEAR_CACHE,
     datetime(2022, 1, 1, tzinfo=timezone.utc),
     datetime(2023, 7, 1, tzinfo=timezone.utc)),
)


def main():
    exchange = ccxt.bybit({'options': {'defaultType': 'swap'},
                           'enableRateLimit': True})
    exchange.load_markets()

    for label, cache_dir, since, until in PERIODS:
        os.makedirs(cache_dir, exist_ok=True)
        print()
        print(f'=== {label} период: {since:%Y-%m} .. {until:%Y-%m} ===')
        # fetch_range пишет в свой CACHE_DIR, поэтому подменяем его на время
        import fetch_bear_data
        original = fetch_bear_data.CACHE_DIR
        fetch_bear_data.CACHE_DIR = cache_dir
        try:
            pairs = NEW_PAIRS + (YOUNG_PAIRS if label == 'бычий' else [])
            for pair in pairs:
                symbol = pair.replace('USDT', '/USDT:USDT')
                if symbol not in exchange.markets:
                    print(f'  {pair}: символа нет на бирже')
                    continue
                for timeframe in TIMEFRAMES:
                    data = fetch_range(
                        exchange, pair, timeframe,
                        int(since.timestamp() * 1000),
                        int(until.timestamp() * 1000), label=label)
                    if data is None:
                        continue
                    path = os.path.join(cache_dir, f'{pair}_{timeframe}.pkl')
                    if not os.path.exists(path):
                        with open(path, 'wb') as fh:
                            pickle.dump(data, fh)
                        print(f'  сохранено {pair} {timeframe}: {len(data)} свечей',
                              flush=True)
        finally:
            fetch_bear_data.CACHE_DIR = original

    print()
    print('Готово. Дальше — замер: расширенный пул против нынешнего,')
    print('приёмка двусторонняя, как и для всего остального.')


if __name__ == '__main__':
    main()
