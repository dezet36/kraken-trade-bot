"""
Теневые сделки: сетап, который брокер отклонил своим предохранителем, — и чем
бы он кончился.

ЗАЧЕМ. refused.csv помнит, ЧТО отвергнуто, но не чем бы это кончилось, а без
исхода предохранитель не проверить: может, он отсекает как раз прибыльное.
25.09.2026 SMC за сутки 219 раз не открыла лонги LINK и LTC — у неё уже
стояли три лонга (свой направленный кэп), и узнать, стоил ли этот запрет
денег, было нечем.

КАК СЧИТАЕТСЯ — КАК У БРОКЕРА, ЕСЛИ БЫ ЗАЯВКУ ПОСТАВИЛИ. Один сетап — одна
тень: тот же сетап, предложенный снова в следующем цикле, только прибавляет
счётчик. Лимит — со смещением стратегии и живёт её срок заявки; вход по ходу
движения (стоп-заявка уровней) наливается пробоем, а при разрыве — по открытию
свечи. Цена дошла до первой цели, не задев входа, — «цель без входа»: брокер
такую заявку снимает (кроме SMC — у неё заявка ждёт весь срок, см.
strategy_profile.drops_at_target). Налился — цели и доли по плану выхода стратегии
(exit_plan.tp_plan), безубыток после первой цели, если он у неё включён; стоп
внутри одной свечи считается раньше цели. Итог — в R от задуманного риска за
вычетом издержек круга (risk_gate.entry_cost_share). Фандинга нет — на доли R
оптимистичнее брокера.

Пока по паре стратегии живёт тень, новый сетап по той же паре тени не
получает: брокер ответил бы «уже есть заявка». После тени — кулдаун стратегии
от постановки, а у налившейся — и от выхода, как брокер ставит его в open() и
_close(). Без этого закрытая «цель без входа» тут же заводилась бы заново: SMC
предлагает тот же сетап каждые пять минут.

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

# Версия правил. 2 (25.09.2026): цель без входа, вход пробоем, одна тень на
# пару, кулдаун. Тени по прежним правилам переигрываются с начала (_migrate):
# LTC и LINK SMC до этого «ждали входа», уйдя за свою первую цель.
RULES = 2

COLUMNS = [
    'mode', 'strategy', 'pair', 'direction',
    # Чем отвергнут и сколько циклов подряд сетап предлагался снова.
    'gate', 'detail', 'first_at', 'refusals',
    'entry', 'stop', 'targets', 'fractions', 'breakeven', 'cost_share_pct',
    # не налился / цель без входа / цель / стоп / частично / открыта / открыт позже
    'outcome', 'targets_hit', 'result_r', 'best_r', 'worst_r',
    'fill_hours', 'closed_hours',
    # Пока ждала входа: ближе всего ко входу (% от входа, >0 — не дошла) и
    # ход к цели без нас (R от входа) — те же числа, что у снятой заявки.
    'min_gap_pct', 'best_run_r',
]

_lock = threading.Lock()
_shadows = None
_cooldown = {}          # 'SMC|LTCUSDT' -> до какой отметки (мс) новой тени по паре нет


def _now_ms():
    return int(time.time() * 1000)


def _norm(symbol):
    return (symbol or '').replace('/', '').replace(':USDT', '').replace(':', '').upper()


def _load():
    global _shadows, _cooldown
    if _shadows is None:
        try:
            with open(STATE_PATH, encoding='utf-8') as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            data = []
        # До 25.09.2026 файл был списком теней; теперь в нём и кулдауны пар.
        if isinstance(data, dict):
            _shadows = [s for s in data.get('shadows') or [] if isinstance(s, dict)]
            _cooldown = {str(k): int(v) for k, v in (data.get('cooldown') or {}).items()}
        else:
            _shadows = [s for s in data if isinstance(s, dict)] if isinstance(data, list) else []
            _cooldown = {}
        if _migrate(_shadows):
            _save()
    return _shadows


def _save():
    """Через временный файл и атомарную замену — правило проекта."""
    try:
        tmp = STATE_PATH + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump({'shadows': _shadows or [], 'cooldown': _cooldown}, fh, ensure_ascii=False)
        os.replace(tmp, STATE_PATH)
    except OSError as exc:
        log(f'⚠️ Тени отказов не сохранены: {exc}')


def _cooldown_hours(strategy):
    try:
        import strategy_profile
        return float(strategy_profile.cooldown_hours(strategy) or 0)
    except Exception:                                  # noqa: BLE001
        return 0.0


def _hold(strategy, pair, until_ms):
    """Новой тени по паре стратегии не заводить до until_ms (кулдаун брокера)."""
    key = f'{strategy}|{pair}'
    _cooldown[key] = max(int(until_ms), _cooldown.get(key, 0))


def _drops_at_target(strategy):
    try:
        import strategy_profile
        return bool(strategy_profile.drops_at_target(strategy))
    except Exception:                                  # noqa: BLE001
        return True


def _migrate(shadows):
    """
    Тени, заведённые по прежним правилам, переигрываются с начала: состояние —
    как в момент отказа, last_ts — на start_ts, и брокер сам отдаст свечи с этой
    отметки (pairs() берёт last_ts). Свечи, уже виденные позициями и
    наблюдениями, те пропускают — повтор безвреден. -> были ли такие тени.

    Живой тени правил 2 без флага «снимать у цели» флаг просто проставляется:
    раз она жива, цели без входа у неё не было, и переигрывать нечего.
    """
    changed = False
    for s in shadows:
        if 'drops_at_target' not in s:
            s['drops_at_target'] = _drops_at_target(s['strategy'])
            changed = True
        if s.get('rules') == RULES:
            continue
        s.update(filled=False, fill_hours=None, hit=0, left=1.0, realized=0.0,
                 best_r=0.0, worst_r=0.0, stop=s.get('stop0', s.get('stop')), outcome='',
                 closed_hours=None, last_ts=s['start_ts'], rules=RULES)
        for key in ('last_close', 'min_gap_pct', 'best_run_r', 'fill_price'):
            s.pop(key, None)
        s.setdefault('stop_entry', False)
        if 'cooldown_h' not in s:
            s['cooldown_h'] = _cooldown_hours(s['strategy'])
        _hold(s['strategy'], s['pair'], s['start_ts'] + int(s['cooldown_h'] * _MS_HOUR))
        changed = True
    return changed


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
            shadows = _load()
            for s in shadows:
                if s['key'] == key:
                    s['refusals'] += 1
                    _save()
                    return
            # По паре уже живёт тень — брокер ответил бы «уже есть заявка».
            if any(s['strategy'] == strategy and s['pair'] == pair for s in shadows):
                return
            hold = f'{strategy}|{pair}'
            if _cooldown.get(hold, 0) > now_ms:
                return
            _cooldown.pop(hold, None)
            offset = strategy_profile.limit_offset_pct(strategy)
            limit = entry * (1 + offset) if is_long else entry * (1 - offset)
            if abs(limit - stop) <= 0:
                return
            share = risk_gate.entry_cost_share(limit, abs(limit - stop),
                                               config.ENTRY_COST_ROUND_TRIP) * 100
            cooldown_h = _cooldown_hours(strategy)
            shadows.append({
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
                'cooldown_h': cooldown_h,
                # Тип входа — у стратегии, как у брокера (_process_pending).
                'stop_entry': str(((signal or {}).get('trigger') or {}).get('entry_type', '')
                                  ).upper() in ('MARKET', 'STOP'),
                # Снимать ли у цели без входа — тоже у стратегии (у SMC — нет).
                'drops_at_target': _drops_at_target(strategy),
                'filled': False, 'fill_hours': None, 'hit': 0, 'left': 1.0,
                'realized': 0.0, 'best_r': 0.0, 'worst_r': 0.0,
                'outcome': '', 'closed_hours': None, 'rules': RULES,
            })
            # Кулдаун с постановки — как брокер ставит его при выставлении лимита.
            _hold(strategy, pair, now_ms + int(cooldown_h * _MS_HOUR))
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


def advance(pair, ts, high, low, close, open_price=None):
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
            _step(s, high, low, close, (ts - s['start_ts']) / _MS_HOUR, open_price)
            if s['outcome']:
                finished.append(s)
                if s['filled']:
                    # Кулдаун и от выхода — как брокер в _close().
                    _hold(s['strategy'], s['pair'], ts + int(s.get('cooldown_h', 0) * _MS_HOUR))
        if finished:
            done = {id(s) for s in finished}
            shadows[:] = [s for s in shadows if id(s) not in done]
        if finished or any(s['pair'] == pair for s in shadows):
            _save()
    if finished:
        write([row(s) for s in finished])
    return finished


def _step(s, high, low, close, hours, open_price=None):
    is_long = s['direction'] == 'LONG'
    if not s['filled']:
        if hours > s['expiry_h']:
            s['outcome'], s['closed_hours'] = 'не налился', round(hours, 2)
            return
        limit, stop_entry = s['limit'], bool(s.get('stop_entry'))
        if stop_entry:
            filled = (high >= limit) if is_long else (low <= limit)
        else:
            filled = (low <= limit) if is_long else (high >= limit)
        if not filled:
            # Как близко подошла цена и сколько ушла без нас — как у брокера.
            risk = abs(limit - s['stop0'])
            gap = ((low - limit) if is_long != stop_entry else (limit - high)) / limit * 100
            run = max(0.0, ((high - limit) if is_long else (limit - low)) / risk) if risk else 0.0
            s['min_gap_pct'] = round(min(s.get('min_gap_pct', gap), gap), 4)
            s['best_run_r'] = round(max(s.get('best_run_r', run), run), 3)
            # ЦЕЛЬ БЕЗ ВХОДА: брокер снимает такую заявку («цена дошла до цели
            # без нас») — если стратегия это правило признаёт. SMC его не
            # признаёт: заявка ждёт отката весь срок, как в её бэктесте.
            target = s['targets'][0]
            if s.get('drops_at_target', True) and ((high >= target) if is_long else (low <= target)):
                s['outcome'], s['closed_hours'] = 'цель без входа', round(hours, 2)
            return
        s['filled'], s['fill_hours'] = True, round(hours, 2)
        # Разрыв через уровень: стоп-заявка исполняется по открытию свечи.
        if stop_entry and open_price is not None and (
                (open_price > limit) if is_long else (open_price < limit)):
            s['fill_price'] = open_price

    # Итог — в R от ЗАДУМАННОГО риска (от лимита до стопа), движение — от
    # цены входа: при разрыве брокер тоже теряет больше одного R.
    entry = s.get('fill_price', s['limit'])
    risk = abs(s['limit'] - s['stop0'])
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
        'min_gap_pct': s.get('min_gap_pct', ''),
        'best_run_r': s.get('best_run_r', ''),
    }


def write(rows):
    if not rows:
        return
    stamped = [{'mode': config.TRADING_MODE, **r} for r in rows]
    csv_journal.append(CSV_PATH, COLUMNS, stamped, 'тени отказов')


def running_rows():
    """Тени, ещё не досмотренные, — строками файла с исходом «идёт»."""
    with _lock:
        active = [dict(s) for s in _load()]
    return [{'mode': config.TRADING_MODE, **row(s), 'outcome': 'идёт'} for s in active]


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
        entry = s.get('fill_price', s['limit'])
        risk = abs(s['limit'] - s['stop0']) or 1.0
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
                r['gate'], {'n': 0, 'entered': 0, 'wins': 0, 'losses': 0, 'sum_r': 0.0,
                            'missed': 0})
            cell['n'] += 1
            if r['outcome'] == 'цель без входа':
                cell['missed'] += 1
            if r['outcome'] in ('цель', 'стоп', 'частично', 'открыта'):
                cell['entered'] += 1
                value = float(r['result_r'] or 0)
                cell['sum_r'] = round(cell['sum_r'] + value, 3)
                cell['wins' if value > 0 else 'losses'] += 1
    except (OSError, ValueError, KeyError):
        pass
    return {'active': live, 'closed': summary}
