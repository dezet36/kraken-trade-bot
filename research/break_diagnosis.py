"""
Что отличает пробой, который продолжился, от пробоя, который вернулся.

ПОЧЕМУ ДИАГНОСТИКА, А НЕ ОЧЕРЕДНОЙ ФИЛЬТР. Пробой отвергался дважды, и обе
попытки шли одинаково: добавить условие, выбранное по интуиции — прижатие,
объём, закрытие за уровнем, — и посмотреть, поможет ли. Оба раза не помогло.

Так проверять дальше бессмысленно: интуитивных условий бесконечно много, и
перебирая их по одному, мы рано или поздно найдём то, что «сработало»
случайно. Здесь спрашивается обратное: берём ВСЕ пробои и смотрим, какие
наблюдаемые в момент пробоя признаки РАЗДЕЛЯЮТ исход. Если ни один не
разделяет — добавлять нечего, и это ответ.

ИЗМЕРЯЕТСЯ ЧИСТЫЙ ХОД ВПЕРЁД, БЕЗ СТОПОВ И БЕЗ ЦЕЛЕЙ. Стоп и цель — это
геометрия, наложенная поверх; она может превратить край в убыток и наоборот, и
потому маскирует главный вопрос. Здесь только: куда пошла цена через N баров
после пробоя. Тот же приём, что рекомендовал документ по волнам Эллиотта и
который вскрыл, что у волновой разметки направленной информации нет вовсе.

ПРИЗНАКИ ВЗЯТЫ ТОЛЬКО ТЕ, ЧТО ИЗВЕСТНЫ В МОМЕНТ ПРОБОЯ. Ни одного, который
требует будущего. Проверяются:

    объём на пробойной свече     классическое подтверждение
    сжатие перед пробоем         диапазон окна к своему среднему
    возраст канала               давно ли граница не обновлялась
    ширина канала в ATR          пробой узкого и широкого — разные события
    согласие со старшим трендом  цена выше или ниже средней за месяц
    час суток                    сессии и сбросы фандинга
    сторона                      лонг или шорт
    расстояние до средней        насколько цена уже ушла

ОТКРЫТЫЙ ИНТЕРЕС ДОБАВЛЕН ВТОРЫМ ЗАХОДОМ, И ВОТ ПОЧЕМУ. Первый прогон показал
ровно то, ради чего затевался: ни один свечной признак не разделяет исход
устойчиво — знак скачет между периодами. Вывод из этого напрашивался «нужны
данные другой природы, а их истории нет, надо копить месяцами». Вывод был
неверен: биржа для открытого интереса молча игнорирует параметр `since` и
отдаёт последние двести записей, отчего и казалось, что глубже восьми суток
ничего нет. Шагать надо КОНЦОМ окна, и тогда история открывается до 2022 года
(research/fetch_open_interest.py).

Открытый интерес важен тем, что различает два события, в свечах неотличимые:

    цена за уровень, интерес РАСТЁТ    входят новые деньги, позиция набирается
    цена за уровень, интерес ПАДАЕТ    закрывают встречную, топливо кончается

ЗНАК ЗДЕСЬ НЕ ПЕРЕВОРАЧИВАЕТСЯ, в отличие от хода цены. Рост интереса на пробое
вверх означает новые лонги, на пробое вниз — новые шорты; и то и другое
подтверждает движение. Поэтому признак берётся как есть, без привязки к
стороне. Перевернуть его было бы ошибкой, гасящей ровно тот эффект, который
ищется.

ЧЕГО ПО-ПРЕЖНЕМУ НЕТ, И ЭТО ПРОВЕРЕНО ТОЧНО ТАК ЖЕ: соотношение лонг/шорт
(стена на 8.3 суток, шаг концом окна не действует) и дельта агрессора (нужна
лента сделок, история не отдаётся вовсе). Фандинг доступен за год и глубже.

Запуск:
    python research/break_diagnosis.py
"""

import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'Live_Bot'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import BEAR_CACHE, BEAR_PAIRS, BULL_CACHE, BULL_PAIRS  # noqa: E402
from common import RISING_CACHES, RISING_PAIRS, ci, diff_ci  # noqa: E402

PAIRS_LIMIT = 16
CHANNEL = 168          # недельный канал — там, где валовый край есть
FORWARD = 72           # смотрим на трое суток вперёд
TREND_MA = 720         # месячная средняя для согласия со старшим трендом

PERIODS = [
    ('2022-01 падение', BEAR_CACHE, BEAR_PAIRS),
    ('2023-07 РОСТ',    RISING_CACHES[0], RISING_PAIRS),
    ('2024-07 РОСТ',    RISING_CACHES[1], RISING_PAIRS),
    ('2025-05 падение', BULL_CACHE, BULL_PAIRS),
]


