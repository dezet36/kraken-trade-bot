"""
Что модель видела — числами и текстом — на каждый разбор.

ЗАЧЕМ. Журнал разборов хранит, что модель сказала; исходы — что сделала
цена. Между ними не хватало третьего: ЧТО МОДЕЛЬ ВИДЕЛА. Без этого через
месяц нельзя ответить «на каких данных она ошибалась», нельзя обучить
фильтр планов и нельзя отличить ошибку модели от дыры в данных.

ДВА ФАЙЛА. Разметка целиком — текстом на каждый разбор
(bot_data/llm_markup/<время>_<пара>.txt, ~7 КБ): человек читает её как
модель. Признаки — строкой JSON в bot_data/llm_features.jsonl: плоская
таблица чисел, к которой потом присоединяются исходы по ключу (pair, at).
Всё, что не записано сегодня, потеряно навсегда — поэтому записываем и
поломки, и отказы: у них те же признаки.

ФЛАГИ КАЧЕСТВА ДАННЫХ — часть признаков: какие блоки снимка были пусты,
возраст дельты и рынка в целом, дыра в свечах, время разбора по фазам. Без
них плохой исход нельзя отнести к плохим данным, и виноватой выйдет модель.
"""

import json
import math
import os
import re
from datetime import datetime, timezone

import config
from logger import log

MARKUP_DIR = os.path.join(config.DATA_DIR, 'llm_markup')
FEATURES_PATH = os.path.join(config.DATA_DIR, 'llm_features.jsonl')

# Блоки снимка, чьё отсутствие — флаг качества данных.
SNAPSHOT_BLOCKS = ('profile', 'absorption', 'delta', 'book', 'smc', 'pois', 'htf', 'htf_zones',
                   'oi_flow', 'liquidations', 'liq_fact', 'tape', 'benchmark', 'sessions',
                   'activity', 'fvgs', 'delta_hours', 'funding_trend', 'oi_week', 'day_profile', 'macro')

SESSIONS = (('ASIA', 0, 5), ('LONDON', 7, 10), ('NY', 12, 15), ('LONDON_CLOSE', 15, 17))


