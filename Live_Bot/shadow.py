"""
Теневые сделки: сетап, который брокер отклонил своим предохранителем, — и чем
бы он кончился.

ЗАЧЕМ. refused.csv помнит, ЧТО отвергнуто, но не чем бы это кончилось, а без
исхода предохранитель не проверить: может, он отсекает как раз прибыльное.
25.09.2026 SMC за сутки 219 раз не открыла лонги LINK и LTC — у неё уже
стояли три лонга (свой направленный кэп), и узнать, стоил ли этот запрет
денег, было нечем.

КАК СЧИТАЕТСЯ — КАК У БРОКЕРА. Один сетап — одна тень: тот же сетап, предложенный
снова в следующем цикле, только прибавляет счётчик. Лимит — со смещением
стратегии и живёт её срок заявки; налился — цели и доли по её плану выхода
(exit_plan.tp_plan), безубыток после первой цели, если он у неё включён; стоп
внутри одной свечи считается раньше цели. Итог — в R за вычетом издержек круга
(risk_gate.entry_cost_share). Фандинга нет — на доли R оптимистичнее брокера.

НЕ ДЕНЬГИ И НЕ ЖУРНАЛ СДЕЛОК. Отдельные файлы; ни депозит, ни пределы тень не
трогает. Сетап, который позже открыт по-настоящему, помечается и снимается:
его исход — в журнале сделок.
"""

import json
import os
import threading
import time

import config
import csv_journal
from logger import log

STATE_PATH = os.path.join(config.DATA_DIR, 'shadow_state.json')
CSV_PATH = os.path.join(config.DATA_DIR, 'shadow_trades.csv')
HORIZON_H = 168                 # как у наблюдений за вердиктами ИИ
_MS_HOUR = 3_600_000

COLUMNS = [
    'mode', 'strategy', 'pair', 'direction',
    # Чем отвергнут и сколько циклов подряд сетап предлагался снова.
    'gate', 'detail', 'first_at', 'refusals',
    'entry', 'stop', 'targets', 'fractions', 'breakeven', 'cost_share_pct',
    # не налился / цель / стоп / частично / открыта / открыт позже
    'outcome', 'targets_hit', 'result_r', 'best_r', 'worst_r',
    'fill_hours', 'closed_hours',
]

_lock = threading.Lock()
_shadows = None


def _now_ms():
    return int(time.time() * 1000)


def _norm(symbol):
    return (symbol or '').replace('/', '').replace(':USDT', '').replace(':', '').upper()


def _load():
    global _shadows
    if _shadows is None:
        try:
            with open(STATE_PATH, encoding='utf-8') as fh:
                data = json.load(fh)
            _shadows = data if isinstance(data, list) else []
        except (OSError, ValueError):
            _shadows = []
    return _shadows


def _save():
    """Через временный файл и атомарную замену — правило проекта."""
    try:
        tmp = STATE_PATH + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump(_shadows or [], fh, ensure_ascii=False)
        os.replace(tmp, STATE_PATH)
    except OSError as exc:
        log(f'⚠️ Тени отказов не сохранены: {exc}')


def _key(strategy, pair, direction, entry, stop):
    return [strategy, _norm(pair), direction, round(float(entry), 10), round(float(stop), 10)]


