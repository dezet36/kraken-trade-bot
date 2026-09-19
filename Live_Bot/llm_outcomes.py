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

HORIZONS = (1, 4, 12, 24)
_MS_HOUR = 3_600_000

COLUMNS = [
    'mode', 'at', 'pair',
    # Что решила модель: enter — план прошёл все проверки; skip — отказ.
    # gate — имя отказа, включая отказы кода и критика по плану «войти».
    'decision', 'gate', 'side',
    # Куда модель ждала рынок — и при отказе: по этому судят направление.
    'bias',
    # Цена в момент разбора и план, если он был.
    'price', 'entry', 'stop', 'tp1',
    # Ход цены от момента разбора, в процентах, на каждом горизонте.
    'pct_1h', 'pct_4h', 'pct_12h', 'pct_24h',
    'max_up_pct', 'max_down_pct',
    # В долях риска плана — только когда план был: сколько R дала бы идея
    # в лучшем и худшем случае, дошла ли до первой цели, задела ли стоп.
    'best_r', 'worst_r', 'hit_tp1', 'hit_sl',
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
    try:
        with open(STATE_PATH, 'w', encoding='utf-8') as fh:
            json.dump(_watches or [], fh, ensure_ascii=False)
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
        w = {
            'pair': pair,
            'at': at,
            'decision': 'enter' if verdict.get('ok') else 'skip',
            'gate': verdict.get('gate', ''),
            'side': side,
            'bias': verdict.get('bias', ''),
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
            w['high'] = max(w['high'], high)
            w['low'] = min(w['low'], low)
            if w.get('side'):
                is_long = w['side'] == 'LONG'
                if w.get('tp1') is not None:
                    if (high >= w['tp1']) if is_long else (low <= w['tp1']):
                        w['hit_tp1'] = 1
                if w.get('stop') is not None:
                    if (low <= w['stop']) if is_long else (high >= w['stop']):
                        w['hit_sl'] = 1
            hours = (ts - w['start_ts']) / _MS_HOUR
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
        'gate': w['gate'], 'side': w['side'], 'bias': w.get('bias', ''), 'price': p0,
        'entry': w['entry'] if w['entry'] is not None else '',
        'stop': w['stop'] if w['stop'] is not None else '',
        'tp1': w['tp1'] if w['tp1'] is not None else '',
        'max_up_pct': round((w['high'] / p0 - 1) * 100, 3) if p0 else '',
        'max_down_pct': round((w['low'] / p0 - 1) * 100, 3) if p0 else '',
        'best_r': _r(w, w['high'] if w['side'] == 'LONG' else w['low']),
        'worst_r': _r(w, w['low'] if w['side'] == 'LONG' else w['high']),
        'hit_tp1': w['hit_tp1'] if w.get('side') else '',
        'hit_sl': w['hit_sl'] if w.get('side') else '',
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
