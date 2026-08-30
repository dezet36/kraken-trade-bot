"""
Что цена делала ПОСЛЕ выхода из сделки.

ЗАЧЕМ. Журнал знает, чем сделка кончилась, и молчит о том, что было дальше.
А это ровно тот вопрос, который решает судьбу стопов и целей:

    выбило по стопу — а через час цена дошла бы до цели? стоп тесен;
    взяли цель      — а цена прошла ещё вдвое?          цель близка.

Разбор 364 сделок 29 августа 2026 упёрся в это дважды. Проигравшие подходили
к цели редко (10% доходили до 0.8R), победители оставляли на столе всего
0.12R — но оба числа меряются ДО выхода. Что происходило после, не знал никто,
и «стоп слишком тесный» осталось догадкой.

ОТДЕЛЬНЫМ ФАЙЛОМ, А НЕ КОЛОНКАМИ В ЖУРНАЛЕ. Наблюдение длится часами после
закрытия, то есть строку пришлось бы дописывать задним числом. Журнал сделок
пишется только вперёд, и это его главное свойство: строка, однажды записанная,
больше не меняется. Переписывать её ради наблюдения значит поставить под
сомнение весь файл.

Связь по trade_id: при разборе два файла соединяются по нему.
"""

import csv
import os

import config
from logger import log

CSV_PATH = os.path.join(config.DATA_DIR, 'follow_up.csv')

# Горизонты наблюдения в часах. Час — успела ли цена вернуться сразу; четыре —
# пережила ли идея ближайшие колебания; двенадцать — дошло ли дело до цели.
HORIZONS = (1, 4, 12)
_MS_HOUR = 3_600_000

COLUMNS = [
    'trade_id', 'strategy', 'pair', 'direction', 'closed_at', 'exit_reason',
    'entry_price', 'exit_price', 'stop_loss', 'tp1',
    # Куда цена уходила после выхода, в долях первоначального риска и
    # относительно ВХОДА: так число прямо отвечает, до какого R дошла бы
    # сделка, если бы её не закрыли.
    'best_after_r', 'worst_after_r',
    # Цена на каждом горизонте, тоже в R от входа.
    'r_1h', 'r_4h', 'r_12h',
    # Дошло ли дело до цели или до стопа уже ПОСЛЕ нашего выхода.
    'hit_tp1_after', 'hit_sl_after',
    'observed_hours',
]


def watch(pos, ts, exit_price, reason, trade_id):
    """
    Заводит наблюдение за парой после закрытия сделки.

    Возвращает словарь для хранения в состоянии брокера. Всё нужное для
    пересчёта в R берётся сразу: сама позиция вот-вот исчезнет.
    """
    entry = float(pos['entry_price'])
    stop = float(pos['stop_loss'])
    sl_dist = abs(entry - stop)
    targets = pos.get('targets') or []
    return {
        'trade_id': trade_id,
        'strategy': pos['strategy'],
        'pair': pos['pair'],
        'direction': pos['direction'],
        'closed_ts': ts,
        'closed_at': pos.get('closed_at') or '',
        'exit_reason': reason,
        'entry_price': entry,
        'exit_price': float(exit_price),
        'stop_loss': stop,
        'tp1': float(targets[0]) if targets else '',
        'sl_dist': sl_dist,
        'best': entry,
        'worst': entry,
        'marks': {},
        'hit_tp1': 0,
        'hit_sl': 0,
        'last_ts': ts,
    }


def _r(w, price):
    """Цена в долях риска относительно ВХОДА, со знаком сделки."""
    if not w['sl_dist']:
        return ''
    sign = 1 if w['direction'] == 'LONG' else -1
    return round(sign * (price - w['entry_price']) / w['sl_dist'], 3)


def advance(watches, pair, ts, high, low, close):
    """
    Прогоняет одну свечу через наблюдения по этой паре.

    Возвращает список ДОСМОТРЕННЫХ наблюдений — их пора записывать и убирать.
    Остальные остаются в списке до своего срока.
    """
    done = []
    for w in watches:
        if w['pair'] != pair or ts <= w['closed_ts'] or ts <= w['last_ts']:
            continue
        w['last_ts'] = ts
        is_long = w['direction'] == 'LONG'

        # Лучшее и худшее — по ходу ИСХОДНОЙ сделки: вопрос в том, дошла бы
        # она до цели, а не в том, куда вообще ушла цена.
        w['best'] = max(w['best'], high) if is_long else min(w['best'], low)
        w['worst'] = min(w['worst'], low) if is_long else max(w['worst'], high)

        if w['tp1'] != '':
            reached = high >= w['tp1'] if is_long else low <= w['tp1']
            if reached:
                w['hit_tp1'] = 1
        touched_stop = low <= w['stop_loss'] if is_long else high >= w['stop_loss']
        if touched_stop:
            w['hit_sl'] = 1

        hours = (ts - w['closed_ts']) / _MS_HOUR
        for h in HORIZONS:
            if hours >= h and str(h) not in w['marks']:
                w['marks'][str(h)] = close
        if hours >= HORIZONS[-1]:
            done.append(w)
    return done


def row(w):
    """Строка для файла. Незаполненные горизонты остаются пустыми."""
    hours = (w['last_ts'] - w['closed_ts']) / _MS_HOUR
    out = {
        'trade_id': w['trade_id'], 'strategy': w['strategy'], 'pair': w['pair'],
        'direction': w['direction'], 'closed_at': w['closed_at'],
        'exit_reason': w['exit_reason'],
        'entry_price': w['entry_price'], 'exit_price': w['exit_price'],
        'stop_loss': w['stop_loss'], 'tp1': w['tp1'],
        'best_after_r': _r(w, w['best']), 'worst_after_r': _r(w, w['worst']),
        'hit_tp1_after': w['hit_tp1'], 'hit_sl_after': w['hit_sl'],
        'observed_hours': round(hours, 1),
    }
    for h in HORIZONS:
        mark = w['marks'].get(str(h))
        out[f'r_{h}h'] = _r(w, mark) if mark is not None else ''
    return out


def write(rows):
    """Дописывает наблюдения в файл. Отказ записи не имеет права мешать боту."""
    if not rows:
        return
    try:
        fresh = not os.path.exists(CSV_PATH) or os.path.getsize(CSV_PATH) == 0
        with open(CSV_PATH, 'a', encoding='utf-8', newline='') as fh:
            writer = csv.DictWriter(fh, fieldnames=COLUMNS, extrasaction='ignore')
            if fresh:
                writer.writeheader()
            for r in rows:
                writer.writerow(r)
    except Exception as exc:                       # noqa: BLE001
        log(f'⚠️ Наблюдение после выхода не записано: {exc}')
