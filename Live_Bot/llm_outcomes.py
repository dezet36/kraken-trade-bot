"""
Что цена делала ПОСЛЕ каждого вердикта модели — вход это был или отказ.

ЗАЧЕМ. Журнал разборов хранит, что модель сказала, и молчит о том, была ли
она права. А это единственный вопрос, ради которого стратегия существует:
разбор читается убедительно при любом решении — и при верном, и при
ошибочном, — и по тексту их не различить. Различить можно только по тому,
куда пошла цена.

ЧТО ЗАПИСЫВАЕТСЯ. Для каждого вердикта — цена в момент разбора и её ход на
горизонтах 1, 4, 12 и 24 часа, максимум вверх и вниз. Если модель называла
направление (вход, или вход, отвергнутый проверками кода или критиком), то
ещё и в долях её же риска: дошла бы сделка до первой цели, снесло бы её по
стопу. Так через неделю видно четыре клетки: вошли и выиграли, вошли и
проиграли, отказали и цена ушла без нас, отказали правильно.

ОТДЕЛЬНЫМ ФАЙЛОМ, А НЕ КОЛОНКАМИ В ЖУРНАЛЕ РАЗБОРОВ. Наблюдение длится сутки,
строку пришлось бы дописывать задним числом, а журнал пишется только вперёд.
Тот же довод, что у follow_up для сделок.

СОСТОЯНИЕ В ФАЙЛЕ. Наблюдение живёт сутки, бот за это время перезапускается.
В памяти доживали бы только те, которым повезло с простоем, и выборка была
бы смещена в сторону спокойных периодов.

СВЕЧИ ПРИНОСИТ БУМАЖНЫЙ БРОКЕР: он и так запрашивает пятиминутки по парам
под наблюдением и прогоняет их через follow_up. Здесь тот же приём — пары
под наблюдением добавляются к его списку, свечи приходят в advance.
"""

import json
import os
import threading

import config
import csv_journal
from logger import log

CSV_PATH = os.path.join(config.DATA_DIR, 'llm_outcomes.csv')
STATE_PATH = os.path.join(config.DATA_DIR, 'llm_outcomes_state.json')

HORIZONS = (1, 4, 12, 24, 48)
_MS_HOUR = 3_600_000
# Касание уровня — цена в этой доле процента от него; реакция считается в ATR.
LEVEL_TOUCH_PCT = 0.1

COLUMNS = [
    'mode', 'at', 'pair',
    # Что решила модель: enter — план прошёл все проверки; skip — отказ.
    # gate — имя отказа, включая отказы кода и критика по плану «войти».
    'decision', 'gate', 'side',
    # Куда модель ждала рынок — и при отказе: по этому судят направление.
    'bias', 'data_gap_bars',
    # Цена в момент разбора и план, если он был.
    'price', 'entry', 'stop', 'tp1',
    # Ход цены от момента разбора, в процентах, на каждом горизонте.
    'pct_1h', 'pct_4h', 'pct_12h', 'pct_24h',
    'max_up_pct', 'max_down_pct',
    # В долях риска плана — только когда план был: сколько R дала бы идея
    # в лучшем и худшем случае, дошла ли до первой цели, задела ли стоп.
    'best_r', 'worst_r', 'hit_tp1', 'hit_sl',
    # Глубже: КОГДА дошла до цели и до стопа (часы от разбора); коснулась
    # ли цена входа и когда; ближе всего к входу (в % входа, >0 — не дошла);
    # ход к цели без нас (в R), пока вход не дан; когда наступило бы
    # условие модели по закрытым часовым свечам.
    'tp_hours', 'sl_hours', 'entry_touched', 'entry_hours', 'min_dist_entry_pct',
    'missed_move_r', 'trigger_when', 'cond_hours',
    # Каждый уровень списка: коснулась ли цена и на сколько ATR отошла
    # после касания — оценка генератора уровней, не модели. JSON.
    'levels_hit',
    'observed_hours',
]

_lock = threading.Lock()
_watches = None


def _load():
    global _watches
    if _watches is None:
        try:
            with open(STATE_PATH, encoding='utf-8') as fh:
                data = json.load(fh)
            _watches = data if isinstance(data, list) else []
        except (OSError, ValueError):
            _watches = []
    return _watches


def _save():
    """
    Через временный файл и атомарную замену: прямая запись оставляла окно,
    в котором файл пуст — читатель (и перезапуск бота) в этот момент видел
    ноль наблюдений вместо сорока (21.09.2026, поймано при проверке).
    """
    try:
        tmp = STATE_PATH + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump(_watches or [], fh, ensure_ascii=False)
        os.replace(tmp, STATE_PATH)
    except OSError as exc:
        log(f'⚠️ Наблюдения за вердиктами не сохранены: {exc}')