def watch(strategy, signal, gate, detail='', now_ms=None):
    """Заводит тень отвергнутого сетапа или прибавляет счётчик уже заведённой. Молча."""
    try:
        import exit_plan
        import risk_gate
        import strategy_profile
        params = (signal or {}).get('params') or {}
        direction = ((signal or {}).get('setup') or {}).get('type', '')
        pair = _norm((signal or {}).get('trading_pair', ''))
        entry, stop = float(params['entry']), float(params['stop_loss'])
        targets, fractions = exit_plan.tp_plan(params)
        if direction not in ('LONG', 'SHORT') or not pair or not targets:
            return
        is_long = direction == 'LONG'
        key = _key(strategy, pair, direction, entry, stop)
        now_ms = now_ms or _now_ms()
        with _lock:
            for s in _load():
                if s['key'] == key:
                    s['refusals'] += 1
                    _save()
                    return
            offset = strategy_profile.limit_offset_pct(strategy)
            limit = entry * (1 + offset) if is_long else entry * (1 - offset)
            if abs(limit - stop) <= 0:
                return
            share = risk_gate.entry_cost_share(limit, abs(limit - stop),
                                               config.ENTRY_COST_ROUND_TRIP) * 100
            _load().append({
                'key': key, 'strategy': strategy, 'pair': pair, 'direction': direction,
                'gate': gate, 'detail': str(detail or '')[:200],
                'first_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now_ms / 1000)),
                'refusals': 1, 'start_ts': now_ms, 'last_ts': now_ms,
                'entry': entry, 'limit': limit, 'stop0': stop, 'stop': stop,
                'targets': targets, 'fractions': fractions,
                'breakeven': bool(exit_plan.wants_breakeven(params)),
                'cost_share_pct': round(share, 3),
                'expiry_h': float(strategy_profile.expiry_hours(strategy)),
                'max_hold_h': float(strategy_profile.max_hold_hours(strategy) or 0),
                'filled': False, 'fill_hours': None, 'hit': 0, 'left': 1.0,
                'realized': 0.0, 'best_r': 0.0, 'worst_r': 0.0,
                'outcome': '', 'closed_hours': None,
            })
            _save()
    except Exception as exc:                           # noqa: BLE001
        log(f'⚠️ Тень отказа не заведена: {exc}')


def opened(strategy, pair, direction, entry, stop):
    """Сетап всё же открыт по-настоящему — тень снимается: исход будет в журнале сделок."""
    try:
        key = _key(strategy, pair, direction, entry, stop)
        with _lock:
            shadows = _load()
            done = [s for s in shadows if s['key'] == key]
            if not done:
                return
            for s in done:
                s['outcome'] = 'открыт позже'
            shadows[:] = [s for s in shadows if s['key'] != key]
            _save()
        write([row(s) for s in done])
    except Exception as exc:                           # noqa: BLE001
        log(f'⚠️ Тень отказа не снята: {exc}')


def pairs():
    """Пары под тенью и самая ранняя метка, с которой нужны свечи."""
    with _lock:
        out = {}
        for s in _load():
            out[s['pair']] = min(out.get(s['pair'], s['last_ts']), s['last_ts'])
        return out


def advance(pair, ts, high, low, close):
    """
    Одна свеча через тени пары. Досмотренные — в файл.

    Свеча, уже виденная (ts не позже last_ts), пропускается: брокер зовёт это
    по разу на стратегию, и без проверки одна свеча считалась бы пять раз.
    """
    finished = []
    with _lock:
        shadows = _load()
        for s in shadows:
            if s['pair'] != pair or ts <= s['last_ts']:
                continue
            s['last_ts'], s['last_close'] = ts, close
            _step(s, high, low, close, (ts - s['start_ts']) / _MS_HOUR)
            if s['outcome']:
                finished.append(s)
        if finished:
            done = {id(s) for s in finished}
            shadows[:] = [s for s in shadows if id(s) not in done]
        if finished or any(s['pair'] == pair for s in shadows):
            _save()
    if finished:
        write([row(s) for s in finished])
    return finished


def _step(s, high, low, close, hours):
    is_long = s['direction'] == 'LONG'
    if not s['filled']:
        if hours > s['expiry_h']:
            s['outcome'], s['closed_hours'] = 'не налился', round(hours, 2)
            return
        if not ((low <= s['limit']) if is_long else (high >= s['limit'])):
            return
        s['filled'], s['fill_hours'] = True, round(hours, 2)

    entry = s['limit']
    risk = abs(entry - s['stop0'])
    s['best_r'] = max(s['best_r'], ((high - entry) if is_long else (entry - low)) / risk)
    s['worst_r'] = min(s['worst_r'], ((low - entry) if is_long else (entry - high)) / risk)

    if (low <= s['stop']) if is_long else (high >= s['stop']):
        s['realized'] += s['left'] * ((s['stop'] - entry) if is_long else (entry - s['stop'])) / risk
        s['left'] = 0.0
        s['outcome'] = 'частично' if s['hit'] else 'стоп'
        s['closed_hours'] = round(hours, 2)
        return

    for i, target in enumerate(s['targets']):
        if i < s['hit']:
            continue
        if not ((high >= target) if is_long else (low <= target)):
            break
        s['realized'] += s['fractions'][i] * abs(target - entry) / risk
        s['left'] -= s['fractions'][i]
        s['hit'] = i + 1
        if s['hit'] == 1 and s['breakeven']:
            import exit_plan
            s['stop'] = exit_plan.breakeven_price(entry, is_long)

    if s['hit'] == len(s['targets']) or s['left'] <= 1e-9:
        s['left'] = 0.0
        s['outcome'], s['closed_hours'] = 'цель', round(hours, 2)
        return

    held = hours - (s['fill_hours'] or 0.0)
    if hours >= HORIZON_H or (s['max_hold_h'] and held >= s['max_hold_h']):
        s['realized'] += s['left'] * ((close - entry) if is_long else (entry - close)) / risk
        s['left'] = 0.0
        s['outcome'], s['closed_hours'] = 'открыта', round(hours, 2)