def load_oi(cache_dir, pair, stamps):
    """
    Открытый интерес, выровненный по свечам пары. NaN, где записи нет.

    Выравнивание идёт ПО МЕТКЕ ВРЕМЕНИ, а не по порядку строк: в истории
    интереса бывают пропуски, и совмещение по индексу тихо сдвинуло бы весь
    ряд, подставив в признак будущее.
    """
    path = os.path.join(cache_dir, 'open_interest', f'{pair}_1h.csv')
    if not os.path.exists(path):
        return None
    table = pd.read_csv(path)
    if table.empty:
        return None
    ts = pd.to_datetime(table['timestamp'], utc=True).dt.tz_localize(None)
    series = pd.Series(table['open_interest'].to_numpy(float), index=ts)
    series = series[~series.index.duplicated(keep='last')]
    return series.reindex(stamps).to_numpy(float)


def collect(cache_dir, pairs, label):
    """Все пробои недельного канала с признаками и ходом вперёд."""
    os.environ['SMC_CACHE_DIR'] = cache_dir
    sys.modules.pop('backtest_smc', None)
    import backtest_smc as bt
    from trend import core

    print(f'[{label}] сбор пробоев...', flush=True)
    rows = []
    for pair in pairs[:PAIRS_LIMIT]:
        loaded = bt.load_pair(pair)
        if loaded is None or '1h' not in loaded:
            continue
        df = loaded['1h']
        high = df['high'].to_numpy(float)
        low = df['low'].to_numpy(float)
        close = df['close'].to_numpy(float)
        volume = (df['volume'].to_numpy(float) if 'volume' in df.columns
                  else np.ones(len(close)))
        stamps = pd.to_datetime(df['timestamp'])
        if getattr(stamps.dt, 'tz', None) is not None:
            stamps = stamps.dt.tz_convert('UTC').dt.tz_localize(None)
        hours = stamps.dt.hour.to_numpy()

        atr = core.atr_series(high, low, close)
        top, bottom = core.channels(high, low, CHANNEL)
        vol_avg = pd.Series(volume).rolling(48).mean().to_numpy()
        rng = pd.Series(high - low).rolling(24).mean().to_numpy()
        rng_avg = pd.Series(high - low).rolling(240).mean().to_numpy()
        ma = pd.Series(close).rolling(TREND_MA).mean().to_numpy()

        oi = load_oi(cache_dir, pair, stamps)
        if oi is None:
            oi_day = oi_bar = np.full(len(close), np.nan)
        else:
            shifted_day = pd.Series(oi).shift(24)
            oi_day = ((oi - shifted_day) / shifted_day * 100).to_numpy()
            shifted_bar = pd.Series(oi).shift(1)
            oi_bar = ((oi - shifted_bar) / shifted_bar * 100).to_numpy()

        last = -10 ** 9
        for i in range(max(CHANNEL, TREND_MA) + 20, len(close) - FORWARD):
            if i - last < 72:
                continue
            setup = core.find_break(close, i, top, bottom, atr, break_atr=0.30)
            if setup is None:
                continue
            last = i
            up = setup['direction'] == 'LONG'
            price = close[i]
            ahead = (close[i + FORWARD] - price) / price * 100
            rows.append({
                # Ход вперёд В СТОРОНУ СДЕЛКИ: для шорта знак переворачивается,
                # иначе признаки лонгов и шортов складывались бы со взаимным
                # погашением, и любой из них выглядел бы бессильным.
                'forward': ahead if up else -ahead,
                'volume': volume[i] / vol_avg[i] if vol_avg[i] > 0 else np.nan,
                'squeeze': rng[i] / rng_avg[i] if rng_avg[i] > 0 else np.nan,
                'width': (top[i] - bottom[i]) / atr[i] if atr[i] > 0 else np.nan,
                'with_trend': 1.0 if (price > ma[i]) == up else 0.0,
                'from_ma': abs(price - ma[i]) / atr[i] if atr[i] > 0 else np.nan,
                'hour': float(hours[i]),
                'long': 1.0 if up else 0.0,
                # Знак НЕ переворачивается по стороне: рост интереса на пробое
                # вверх — новые лонги, на пробое вниз — новые шорты, и то и
                # другое подтверждает движение.
                #
                # ОБА ОКНА КОНЧАЮТСЯ НА НАЧАЛЕ ПРОБОЙНОГО ЧАСА. Снимок интереса
                # с меткой T — состояние на начало часа T, а вход идёт по его
                # закрытию. Взять интерес самого пробойного часа значило бы
                # заглянуть в строку вперёд. Замер от этого только строже:
                # признак знает МЕНЬШЕ, чем знал бы торгующий.
                'oi_day': oi_day[i],
                'oi_bar': oi_bar[i],
            })
    print(f'      пробоев {len(rows)}', flush=True)
    return rows


FEATURES = [
    ('объём к среднему',          'volume',      'выше'),
    ('сжатие перед пробоем',      'squeeze',     'сильнее'),
    ('ширина канала в ATR',       'width',       'шире'),
    ('согласие со старшим ТФ',    'with_trend',  'по тренду'),
    ('удалённость от средней',    'from_ma',     'дальше'),
    ('сторона',                   'long',        'лонг'),
    ('интерес за сутки до',       'oi_day',      'рос'),
    ('интерес за час до',         'oi_bar',      'рос'),
]