def watch(pair, verdict, price, ts, at=''):
    """
    Заводит наблюдение по вердикту. Молча: наблюдение не мешает торговле.

    Поломки («ответ обрезан», «модель недоступна») не наблюдаются: это не
    решения, а неисправности, и по ним нечего проверять.
    """
    try:
        import llm_decide
        if verdict.get('gate') in llm_decide.BROKEN_GATES:
            return
        side = verdict.get('side') if verdict.get('side') in ('LONG', 'SHORT') else ''
        entry = verdict.get('entry') if side else None
        stop = verdict.get('stop') if side else None
        targets = verdict.get('targets') or []
        atr_pct = float(verdict.get('atr_pct') or 0)
        w = {
            'pair': pair,
            'at': at,
            'decision': 'enter' if verdict.get('ok') else 'skip',
            'gate': verdict.get('gate', ''),
            'side': side,
            'bias': verdict.get('bias', ''),
            'data_gap_bars': int(verdict.get('data_gap_bars') or 0),
            'price': float(price),
            'entry': float(entry) if entry else None,
            'stop': float(stop) if stop else None,
            'tp1': float(targets[0]) if targets else None,
            'start_ts': int(ts),
            'last_ts': int(ts),
            'high': float(price),
            'low': float(price),
            'marks': {},
            'hit_tp1': 0,
            'hit_sl': 0,
            'tp_hours': None, 'sl_hours': None,
            'entry_touched': 0, 'entry_hours': None,
            'min_dist_entry_pct': None, 'missed_move_r': None,
            'trigger_when': (verdict.get('trigger_when') or 'now') if side else '',
            'trigger_level': float(verdict['trigger_level']) if side and verdict.get('trigger_level') else None,
            'cond_hours': None,
            # Часовые свечи после разбора — из пятиминуток брокера; по ним
            # считается условие модели.
            'hour_bars': [], 'cur_hour': None,
            'atr': float(price) * atr_pct / 100 if atr_pct else None,
            'levels': [{'id': lv.get('id'), 'price': float(lv.get('price')), 'kind': lv.get('kind', ''),
                        'touched_h': None, 'react_atr': None, 'from_above': None}
                       for lv in (verdict.get('levels') or []) if lv.get('price')],
        }
        with _lock:
            _load().append(w)
            _save()
    except Exception as exc:                           # noqa: BLE001
        log(f'⚠️ Наблюдение за вердиктом {pair} не заведено: {exc}')


def pairs():
    """Пары под наблюдением и самая ранняя метка, с которой нужны свечи."""
    with _lock:
        out = {}
        for w in _load():
            out[w['pair']] = min(out.get(w['pair'], w['last_ts']), w['last_ts'])
        return out


def advance(pair, ts, high, low, close):
    """
    Прогоняет одну свечу через наблюдения по паре. Досмотренные — пишет.

    Свеча, уже виденная (ts не позже last_ts), пропускается: брокер зовёт
    это по разу на стратегию, и без этого одна свеча считалась бы пять раз.
    """
    finished = []
    with _lock:
        watches = _load()
        for w in watches:
            if w['pair'] != pair or ts <= w['last_ts']:
                continue
            w['last_ts'] = ts
            w['last_close'] = close
            w['high'] = max(w['high'], high)
            w['low'] = min(w['low'], low)
            hours = (ts - w['start_ts']) / _MS_HOUR
            if w.get('side'):
                _advance_plan(w, ts, high, low, close, hours)
            _advance_levels(w, high, low, close)
            for h in HORIZONS:
                if hours >= h and str(h) not in w['marks']:
                    w['marks'][str(h)] = close
            if hours >= HORIZONS[-1]:
                finished.append(w)
        if finished:
            done = {id(w) for w in finished}
            _watches[:] = [w for w in watches if id(w) not in done]
        if finished or any(w['pair'] == pair for w in watches):
            _save()
    if finished:
        write([row(w) for w in finished])
    return finished