def result_r(s):
    """Итог в R за вычетом издержек круга; не налившаяся — ноль."""
    if not s.get('filled'):
        return 0.0
    return round(s['realized'] - s['cost_share_pct'] / 100, 3)


def row(s):
    return {
        'strategy': s['strategy'], 'pair': s['pair'], 'direction': s['direction'],
        'gate': s['gate'], 'detail': s['detail'], 'first_at': s['first_at'],
        'refusals': s['refusals'], 'entry': s['entry'], 'stop': s['stop0'],
        'targets': json.dumps(s['targets']), 'fractions': json.dumps(s['fractions']),
        'breakeven': int(bool(s['breakeven'])), 'cost_share_pct': s['cost_share_pct'],
        'outcome': s['outcome'], 'targets_hit': s['hit'], 'result_r': result_r(s),
        'best_r': round(s['best_r'], 3), 'worst_r': round(s['worst_r'], 3),
        'fill_hours': s['fill_hours'] if s['fill_hours'] is not None else '',
        'closed_hours': s['closed_hours'] if s['closed_hours'] is not None else '',
    }


def write(rows):
    if not rows:
        return
    stamped = [{'mode': config.TRADING_MODE, **r} for r in rows]
    csv_journal.append(CSV_PATH, COLUMNS, stamped, 'тени отказов')


def snapshot(price_of=None, closed_limit=400):
    """
    Для панели: тени, живые сейчас, и итог досмотренных по стратегиям и воротам.

    price_of(pair) -> цена или None; без него — последнее закрытие, что видела тень.
    """
    with _lock:
        active = [dict(s) for s in _load()]
    live = []
    for s in active:
        price = price_of(s['pair']) if price_of else s.get('last_close')
        entry = s['limit']
        risk = abs(entry - s['stop0']) or 1.0
        now_r = None
        if s['filled'] and price:
            move = (price - entry) if s['direction'] == 'LONG' else (entry - price)
            now_r = round(s['realized'] + s['left'] * move / risk - s['cost_share_pct'] / 100, 2)
        live.append({'strategy': s['strategy'], 'pair': s['pair'], 'direction': s['direction'],
                     'gate': s['gate'], 'first_at': s['first_at'], 'refusals': s['refusals'],
                     'entry': s['entry'], 'stop': s['stop0'], 'target': s['targets'][0],
                     'status': 'в позиции' if s['filled'] else 'ждёт входа',
                     'targets_hit': s['hit'], 'now_r': now_r})
    summary = {}
    try:
        import csv
        with open(CSV_PATH, encoding='utf-8', newline='') as fh:
            rows = [r for r in csv.DictReader(fh) if r.get('mode') == config.TRADING_MODE]
        for r in rows[-closed_limit:]:
            cell = summary.setdefault(r['strategy'], {}).setdefault(
                r['gate'], {'n': 0, 'entered': 0, 'wins': 0, 'losses': 0, 'sum_r': 0.0})
            cell['n'] += 1
            if r['outcome'] in ('цель', 'стоп', 'частично', 'открыта'):
                cell['entered'] += 1
                value = float(r['result_r'] or 0)
                cell['sum_r'] = round(cell['sum_r'] + value, 3)
                cell['wins' if value > 0 else 'losses'] += 1
    except (OSError, ValueError, KeyError):
        pass
    return {'active': live, 'closed': summary}