def split_report(rows, label):
    """Разделяет ли признак исход. Сравниваются верхняя и нижняя половины."""
    table = pd.DataFrame(rows)
    if table.empty:
        return {}
    forward = table['forward'].to_numpy(float)
    lo, hi = ci(forward)
    print()
    print(f'--- {label} ---')
    print(f'всего пробоев {len(table)}, средний ход вперёд '
          f'{forward.mean():+.2f}% [{lo:+.2f}; {hi:+.2f}]')
    print(f'{"признак":<28}{"нижняя половина":>18}{"верхняя половина":>19}'
          f'{"разница":>11}{"интервал разницы":>24}')
    print('-' * 100)
    out = {}
    for human, key, _hint in FEATURES:
        values = table[key].to_numpy(float)
        good = np.isfinite(values) & np.isfinite(forward)
        if good.sum() < 60:
            continue
        v, f = values[good], forward[good]
        if set(np.unique(v)) <= {0.0, 1.0}:
            low_side, high_side = f[v == 0], f[v == 1]
        else:
            median = np.median(v)
            low_side, high_side = f[v <= median], f[v > median]
        if len(low_side) < 25 or len(high_side) < 25:
            continue
        gap = high_side.mean() - low_side.mean()
        dlo, dhi = diff_ci(high_side, low_side)
        out[key] = (gap, dlo, dhi)
        mark = '  ←' if (dlo > 0 or dhi < 0) else ''
        print(f'{human:<28}{low_side.mean():>17.2f}%{high_side.mean():>18.2f}%'
              f'{gap:>+11.2f}{f"[{dlo:+.2f}; {dhi:+.2f}]":>24}{mark}')
    return out


def main():
    per_period = {}
    for label, cache, pairs in PERIODS:
        rows = collect(cache, pairs, label)
        if rows:
            per_period[label] = rows

    print()
    print('=' * 100)
    print(f'ХОД ЦЕНЫ ЧЕРЕЗ {FORWARD} ЧАСОВ ПОСЛЕ ПРОБОЯ, В СТОРОНУ СДЕЛКИ')
    print('Без стопов и без целей: только куда пошла цена.')
    print('=' * 100)
    splits = {}
    for label, rows in per_period.items():
        splits[label] = split_report(rows, label)

    print()
    print('=' * 100)
    print('РАЗДЕЛЯЕТ ЛИ ПРИЗНАК ИСХОД НА ВСЕХ ЧЕТЫРЁХ ПЕРИОДАХ')
    print('=' * 100)
    labels = list(per_period)
    head = f'{"признак":<28}' + ''.join(f'{lab:>18}' for lab in labels)
    print(head)
    print('-' * len(head))
    for human, key, hint in FEATURES:
        cells, signs, strong = '', [], 0
        for label in labels:
            cell = splits.get(label, {}).get(key)
            if cell is None:
                cells += f'{"—":>18}'
                signs.append(0)
                continue
            gap, dlo, dhi = cell
            signs.append(1 if gap > 0 else -1)
            strong += 1 if (dlo > 0 or dhi < 0) else 0
            cells += f'{f"{gap:+.2f}":>18}'
        same = signs and all(s == signs[0] != 0 for s in signs)
        note = ''
        if same and strong >= 2:
            note = f'  РАЗДЕЛЯЕТ ({hint} лучше)' if signs[0] > 0 \
                   else f'  РАЗДЕЛЯЕТ (наоборот: {hint} хуже)'
        elif same:
            note = '  знак один, но значимости нет'
        else:
            note = '  знак пляшет — не разделяет'
        print(f'{human:<28}{cells}{note}')

    print()
    print('ЧТО ЭТО ЗНАЧИТ. Признак, у которого знак пляшет между периодами, не')
    print('разделяет исход, и добавлять его фильтром бессмысленно — именно так')
    print('погибли обе прежние попытки пробоя.')
    print()
    print('ЧИТАТЬ СТРОКИ ПРО ИНТЕРЕС ОСОБЕННО ОСТОРОЖНО. Их две, периодов')
    print('четыре, и разделяющим признак считается только тот, у кого знак ОДИН')
    print('на всех четырёх И интервал не накрывает ноль хотя бы на двух. Восемь')
    print('клеток при случайности дают примерно 0.4 значимых — то есть одна')
    print('случайная звёздочка здесь ожидаема, и на одну звёздочку опираться')
    print('нельзя. Согласованность знака важнее любой отдельной клетки.')
    print()
    print('ЕСЛИ ИНТЕРЕС РАЗДЕЛЯЕТ — это первый признак иной природы, чем всё,')
    print('что провалилось, и его стоит проверить фильтром на отложенных парах.')
    print('ЕСЛИ НЕТ — в доступных данных края для пробоя нет, и остаются только')
    print('дельта агрессора и лонг/шорт, которых историей не достать вовсе.')


if __name__ == '__main__':
    main()