def _advance_plan(w, ts, high, low, close, hours):
    """Цель, стоп, вход и условие плана — по одной свече."""
    is_long = w['side'] == 'LONG'
    entry, stop, tp1 = w.get('entry'), w.get('stop'), w.get('tp1')
    if tp1 is not None and not w['hit_tp1']:
        if (high >= tp1) if is_long else (low <= tp1):
            w['hit_tp1'] = 1
            w['tp_hours'] = round(hours, 2)
    if stop is not None and not w['hit_sl']:
        if (low <= stop) if is_long else (high >= stop):
            w['hit_sl'] = 1
            w['sl_hours'] = round(hours, 2)
    if entry:
        # Ближе всего к входу: для лонга — насколько минимум выше входа.
        gap = ((low - entry) if is_long else (entry - high)) / entry * 100
        if w.get('min_dist_entry_pct') is None or gap < w['min_dist_entry_pct']:
            w['min_dist_entry_pct'] = round(gap, 4)
        if not w.get('entry_touched'):
            if gap <= 0:
                w['entry_touched'] = 1
                w['entry_hours'] = round(hours, 2)
            elif stop is not None and abs(entry - stop):
                # Ход к цели БЕЗ нас — в R от входа: сколько движения ушло,
                # пока вход не был дан (SUI и LTC 21.09: цель без входа).
                favorable = ((high - entry) if is_long else (entry - low)) / abs(entry - stop)
                w['missed_move_r'] = round(max(w.get('missed_move_r') or 0.0, favorable), 3)
    # Часовые свечи для условия: пятиминутки складываются по часу UTC.
    if w.get('trigger_when') and w['trigger_when'] != 'now' and w.get('cond_hours') is None:
        hour = ts - ts % _MS_HOUR
        cur = w.get('cur_hour')
        if cur is None or cur['ts'] != hour:
            if cur is not None:
                w['hour_bars'].append(cur)
                w['hour_bars'] = w['hour_bars'][-50:]      # 48 ч наблюдения + запас
                _try_condition(w, hours)
            w['cur_hour'] = {'ts': hour, 'o': close, 'h': high, 'l': low, 'c': close, 'v': 0.0}
        else:
            cur['h'] = max(cur['h'], high)
            cur['l'] = min(cur['l'], low)
            cur['c'] = close


def _try_condition(w, hours):
    """Наступило ли условие модели по накопленным часовым свечам."""
    try:
        import strategy_llm
        bars = [(b['ts'] + _MS_HOUR, b['o'], b['h'], b['l'], b['c'], b['v']) for b in w['hour_bars']]
        if not bars or w.get('trigger_level') is None:
            return
        # Объёма у собранных из пятиминуток свечей нет: условия «с объёмом»
        # считаются здесь как без объёма — медиана 0 делает порог нулевым.
        met = strategy_llm.condition_met(w['trigger_when'], w['trigger_level'], w['side'], bars, 0.0)
        if met:
            w['cond_hours'] = round(hours, 2)
    except Exception:                              # noqa: BLE001
        pass


def _advance_levels(w, high, low, close):
    """Каждый уровень: первое касание и отход после него в ATR."""
    atr = w.get('atr')
    for lv in w.get('levels') or ():
        price = lv['price']
        tol = price * LEVEL_TOUCH_PCT / 100
        if lv['touched_h'] is None:
            if low - tol <= price <= high + tol:
                lv['touched_h'] = round((w['last_ts'] - w['start_ts']) / _MS_HOUR, 2)
                lv['from_above'] = bool(w['price'] > price)
                lv['react_atr'] = 0.0
            continue
        if atr:
            # Отход в сторону, откуда пришли: подошли сверху — отскок вверх.
            away = (high - price) if lv['from_above'] else (price - low)
            lv['react_atr'] = round(max(lv['react_atr'] or 0.0, away / atr), 2)


def recent(pair, limit=2):
    """
    Что случилось после последних вердиктов по паре — и идущие наблюдения,
    и досмотренные из файла. Свежие впереди. Для разметки: модель видит,
    подтвердил ли рынок её прошлую мысль.
    """
    out = []
    with _lock:
        for w in _load():
            if w['pair'] != pair or w.get('last_close') is None or not w.get('price'):
                continue
            out.append({
                'at': w.get('at', ''), 'side': w.get('side', ''), 'decision': w['decision'],
                'pct': (w['last_close'] / w['price'] - 1) * 100,
                'max_up': (w['high'] / w['price'] - 1) * 100,
                'max_down': (w['low'] / w['price'] - 1) * 100,
                'hit_tp1': bool(w.get('hit_tp1')), 'hit_sl': bool(w.get('hit_sl')),
                'hours': (w['last_ts'] - w['start_ts']) / _MS_HOUR, 'done': False,
            })
    try:
        import csv
        with open(CSV_PATH, encoding='utf-8', newline='') as fh:
            rows = [r for r in csv.DictReader(fh) if r.get('pair') == pair]
        for r in rows[-limit:]:
            pct = r.get('pct_24h') or r.get('pct_12h') or r.get('pct_4h') or ''
            out.append({
                'at': r.get('at', ''), 'side': r.get('side', ''), 'decision': r.get('decision', ''),
                'pct': float(pct) if pct != '' else None,
                'max_up': float(r['max_up_pct']) if r.get('max_up_pct') else None,
                'max_down': float(r['max_down_pct']) if r.get('max_down_pct') else None,
                'hit_tp1': r.get('hit_tp1') == '1', 'hit_sl': r.get('hit_sl') == '1',
                'hours': float(r.get('observed_hours') or 0), 'done': True,
            })
    except FileNotFoundError:
        pass
    except Exception:                              # noqa: BLE001
        pass
    out.sort(key=lambda o: o['at'], reverse=True)
    return out[:limit]