def _num(value, digits=4):
    """Число или None: NaN, строки и None не должны попасть в таблицу."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return round(f, digits)


def _pct(a, b):
    a, b = _num(a), _num(b)
    if a is None or b is None or not b:
        return None
    return round((a / b - 1) * 100, 4)


def session_of(hour):
    for name, start, end in SESSIONS:
        if start <= hour < end:
            return name
    return 'OFF'


def features(pair, df, facts, market, verdict, stats=None, now=None, at=''):
    """
    Плоский словарь признаков одного разбора. Ничего не бросает: пропавший
    кусок — None, а не отсутствие строки.
    """
    now = now or datetime.now(timezone.utc)
    market = market or {}
    facts = facts or {}
    stats = stats or {}
    out = {'pair': pair, 'at': at or now.isoformat(timespec='seconds'),
           'hour_utc': now.hour, 'weekday': now.weekday(), 'session': session_of(now.hour)}

    # ── цена и волатильность ─────────────────────────────────────────────
    try:
        close = df['close']
        price = float(close.iloc[-1])
        out['price'] = _num(price, 8)
        out['atr_pct'] = _num(facts.get('atr_pct'))
        out['change_4h_pct'] = _num(facts.get('change_4h'))
        out['change_24h_pct'] = _pct(price, close.iloc[-25]) if len(close) > 25 else None
        # Волатильность в перцентилях: где нынешний ATR% среди последних 30 суток.
        high, low = df['high'], df['low']
        tr = (high - low) / close * 100
        atr_series = tr.rolling(14).mean().dropna()
        if len(atr_series) > 50:
            # Перцентиль внутри одного и того же ряда: последний ATR против
            # своих 30 суток, а не против ATR другого определения из facts.
            window = atr_series.iloc[-720:]
            out['atr_percentile_30d'] = round(float((window <= float(window.iloc[-1])).mean() * 100), 1)
        window_h, window_l = high.iloc[-720:], low.iloc[-720:]
        span = float(window_h.max() - window_l.min())
        out['range_pos_30d_pct'] = round((price - float(window_l.min())) / span * 100, 1) if span else None
    except Exception:                              # noqa: BLE001
        pass

    # ── структура и старшие ТФ ───────────────────────────────────────────
    smc = market.get('smc') or {}
    out['trend_1h'] = smc.get('trend')
    out['bias_1h'] = smc.get('bias')
    out['side_of_range'] = smc.get('side_of_range')
    out['equilibrium'] = _num(smc.get('equilibrium'), 8)
    if out.get('equilibrium') and out.get('price'):
        out['vs_equilibrium_pct'] = _pct(out['price'], out['equilibrium'])
    last_break = smc.get('last_break') or {}
    out['last_break_type'] = last_break.get('type')
    out['last_break_dir'] = last_break.get('direction')
    out['last_break_bars_ago'] = _num(last_break.get('bars_ago'), 0)
    sweep = smc.get('sweep') or {}
    out['sweep_side'] = sweep.get('side')
    htf = market.get('htf') or {}
    out['trend_4h'] = (htf.get('htf') or {}).get('trend')
    out['trend_1d'] = (htf.get('bias') or {}).get('trend')

    # ── поток и деривативы ───────────────────────────────────────────────
    delta = market.get('delta') or {}
    out['delta_stale'] = bool(delta.get('stale')) if delta else None
    out['delta_fresh_min'] = _num(delta.get('fresh_min'), 0)
    for name in ('h1', 'h4', 'h24'):
        win = delta.get(name) or {}
        out[f'delta_{name}_share_pct'] = _num(win.get('share_pct') if isinstance(win, dict) else None)
        out[f'delta_{name}_cover_min'] = _num(win.get('minutes') if isinstance(win, dict) else None, 0)
    out['delta_divergence'] = delta.get('divergence') if delta else None
    oi_flow = market.get('oi_flow') or {}
    out['oi_change_pct_6b'] = _num(oi_flow.get('change_pct'))
    out['oi_week_pct'] = _num(market.get('oi_week'))
    funding = market.get('funding_trend') or []
    out['funding_last'] = _num(funding[-1], 8) if funding else None
    out['funding_trend'] = (('up' if funding[-1] > funding[0] else 'down' if funding[-1] < funding[0] else 'flat')
                            if len(funding) >= 2 else None)
    book = market.get('book') or {}
    out['book_imbalance'] = _num(book.get('imbalance'))
    out['book_spread_pct'] = _num(book.get('spread_pct'), 6)
    out['book_walls_below'] = len(book.get('walls_below') or []) if book else None
    out['book_walls_above'] = len(book.get('walls_above') or []) if book else None
    liq = market.get('liq_fact') or {}
    h24 = liq.get('h24') or {}
    out['liq_long_size_24h'] = _num(h24.get('long_size'), 2)
    out['liq_short_size_24h'] = _num(h24.get('short_size'), 2)
    out['liq_events_24h'] = _num(liq.get('events'), 0)
    act = market.get('activity') or {}
    out['activity_last_x'] = _num(act.get('last_x'))
    out['activity_day_x'] = _num(act.get('day_x'))
    bench = market.get('benchmark') or {}
    out['btc_24h_pct'] = _num(bench.get('btc_24h'))
    out['vs_btc_24h_pp'] = _num(bench.get('relative_24h'))
    macro = market.get('macro') or {}
    for key in ('usdt_d', 'btc_d', 'usdt_d_24h', 'usdt_d_7d', 'btc_d_24h', 'total2_24h', 'usdt_d_streak_days'):
        out[f'macro_{key}'] = _num(macro.get(key))
    if macro.get('counted_24h'):
        out['macro_breadth_pct'] = round(macro['up_24h'] / macro['counted_24h'] * 100, 1)
    out['macro_stale'] = bool(macro.get('stale')) if macro else None
    dp = market.get('day_profile') or {}
    if dp.get('vwap_today') and out.get('price'):
        out['vs_vwap_pct'] = _pct(out['price'], dp['vwap_today'])
    if dp.get('poc_24h') and out.get('price'):
        out['vs_poc24_pct'] = _pct(out['price'], dp['poc_24h'])
    prof = market.get('profile') or {}
    if prof.get('poc') and out.get('price'):
        out['vs_poc200_pct'] = _pct(out['price'], prof['poc'])
        out['in_value_area'] = bool(prof.get('value_low', 0) <= out['price'] <= prof.get('value_high', 0))

    # ── уровни: ближайшие пулы ───────────────────────────────────────────
    levels = verdict.get('levels') or []
    price = out.get('price')
    if price and levels:
        pool = re.compile(r'скоплен|ликвидац|EQH|EQL|максимум|минимум|стоп')
        ups = [lv for lv in levels if lv['price'] > price and pool.search(lv.get('kind', ''))]
        downs = [lv for lv in levels if lv['price'] < price and pool.search(lv.get('kind', ''))]
        out['pool_up_pct'] = _pct(min(ups, key=lambda l: l['price'])['price'], price) if ups else None
        out['pool_down_pct'] = _pct(max(downs, key=lambda l: l['price'])['price'], price) if downs else None
        out['levels_n'] = len(levels)
        out['levels_above'] = sum(1 for lv in levels if lv['price'] > price)

    # ── решение модели ───────────────────────────────────────────────────
    out['decision'] = 'enter' if verdict.get('ok') else 'skip'
    out['gate'] = verdict.get('gate', '')
    out['side'] = verdict.get('side') or ''
    out['bias'] = verdict.get('bias', '')
    out['p'] = _num(verdict.get('p'))
    out['rr'] = _num(verdict.get('rr'))
    out['votes'] = _num(verdict.get('votes'), 0)
    conf = verdict.get('confluence') or {}
    for f in ('poi', 'vp', 'der', 'smc', 'flow'):
        out[f'cf_{f}'] = bool(conf.get(f)) if conf else None
    out['trigger_when'] = verdict.get('trigger_when') or ''
    if verdict.get('entry') and price:
        sign = 1 if verdict.get('side') == 'LONG' else -1
        out['entry_dist_pct'] = round(sign * (price - float(verdict['entry'])) / price * 100, 4)
        out['stop_pct'] = _num(verdict.get('stop_pct'))
        if verdict.get('targets'):
            out['tp1_pct'] = _pct(float(verdict['targets'][0]), float(verdict['entry']))
    out['ids_entry'] = (verdict.get('ids') or {}).get('entry')
    out['ids_stop'] = (verdict.get('ids') or {}).get('stop')
    out['critic'] = (verdict.get('critic') or {}).get('verdict', '')

    # ── качество данных и время ──────────────────────────────────────────
    out['missing_blocks'] = [b for b in SNAPSHOT_BLOCKS if market.get(b) is None] if market else list(SNAPSHOT_BLOCKS)
    out['data_gap_bars'] = _num(facts.get('data_gap_bars'), 0)
    out['macro_age_min'] = _num(macro.get('age_min'), 1)
    out['seconds'] = _num(stats.get('seconds'), 1)
    out['prompt_tokens'] = _num(stats.get('prompt_tokens'), 0)
    out['cached_tokens'] = _num(stats.get('cached_tokens'), 0)
    out['answer_tokens'] = _num(stats.get('answer_tokens'), 0)
    out['thought_tokens'] = _num(stats.get('thought_tokens'), 0)
    out['thought_chars'] = len(verdict.get('thought') or '')
    out['finish'] = stats.get('finish')
    out['model'] = stats.get('model', '')
    return out


def markup_path(pair, at):
    stamp = re.sub(r'[^0-9T]', '-', (at or '')[:19]) or datetime.now(timezone.utc).strftime('%Y-%m-%dT%H-%M-%S')
    return os.path.join(MARKUP_DIR, f'{stamp}_{pair}.txt')


def record(pair, df, verdict, stats=None, at=''):
    """Разметка — файлом, признаки — строкой. Молча при любой поломке записи."""
    try:
        text = verdict.get('markup') or ''
        if text:
            os.makedirs(MARKUP_DIR, exist_ok=True)
            with open(markup_path(pair, at), 'w', encoding='utf-8') as fh:
                fh.write(text)
        row = features(pair, df, verdict.get('facts') or {}, verdict.get('market_snapshot') or {},
                       verdict, stats, at=at)
        row['markup_file'] = os.path.basename(markup_path(pair, at)) if text else ''
        with open(FEATURES_PATH, 'a', encoding='utf-8') as fh:
            fh.write(json.dumps(row, ensure_ascii=False, default=str) + '\n')
        return row
    except Exception as exc:                       # noqa: BLE001
        log(f'⚠️ Признаки разбора {pair} не записаны: {exc}')
        return None
