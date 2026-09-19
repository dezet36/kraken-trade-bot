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
import csv_journal
from logger import log

CSV_PATH = os.path.join(config.DATA_DIR, 'follow_up.csv')

# Горизонты наблюдения в часах. Час — успела ли цена вернуться сразу; четыре —
# пережила ли идея ближайшие колебания; двенадцать — дошло ли дело до цели.
HORIZONS = (1, 4, 12)
_MS_HOUR = 3_600_000

COLUMNS = [
    # Бумага или бой: файл один на оба пути. Без разделения наблюдения двух
    # режимов смешиваются, а выводы по смешанной выборке ничего не значат.
    'mode',
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
    stamped = [{'mode': config.TRADING_MODE, **r} for r in rows]
    csv_journal.append(CSV_PATH, COLUMNS, stamped, 'наблюдение после выхода')


# ── Боевой путь ──────────────────────────────────────────────────────────────
#
# ПОЧЕМУ ОТДЕЛЬНЫЙ ПРИВОД. Бумажный брокер держит наблюдения в своём состоянии
# и прогоняет их теми же свечами, что и позиции: у него всё в одном цикле.
# У боевого пути такого места нет — свечи разбирают стратегии внутри скана, и
# тянуть наблюдения через четыре сканера значило бы связать их с логикой,
# которая к наблюдениям отношения не имеет.
#
# Поэтому здесь свой шаг: состояние в файле, свечи запрашиваются сами. Пар под
# наблюдением единицы (только те, где сделка закрылась меньше 12 часов назад),
# так что это несколько запросов за цикл.
#
# СОСТОЯНИЕ В ФАЙЛЕ, А НЕ В ПАМЯТИ. Наблюдение живёт 12 часов, а бот за это
# время перезапускается: обновлением, падением, руками. Держи мы его в памяти,
# до записи доживали бы только те наблюдения, которым повезло с простоем, — и
# выборка оказалась бы смещена в сторону спокойных периодов.

STATE_PATH = os.path.join(config.DATA_DIR, 'follow_up_state.json')


def load_state(path=None):
    """Наблюдения с прошлого запуска. Пустой список, если файла нет."""
    import json
    try:
        with open(path or STATE_PATH, encoding='utf-8') as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def save_state(watches, path=None):
    """Сохраняет наблюдения. Молча: это данные для разбора, а не для торговли."""
    import json
    try:
        with open(path or STATE_PATH, 'w', encoding='utf-8') as fh:
            json.dump(watches, fh, ensure_ascii=False)
    except OSError as exc:
        log(f'⚠️ Наблюдения после выхода не сохранены: {exc}')


def watch_live(position, pair, exit_price, reason, trade_id, strategy):
    """
    Заводит наблюдение по БОЕВОЙ позиции и кладёт его в файл состояния.

    Боевая позиция устроена иначе бумажной, поэтому нужные поля собираются
    здесь, а не в watch(): общая функция не должна знать про два формата.
    """
    from datetime import datetime, timezone
    try:
        params = position.get('params') or {}
        targets, _ = _live_targets(params)
        shaped = {
            'entry_price': position['entry_price'],
            'stop_loss': params['stop_loss'],
            'targets': targets,
            'strategy': strategy or '',
            'pair': pair,
            'direction': position['signal']['setup']['type'],
            'closed_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        }
        ts = int(datetime.now(timezone.utc).timestamp() * 1000)
        watches = load_state()
        watches.append(watch(shaped, ts, exit_price, reason, trade_id))
        save_state(watches)
    except Exception as exc:                       # noqa: BLE001
        log(f'⚠️ Наблюдение после выхода не заведено: {exc}')


def _live_targets(params):
    """Цели позиции в том же виде, в каком их отдаёт план выхода."""
    from exit_plan import tp_plan
    return tp_plan(params)


def advance_live(fetch_candles=None):
    """
    Один шаг наблюдений боевого пути: догрузить свечи, продвинуть, дописать.

    fetch_candles(pair, since_ms) -> список свечей [ts, o, h, l, c, v]. По
    умолчанию берётся биржевой загрузчик; параметр существует ради проверок,
    которым незачем ходить в сеть.

    Возвращает число ДОСМОТРЕННЫХ наблюдений.
    """
    watches = load_state()
    if not watches:
        return 0

    if fetch_candles is None:
        fetch_candles = _default_fetch

    finished = []
    for pair in {w['pair'] for w in watches if w.get('pair')}:
        mine = [w for w in watches if w['pair'] == pair]
        since = min(w['last_ts'] for w in mine)
        try:
            candles = fetch_candles(pair, since)
        except Exception as exc:                   # noqa: BLE001
            log(f'   {pair}: свечи для наблюдения не получены — {exc}')
            continue
        for candle in candles or []:
            ts, _o, high, low, close = (candle[0], candle[1], candle[2],
                                        candle[3], candle[4])
            finished.extend(advance(mine, pair, ts, high, low, close))

    if finished:
        write([row(w) for w in finished])
        done = {id(w) for w in finished}
        watches = [w for w in watches if id(w) not in done]

    save_state(watches)
    return len(finished)


def _default_fetch(pair, since_ms):
    """Свечи с биржи. Шаг тот же, что у бумажного наблюдения, — 5 минут."""
    import exchange
    # Свечи публичные — клиент без ключей. С ключами в .env их на сервере
    # может и не быть (фантомный счёт), и наблюдение молча падало.
    df = exchange.fetch_ohlcv('5m', limit=200, symbol=pair, since=since_ms,
                              client=exchange.make_market_client())
    if df is None or df.empty:
        return []
    out = []
    for _, r in df.iterrows():
        out.append([int(r['timestamp'].timestamp() * 1000), float(r['open']),
                    float(r['high']), float(r['low']), float(r['close'])])
    return out