def expire(now_ms):
    """Заброшенные наблюдения (пара выпала из пула) закрываются по времени."""
    cutoff = now_ms - HORIZONS[-1] * 4 * _MS_HOUR
    with _lock:
        watches = _load()
        stale = [w for w in watches if w['start_ts'] < cutoff]
        if stale:
            keep = {id(w) for w in stale}
            _watches[:] = [w for w in watches if id(w) not in keep]
            _save()
    if stale:
        write([row(w) for w in stale])
    return stale


def _r(w, price):
    """Цена в долях риска плана, со знаком направления. Пусто без плана."""
    if not w.get('side') or w.get('entry') is None or w.get('stop') is None:
        return ''
    dist = abs(w['entry'] - w['stop'])
    if not dist:
        return ''
    sign = 1 if w['side'] == 'LONG' else -1
    return round(sign * (price - w['entry']) / dist, 3)


def row(w):
    p0 = w['price']
    hours = (w['last_ts'] - w['start_ts']) / _MS_HOUR
    out = {
        'at': w.get('at', ''), 'pair': w['pair'], 'decision': w['decision'],
        'gate': w['gate'], 'side': w['side'], 'bias': w.get('bias', ''),
        'data_gap_bars': w.get('data_gap_bars', 0), 'price': p0,
        'entry': w['entry'] if w['entry'] is not None else '',
        'stop': w['stop'] if w['stop'] is not None else '',
        'tp1': w['tp1'] if w['tp1'] is not None else '',
        'max_up_pct': round((w['high'] / p0 - 1) * 100, 3) if p0 else '',
        'max_down_pct': round((w['low'] / p0 - 1) * 100, 3) if p0 else '',
        'best_r': _r(w, w['high'] if w['side'] == 'LONG' else w['low']),
        'worst_r': _r(w, w['low'] if w['side'] == 'LONG' else w['high']),
        'hit_tp1': w['hit_tp1'] if w.get('side') else '',
        'hit_sl': w['hit_sl'] if w.get('side') else '',
        'tp_hours': w.get('tp_hours') if w.get('tp_hours') is not None else '',
        'sl_hours': w.get('sl_hours') if w.get('sl_hours') is not None else '',
        'entry_touched': w.get('entry_touched', 0) if w.get('side') else '',
        'entry_hours': w.get('entry_hours') if w.get('entry_hours') is not None else '',
        'min_dist_entry_pct': w.get('min_dist_entry_pct') if w.get('min_dist_entry_pct') is not None else '',
        'missed_move_r': (w['missed_move_r'] if w.get('missed_move_r') is not None and not w.get('entry_touched') else ''),
        'trigger_when': w.get('trigger_when', ''),
        'cond_hours': w.get('cond_hours') if w.get('cond_hours') is not None else '',
        'levels_hit': json.dumps([{'id': lv['id'], 'kind': lv['kind'].split(' / ')[0], 'p': lv['price'],
                                   'touched_h': lv['touched_h'], 'react_atr': lv['react_atr']}
                                  for lv in (w.get('levels') or [])], ensure_ascii=False),
        'observed_hours': round(hours, 1),
    }
    for h in HORIZONS:
        mark = w['marks'].get(str(h))
        out[f'pct_{h}h'] = (round((mark / p0 - 1) * 100, 3)
                            if mark is not None and p0 else '')
    return out


def write(rows):
    if not rows:
        return
    stamped = [{'mode': config.TRADING_MODE, **r} for r in rows]
    csv_journal.append(CSV_PATH, COLUMNS, stamped, 'исходы вердиктов модели')
