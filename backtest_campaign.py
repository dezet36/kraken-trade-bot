"""
Backtest: FIBONACCI CAMPAIGN strategy (полная отработка по чек-листу).

Отличие от backtest.py (OLD/NEW):
  - Импульс ФИКСИРУЕТСЯ (lock) и ведётся как кампания до TP1/инвалидации.
  - ДВА последовательных входа, каждый со своей целью:
        Вход №1 (зона A или B)  -> цель TP1 (-18%)
        Вход №2 (зона C 0-23.6% или зона A) -> цель TP2 (-61.8%)
  - Зона B (78.6-88.6%) условная: глубокий сценарий после провала зоны A.
  - Инвалидация: закрытие ниже 88.6%.
  - Вход — лимит на границе зоны (вход «в области интереса», как в документе).

Машина состояний (LONG; SHORT зеркально):
  AWAIT_E1_A : ждём цену в зону A (38.2-61.8). Лимит -> вход №1, цель TP1, SL за 61.8.
       TP1  -> сценарий normal, AWAIT_E2_C
       SL   -> AWAIT_E1_B (глубокий сценарий)
       close>B (новый хай) -> кампания закрыта (переразметка)
  AWAIT_E1_B : ждём цену в зону B (78.6-88.6). Лимит -> вход №1, цель TP1, SL за 100.
       TP1  -> сценарий deep, AWAIT_E2_A
       SL   -> кампания закрыта
  AWAIT_E2_C : ждём цену в зону C (0-23.6). Лимит -> вход №2, цель TP2, SL за 38.2.
  AWAIT_E2_A : ждём цену в зону A (38.2-61.8). Лимит -> вход №2, цель TP2, SL за 61.8.
       TP2  -> кампания complete
       SL   -> кампания закрыта
  Инвалидация (close < 88.6) на любой AWAIT-фазе -> кампания закрыта.

Результат каждого трейда — в R-мультипликаторах; портфельная эквити строится
компаундированием 1% риска на сделку (сортировка по времени выхода).
"""

import sys, os, time, math
sys.path.insert(0, r'D:\Bot trade\Live_Bot')

import pandas as pd
import numpy as np
from datetime import timedelta

import backtest as bt   # переиспуем загрузку данных + хелперы (main защищён __name__)

# ── Параметры кампании ───────────────────────────────────────────────────────
RISK_PCT        = 1.0
INITIAL_BALANCE = 10_000.0
SL_BUFFER       = 0.010     # буфер за уровнем (доля размера импульса)
MIN_SL_PCT      = 0.008     # пол дистанции SL, % от entry (синхронизировано с Live_Bot/config.py:MIN_SL_PERCENT)
MIN_IMPULSE_PCT = 3.0
MIN_RR_E1       = 2.0       # мин RR для первого входа (синхронизировано с Live_Bot/config.py:MIN_RR)
MIN_RR_E2       = 1.2       # мин RR для второго (континуация, допускаем ниже)
LOOKBACK_1H     = 48
STALE_HOURS     = 72        # макс. время ожидания заполнения входа в кампании
TP1_EXT         = 0.18      # -18%  (первый таргет, первый вход)
TP1B_EXT        = 0.27      # -27%  (второй TP первого входа)
TP2_EXT         = 0.618     # -61.8% (таргет второго входа)
COOLDOWN_H      = 12        # пауза между кампаниями на одной паре (после завершения)
COST_FRAC       = 0.0006    # round-trip издержки (комиссия+проскальзывание) от нотионала
MAX_CONCURRENT  = 5         # лимит одновременных позиций в портфеле

ZONE = {
    'A_top': 0.382, 'A_bot': 0.618,   # retracement edges зоны A
    'B_top': 0.786, 'B_bot': 0.886,   # зоны B
    'C_top': 0.000, 'C_bot': 0.236,   # зоны C (мелкий откат у хая)
    'inv':   0.886,                   # уровень инвалидации
    'L382':  0.382, 'L618': 0.618,    # ключевые уровни для SL
}

# ── Экспериментальные ручки (дефолты = текущее поведение; меняются setattr'ом
#    из exp_runner.py, живой бот их не видит) ─────────────────────────────────
ENTRY_R_E1A       = None          # None = ZONE['A_top'] (0.382); напр. 0.50 = лимит на 50%-уровне
BE_EXT            = 0.0           # взведение безубытка на fib_price(setup, -BE_EXT); 0.0 = уровень B
HTF_STRICT        = False         # True = блокировать кампании при HTF NEUTRAL (только по тренду)
BLOCK_FILL_HOURS  = frozenset()   # часы UTC, в которые лимит НЕ заполняется (сессионный фильтр)
BLOCK_BIRTH_HOURS = frozenset()   # часы UTC, в которые кампании НЕ рождаются
BLOCK_BIRTH_DOW   = frozenset()   # дни недели (0=Пн..6=Вс), в которые кампании НЕ рождаются
DIR_CAP           = None          # макс. активных позиций в ОДНУ сторону (None/0 = выкл)
# ── Ручки v2 (пересмотр геометрии; дефолты = текущее поведение v1) ───────────
SL_R_E1A          = None          # уровень стопа E1_A как retracement-доля: None = ZONE['L618']
                                  # (0.618); 0.786 / 0.886 (=инвалидация) / 1.0 (за начало импульса)
TRAIL_AFTER_BE_K  = None          # None = фикс-TP (v1); K>0 = после взведения BE стоп трейлится
                                  # на K × исходную SL-дистанцию от экстремума, TP отменяется
MAX_HOLD_H        = None          # тайм-стоп: макс. время жизни позиции в часах (None = без лимита).
                                  # КРИТИЧНО для широких стопов: без него позиция (особенно после BE,
                                  # стоп=entry) может застрять в коридоре НАВЕЧНО и заблокировать пару.
MAX_IMPULSE_PCT   = None          # макс. размер импульса, % от цены (None = без лимита; v1 имел только MIN)
HTF_FILTER        = True          # False = отключить HTF-блок контртренда (никогда не тестировалось)


def fib_price(setup, r):
    """Цена уровня r (доля коррекции): r=0 -> B (конец импульса), r=1 -> A (начало).
    Отрицательный r -> расширение за B (зона таргетов)."""
    B, sz = setup['end_price'], setup['size']
    return B - r * sz if setup['type'] == 'LONG' else B + r * sz


# ── Симуляция одной пары как кампании ────────────────────────────────────────
def simulate_pair_campaign(df1, df5, df4h, pair, cfg):
    if df1 is None or df5 is None or len(df1) < LOOKBACK_1H + 12 or len(df5) < 50:
        return []

    ts5 = df5['timestamp'].values
    o5  = df5['open'].values.astype(float)
    h5  = df5['high'].values.astype(float)
    l5  = df5['low'].values.astype(float)
    c5  = df5['close'].values.astype(float)
    n5  = len(df5)
    hr5 = pd.DatetimeIndex(df5['timestamp']).hour.values   # час UTC каждой 5м-свечи (для сессионных ручек)
    dw5 = pd.DatetimeIndex(df5['timestamp']).dayofweek.values   # день недели (0=Пн) каждой 5м-свечи

    # Целочисленные ns-таймстампы для быстрого searchsorted (без tz-конфликтов)
    ts5i = df5['timestamp'].values.astype('datetime64[ns]').astype('int64')
    ts1i = df1['timestamp'].values.astype('datetime64[ns]').astype('int64')
    ts4i = (df4h['timestamp'].values.astype('datetime64[ns]').astype('int64')
            if df4h is not None else None)

    trades   = []
    camp     = None          # активная кампания
    pos      = None          # открытая позиция в кампании
    last_det_hour  = None    # чтобы детектить импульс не чаще раза в 1H
    cooldown_until = 0        # индекс 5M-свечи, до которого новые кампании запрещены
    camp_seq       = 0        # счётчик кампаний пары (для портфельной связки)

    def build_campaign(end5_idx):
        """Пытается зафиксировать свежий импульс по 1H на момент времени 5M-свечи."""
        if BLOCK_BIRTH_HOURS and hr5[end5_idx] in BLOCK_BIRTH_HOURS:
            return None
        if BLOCK_BIRTH_DOW and dw5[end5_idx] in BLOCK_BIRTH_DOW:
            return None
        cur_i = ts5i[end5_idx]
        pos1  = int(np.searchsorted(ts1i, cur_i, side='right'))
        if pos1 < LOOKBACK_1H:
            return None
        df1w  = df1.iloc[max(0, pos1 - (LOOKBACK_1H + 1)):pos1]
        if len(df1w) < LOOKBACK_1H:
            return None
        setup = bt.find_impulse_new(df1w, lookback=LOOKBACK_1H)
        if not setup:
            return None
        size_pct = setup['size'] / setup['end_price'] * 100
        if size_pct < MIN_IMPULSE_PCT:
            return None
        if MAX_IMPULSE_PCT and size_pct > MAX_IMPULSE_PCT:
            return None

        # HTF фильтр
        if HTF_FILTER and ts4i is not None:
            pos4 = int(np.searchsorted(ts4i, cur_i, side='right'))
            htf = bt.get_htf_trend(df4h.iloc[max(0, pos4 - 220):pos4])
            if htf == 'BULLISH' and setup['type'] == 'SHORT':
                return None
            if htf == 'BEARISH' and setup['type'] == 'LONG':
                return None
            if HTF_STRICT and htf == 'NEUTRAL':
                return None

        cur_price = c5[end5_idx]
        za_top = fib_price(setup, ZONE['A_top'])   # мелкая граница зоны A
        # Должны ещё не уйти ниже инвалидации и не выше B
        inv = fib_price(setup, ZONE['inv'])
        if setup['type'] == 'LONG':
            if cur_price < inv or cur_price > setup['end_price']:
                return None
        else:
            if cur_price > inv or cur_price < setup['end_price']:
                return None

        return {
            'setup':   setup,
            'phase':   'AWAIT_E1_A',
            'scenario': None,
            'born_idx': end5_idx,
            'inv':     inv,
        }

    def long(setup):
        return setup['type'] == 'LONG'

    def zone_limit(setup, kind):
        """Лимит-цена входа = первая (мелкая) граница зоны, куда цена заходит при откате."""
        if kind == 'A':
            return fib_price(setup, ENTRY_R_E1A if ENTRY_R_E1A is not None else ZONE['A_top'])
        if kind == 'B':
            return fib_price(setup, ZONE['B_top'])   # 78.6 уровень
        if kind == 'C':
            return fib_price(setup, ZONE['C_top'])   # 0% уровень (B)
        return None

    def zone_far(setup, kind):
        """Дальняя граница зоны — куда цена ещё может дойти (для контроля)."""
        if kind == 'A':
            return fib_price(setup, ZONE['A_bot'])   # 61.8
        if kind == 'B':
            return fib_price(setup, ZONE['B_bot'])   # 88.6
        if kind == 'C':
            return fib_price(setup, ZONE['C_bot'])   # 23.6
        return None

    def sl_for(setup, sl_kind):
        """SL за указанным уровнем с буфером. Для E1_A ('618') уровень
        параметризуем ручкой SL_R_E1A (v2): 0.618 (дефолт) / 0.786 / 0.886 / 1.0."""
        sz = setup['size']
        if setup['type'] == 'LONG':
            if sl_kind == '618':
                r = SL_R_E1A if SL_R_E1A is not None else ZONE['L618']
                return fib_price(setup, r) - sz * SL_BUFFER
            if sl_kind == '100':  return setup['start_price']            - sz * SL_BUFFER
            if sl_kind == '382':  return fib_price(setup, ZONE['L382']) - sz * SL_BUFFER
        else:
            if sl_kind == '618':
                r = SL_R_E1A if SL_R_E1A is not None else ZONE['L618']
                return fib_price(setup, r) + sz * SL_BUFFER
            if sl_kind == '100':  return setup['start_price']            + sz * SL_BUFFER
            if sl_kind == '382':  return fib_price(setup, ZONE['L382']) + sz * SL_BUFFER
        return None

    def try_fill(setup, limit, i):
        """Заполнение лимита на свече i. Возврат цены входа или None."""
        if long(setup):
            if o5[i] <= limit:      # гэпом ниже лимита
                return o5[i]
            if l5[i] <= limit:      # дошли до лимита
                return limit
        else:
            if o5[i] >= limit:
                return o5[i]
            if h5[i] >= limit:
                return limit
        return None

    BOS_W = 6   # окно для микро-слома структуры на 5M

    def entry_fill(setup, kind, i):
        """Вход в зону. cfg['bos']=False -> лимит на границе зоны.
        cfg['bos']=True -> после касания зоны ждём слом структуры на 5M
        (закрытие за экстремум последних BOS_W свечей телом в сторону входа)."""
        if BLOCK_FILL_HOURS and hr5[i] in BLOCK_FILL_HOURS:
            return None
        lim = zone_limit(setup, kind)
        if not cfg.get('bos'):
            return try_fill(setup, lim, i)
        if i < BOS_W:
            return None
        tk = 'tch_' + camp['phase']
        if long(setup):
            if l5[i] <= lim:
                camp[tk] = True
            if camp.get(tk) and c5[i] > h5[i - BOS_W:i].max() and c5[i] > o5[i]:
                return c5[i]
        else:
            if h5[i] >= lim:
                camp[tk] = True
            if camp.get(tk) and c5[i] < l5[i - BOS_W:i].min() and c5[i] < o5[i]:
                return c5[i]
        return None

    def open_entry(setup, entry_price, sl_price, targets, fracs, be_level, min_rr, tag, i):
        # Пол дистанции SL (как в live strategy.py:calculate_trade_params) — без него
        # геометрия импульса иногда даёт entry~=sl (SL-дистанция около нуля), что
        # взрывает R-мультипликатор на много порядков на одной случайной сделке.
        min_sl_dist = entry_price * MIN_SL_PCT
        sl_dist = abs(entry_price - sl_price)
        if sl_dist < min_sl_dist:
            sl_price = (entry_price - min_sl_dist) if long(setup) else (entry_price + min_sl_dist)
            sl_dist = min_sl_dist
        if sl_dist <= 0:
            return None
        rr = abs(targets[0] - entry_price) / sl_dist   # RR к первому TP
        if rr < min_rr:
            return None
        return {
            'entry': entry_price, 'sl': sl_price, 'sl_dist': sl_dist,
            'targets': targets, 'fracs': fracs, 'tp_idx': 0, 'remaining': 1.0,
            'realized_R': 0.0, 'be_level': be_level, 'be_done': False,
            'rr': rr, 'tag': tag, 'target': targets[0],
            'entry_idx': i, 'entry_time': ts5[i],
        }

    def close_pos(p, exit_price, reason, i):
        """Закрывает ОСТАТОК позиции. Итоговый R = реализованные части + остаток - издержки."""
        nonlocal cooldown_until
        is_long = long(camp['setup'])
        leg_R = ((exit_price - p['entry']) if is_long else (p['entry'] - exit_price)) / p['sl_dist']
        total_R = p['realized_R'] + p['remaining'] * leg_R
        total_R -= COST_FRAC * p['entry'] / p['sl_dist']   # издержки round-trip в R
        cooldown_until = i + COOLDOWN_H * 12               # кулдаун перед новой кампанией
        trades.append({
            'pair': pair, 'cid': camp.get('cid', pair), 'dir': camp['setup']['type'], 'tag': p['tag'],
            'birth_time': ts5[camp['born_idx']],   # рождение кампании (для сессионной диагностики)
            'entry_time': p['entry_time'], 'exit_time': ts5[i],
            'entry': p['entry'], 'sl': p['sl'], 'target': p['target'],
            'rr': round(p['rr'], 2), 'exit_reason': reason, 'R': total_R,
            'tps_hit': p['tp_idx'], 'be': p['be_done'],
        })
        return total_R

    # ── Главный цикл по 5M ────────────────────────────────────────────────────
    i = LOOKBACK_1H * 12   # пропускаем стартовый прогрев
    while i < n5:
        setup = camp['setup'] if camp else None
        is_long = long(setup) if setup else True

        # 1) Управление открытой позицией
        if pos is not None:
            # Тайм-стоп: позиция старше MAX_HOLD_H часов -> рыночное закрытие по close
            if MAX_HOLD_H and (i - pos['entry_idx']) > MAX_HOLD_H * 12:
                close_pos(pos, c5[i], 'TIME_' + pos['tag'], i)
                pos = None
                camp = None
                i += 1
                continue
            # Безубыток: цена пробила уровень B импульса (0%, конец импульса) -> SL=вход
            if not pos['be_done'] and pos['be_level'] is not None:
                crossed = ((is_long and h5[i] >= pos['be_level']) or
                           (not is_long and l5[i] <= pos['be_level']))
                if crossed:
                    pos['sl'] = pos['entry']
                    pos['be_done'] = True

            trail_on = bool(TRAIL_AFTER_BE_K) and pos['be_done']
            sl  = pos['sl']
            # v2 trail-режим: после BE фикс-TP отменяется, выходим только по трейл-стопу
            tgt = (None if trail_on else
                   (pos['targets'][pos['tp_idx']] if pos['tp_idx'] < len(pos['targets']) else None))
            sl_hit = (is_long and l5[i] <= sl) or (not is_long and h5[i] >= sl)
            tp_hit = tgt is not None and ((is_long and h5[i] >= tgt) or (not is_long and l5[i] <= tgt))
            if sl_hit and tp_hit:   # оба на свече — ближе к open
                if abs(o5[i] - sl) <= abs(o5[i] - tgt):
                    tp_hit = False
                else:
                    sl_hit = False

            if not sl_hit and trail_on:
                # обновляем трейл ПО ЗАКРЫТИИ бара (действует со следующего — без lookahead)
                if is_long:
                    pos['trail_ext'] = max(pos.get('trail_ext', h5[i]), h5[i])
                    pos['sl'] = max(pos['sl'], pos['trail_ext'] - TRAIL_AFTER_BE_K * pos['sl_dist'])
                else:
                    pos['trail_ext'] = min(pos.get('trail_ext', l5[i]), l5[i])
                    pos['sl'] = min(pos['sl'], pos['trail_ext'] + TRAIL_AFTER_BE_K * pos['sl_dist'])

            if sl_hit:
                if trail_on and pos['sl'] != pos['entry']:
                    reason = 'TRAIL_' + pos['tag']
                else:
                    reason = ('BE_' if pos['be_done'] else 'SL_') + pos['tag']
                close_pos(pos, sl, reason, i)
                tag = pos['tag']; pos = None
                if tag == 'E1_A' and cfg['e1b'] and reason.startswith('SL'):
                    camp['phase'] = 'AWAIT_E1_B'      # глубокий сценарий (только реальный SL)
                else:
                    camp = None
                i += 1; continue

            if tp_hit:
                frac = pos['fracs'][pos['tp_idx']]
                leg  = ((tgt - pos['entry']) if is_long else (pos['entry'] - tgt)) / pos['sl_dist']
                pos['realized_R'] += frac * leg
                pos['remaining']  -= frac
                pos['tp_idx']     += 1
                if pos['tp_idx'] >= len(pos['targets']) or pos['remaining'] <= 1e-9:
                    close_pos(pos, tgt, 'TP_' + pos['tag'], i)
                    tag = pos['tag']; pos = None
                    if tag == 'E1_A' and cfg['e2']:
                        camp['phase'] = 'AWAIT_E2_C'; camp['scenario'] = 'normal'
                    elif tag == 'E1_B' and cfg['e2']:
                        camp['phase'] = 'AWAIT_E2_A'; camp['scenario'] = 'deep'
                    else:
                        camp = None
                i += 1; continue
            i += 1; continue

        # 2) Нет позиции, но есть кампания: ждём вход / проверяем инвалидацию
        if camp is not None:
            setup = camp['setup']; is_long = long(setup)
            # Инвалидация: закрытие за 88.6
            if (is_long and c5[i] < camp['inv']) or (not is_long and c5[i] > camp['inv']):
                camp = None; i += 1; continue
            # Протухание
            if i - camp['born_idx'] > STALE_HOURS * 12:
                camp = None; i += 1; continue

            phase = camp['phase']

            if phase == 'AWAIT_E1_A':
                # новый хай за B -> переразметка
                if (is_long and c5[i] > setup['end_price']) or (not is_long and c5[i] < setup['end_price']):
                    camp = None; i += 1; continue
                # цена ушла глубже зоны A без входа -> ждём зону B
                za_bot = zone_far(setup, 'A')
                fp = entry_fill(setup, 'A', i)
                if fp is not None:
                    if cfg.get('ntp', 1) == 2:
                        tgts = [fib_price(setup, -TP1_EXT), fib_price(setup, -TP1B_EXT)]
                        frs  = [0.5, 0.5]
                    else:
                        tgts = [fib_price(setup, -TP1_EXT)]
                        frs  = [1.0]
                    be_lvl = fib_price(setup, -BE_EXT) if cfg.get('be') else None   # BE_EXT=0.0 -> уровень B
                    p = open_entry(setup, fp, sl_for(setup, '618'),
                                   tgts, frs, be_lvl, MIN_RR_E1, 'E1_A', i)
                    if p: pos = p
                    i += 1; continue
                if (is_long and c5[i] < za_bot) or (not is_long and c5[i] > za_bot):
                    camp = (None if not cfg['e1b'] else camp)
                    if camp is not None:
                        camp['phase'] = 'AWAIT_E1_B'
                i += 1; continue

            if phase == 'AWAIT_E1_B':
                fp = entry_fill(setup, 'B', i)
                if fp is not None:
                    p = open_entry(setup, fp, sl_for(setup, '100'),
                                   [fib_price(setup, -TP1_EXT)], [1.0], None,
                                   MIN_RR_E1, 'E1_B', i)
                    if p: pos = p
                i += 1; continue

            if phase == 'AWAIT_E2_C':
                fp = entry_fill(setup, 'C', i)   # 0% уровень (откат к хаю)
                if fp is not None:
                    p = open_entry(setup, fp, sl_for(setup, '382'),
                                   [fib_price(setup, -TP2_EXT)], [1.0], None,
                                   MIN_RR_E2, 'E2_C', i)
                    if p: pos = p
                    else: camp = None
                i += 1; continue

            if phase == 'AWAIT_E2_A':
                fp = entry_fill(setup, 'A', i)
                if fp is not None:
                    p = open_entry(setup, fp, sl_for(setup, '618'),
                                   [fib_price(setup, -TP2_EXT)], [1.0], None,
                                   MIN_RR_E2, 'E2_A', i)
                    if p: pos = p
                    else: camp = None
                i += 1; continue

            i += 1; continue

        # 3) Нет кампании: детектим новый импульс не чаще раза в 1H, с кулдауном
        cur_hour = pd.Timestamp(ts5[i]).floor('h')
        if cur_hour != last_det_hour:
            last_det_hour = cur_hour
            if i >= cooldown_until:
                camp = build_campaign(i)
                if camp is not None:
                    camp_seq += 1
                    camp['cid'] = f'{pair}#{camp_seq}'
        i += 1

    # Позиция, открытая на конец данных, ОБЯЗАНА попасть в учёт (иначе широкие
    # стопы «прячут» застрявшие месяцами позиции и их нереализованный PnL)
    if pos is not None:
        close_pos(pos, c5[n5 - 1], 'EOD_' + pos['tag'], n5 - 1)

    return trades


# ── Портфельный фильтр: лимит одновременных позиций (с учётом связки кампании) ─
def portfolio_filter(trades, cap=None, dir_cap=None):
    """cap — общий лимит позиций; dir_cap — макс. позиций в ОДНУ сторону (None/0 = выкл)."""
    cap = MAX_CONCURRENT if cap is None else cap
    dir_cap = DIR_CAP if dir_cap is None else dir_cap
    # tie-break по (pair, cid): при равных entry_time (5м-грид) результат не должен
    # зависеть от порядка входного списка пар (воспроизводимость)
    ts = sorted(trades, key=lambda t: (pd.Timestamp(t['entry_time']), t['pair'], str(t['cid'])))
    active = {}            # pair -> (exit_time, dir)
    dropped_cids = set()
    accepted = []
    max_conc = 0
    for t in ts:
        et = pd.Timestamp(t['entry_time'])
        xt = pd.Timestamp(t['exit_time'])
        active = {p: v for p, v in active.items() if v[0] > et}   # освобождаем закрытые
        if t['cid'] in dropped_cids:
            continue
        if t['pair'] in active or len(active) >= cap:
            dropped_cids.add(t['cid'])     # дропаем всю цепочку кампании
            continue
        if dir_cap and sum(1 for v in active.values() if v[1] == t['dir']) >= dir_cap:
            dropped_cids.add(t['cid'])     # направление занято — дроп всей кампании
            continue
        active[t['pair']] = (xt, t['dir'])
        max_conc = max(max_conc, len(active))
        accepted.append(t)
    return accepted, max_conc, len(ts) - len(accepted)


def measure_max_concurrency(trades):
    events = []
    for t in trades:
        events.append((pd.Timestamp(t['entry_time']), 1))
        events.append((pd.Timestamp(t['exit_time']), -1))
    events.sort()
    cur = mx = 0
    for _, d in events:
        cur += d
        mx = max(mx, cur)
    return mx


# ── Портфельная эквити (компаунд заданного % риска) ──────────────────────────
def build_equity(trades, risk_pct=None):
    risk_pct = RISK_PCT if risk_pct is None else risk_pct
    trades = sorted(trades, key=lambda t: pd.Timestamp(t['exit_time']))
    balance = INITIAL_BALANCE
    curve = [balance]
    ruined = False
    for t in trades:
        pnl = t['R'] * (risk_pct / 100) * balance
        balance += pnl
        if balance <= 0:        # маржин-колл / обнуление депозита
            balance = 0.0
            ruined = True
            curve.append(balance)
            break
        t['pnl'] = pnl
        t['balance'] = balance
        curve.append(balance)
    return trades, curve, ruined


def stats(trades, curve, label='FIBONACCI CAMPAIGN'):
    if not trades:
        print('  Нет сделок.')
        return
    df = pd.DataFrame(trades)
    n = len(df)
    wins = (df['R'] > 0).sum()
    losses = (df['R'] < 0).sum()
    wr = wins / n * 100
    gross_p = df[df['pnl'] > 0]['pnl'].sum()
    gross_l = abs(df[df['pnl'] < 0]['pnl'].sum())
    pf = gross_p / gross_l if gross_l > 0 else math.inf
    total_pct = (curve[-1] - curve[0]) / curve[0] * 100
    peak = curve[0]; mdd = 0.0
    for b in curve:
        peak = max(peak, b)
        mdd = max(mdd, (peak - b) / peak * 100)
    avg_R = df['R'].mean()
    df['exit_time'] = pd.to_datetime(df['exit_time'])
    weeks = df['exit_time'].dt.to_period('W').nunique()
    tpw = n / weeks if weeks else n

    print('\n' + '=' * 60)
    print(f'  {label} | {bt.MONTHS_BACK} мес | {len(bt.BACKTEST_PAIRS)} пар | ${INITIAL_BALANCE:,.0f}')
    print('=' * 60)
    print(f'  Всего сделок:      {n}')
    print(f'  Сделок/неделю:     {tpw:.1f}')
    print(f'  Wins / Losses:     {wins}/{losses}')
    print(f'  Win Rate:          {wr:.1f}%')
    print(f'  Profit Factor:     {pf:.2f}')
    print(f'  Avg R:             {avg_R:+.2f}R')
    print(f'  Total PnL:         {total_pct:+.1f}%')
    print(f'  Final balance:     ${curve[-1]:,.2f}')
    print(f'  Max drawdown:      {mdd:.1f}%')
    print('-' * 60)
    print('  По типу входа:')
    for tag in ['E1_A', 'E1_B', 'E2_C', 'E2_A']:
        sub = df[df['tag'] == tag]
        if len(sub):
            w = (sub['R'] > 0).sum()
            print(f'    {tag:<5} {len(sub):>3} сделок | WR {w/len(sub)*100:>4.0f}% | sumR {sub["R"].sum():+.1f}')
    print('  Причины выхода:')
    for r, cnt in df['exit_reason'].value_counts().items():
        print(f'    {r:<10} {cnt}')
    print('=' * 60 + '\n')


def run_config(data_1h, data_5m, cfg, label, save_csv=False,
               risk_pct=None, cap=None, csv_path=r'D:\Bot trade\backtest_campaign.csv'):
    all_trades = []
    for pair in bt.BACKTEST_PAIRS:
        tr = simulate_pair_campaign(data_1h[pair], data_5m[pair],
                                    data_1h.get(pair + '_4h'), pair, cfg)
        all_trades.extend(tr)

    sumR        = sum(t['R'] for t in all_trades)
    max_conc    = measure_max_concurrency(all_trades)
    n_campaigns = len(set(t['cid'] for t in all_trades))
    eff_cap     = MAX_CONCURRENT if cap is None else cap
    capped, mc, dropped = portfolio_filter(all_trades, cap=cap)
    capped, curve, ruined = build_equity(capped, risk_pct=risk_pct)
    print(f'\n  Портфельный фильтр (лимит {eff_cap}): всего {len(all_trades)} сделок / '
          f'{n_campaigns} кампаний | flat-edge {sumR:+.0f}R | макс.одновр {max_conc} | '
          f'принято {len(capped)}, отброшено {dropped}' + (' | RUIN' if ruined else ''))
    stats(capped, curve, label=label)
    if save_csv and capped:
        pd.DataFrame(capped).to_csv(csv_path, index=False)
        print(f'Детальный лог (capped): {csv_path}')
    return capped, curve


if __name__ == '__main__':
    t0 = time.time()
    data_1h, data_5m = bt.load_all_data()

    print('\n[CAMPAIGN] Матрица конфигураций...', flush=True)

    def compute_metrics(capped, curve):
        if not capped:
            return (0, 0, 0, 0, 0)
        df = pd.DataFrame(capped)
        wr = (df['R'] > 0).mean() * 100
        gp = df[df['pnl'] > 0]['pnl'].sum()
        gl = abs(df[df['pnl'] < 0]['pnl'].sum())
        pf = gp / gl if gl > 0 else math.inf
        avg_r = df['R'].mean()
        pnl_pct = (curve[-1] - curve[0]) / curve[0] * 100
        peak = curve[0]; mdd = 0.0
        for b in curve:
            peak = max(peak, b)
            mdd = max(mdd, (peak - b) / peak * 100)
        return (wr, pf, pnl_pct, mdd, avg_r)

    grid_configs = [
        ({'e1b': False, 'e2': False, 'bos': False, 'ntp': 1, 'be': False}, 'D1 (1 TP -18%)'),
        ({'e1b': False, 'e2': False, 'bos': False, 'ntp': 1, 'be': True},  'D1B (1 TP + безуб@B)'),   # = точный live-конфиг
        ({'e1b': False, 'e2': False, 'bos': False, 'ntp': 2, 'be': True},  'D3 (2 TP + безуб@B)'),
    ]
    risks = (0.5, 1.0, 1.5, 2.0, 3.0)
    caps  = (5, 4, 3, 2)
    grid_rows = []

    DEPLOY_RISK, DEPLOY_CAP = 0.5, 3   # точка реального деплоя — детальный CSV на переиспользованных trd

    for cfg, cname in grid_configs:
        trd = []
        for pair in bt.BACKTEST_PAIRS:
            trd.extend(simulate_pair_campaign(
                data_1h[pair], data_5m[pair], data_1h.get(pair + '_4h'), pair, cfg))

        # Детальный CSV на точке деплоя — без повторного вызова simulate_pair_campaign
        tag = cname.split()[0]
        deploy_capped, _, _ = portfolio_filter(trd, cap=DEPLOY_CAP)
        deploy_capped, deploy_curve, _ = build_equity(deploy_capped, risk_pct=DEPLOY_RISK)
        if deploy_capped:
            pd.DataFrame(deploy_capped).to_csv(
                rf'D:\Bot trade\backtest_campaign_{tag}_r05_c3.csv', index=False)
            print(f'Детальный CSV @ risk={DEPLOY_RISK}%/cap={DEPLOY_CAP}: '
                  f'backtest_campaign_{tag}_r05_c3.csv ({len(deploy_capped)} сделок)')

        print('\n' + '=' * 80)
        print(f'  СЕТКА РИСК × ЛИМИТ ПОЗИЦИЙ — {cname}')
        print('  В ячейке: итоговый доход %  /  макс. просадка %   (RUIN = обнуление депо)')
        print('=' * 80)
        print('  риск\\лимит │' + ''.join(f'{("cap " + str(c)):>16}' for c in caps))
        print('  ' + '-' * 78)
        for risk in risks:
            cells = []
            for cap in caps:
                capped, _, _ = portfolio_filter(trd, cap=cap)
                capped, curve, ruined = build_equity(capped, risk_pct=risk)
                wr, pf, pnl, dd, avg_r = compute_metrics(capped, curve)
                grid_rows.append({
                    'config': cname, 'risk_pct': risk, 'cap': cap,
                    'n_trades': len(capped), 'wr_pct': round(wr, 1),
                    'pf': round(pf, 3) if math.isfinite(pf) else None,
                    'pnl_pct': round(pnl, 1), 'max_dd_pct': round(dd, 1),
                    'avg_R': round(avg_r, 3), 'ruined': ruined,
                    'final_balance': round(curve[-1], 2) if capped else None,
                })
                cells.append(f'{"RUIN":>16}' if ruined
                             else f'{pnl:>+8.0f}%/{dd:>3.0f}%'.rjust(16))
            print(f'  {risk:>5.1f}%     │' + ''.join(cells))
        print('=' * 80)

    print('  Подсказка: ищем макс. доход при просадке, которую готов терпеть (обычно < 35-40%).')

    grid_df = pd.DataFrame(grid_rows)
    grid_df.to_csv(r'D:\Bot trade\backtest_campaign_grid.csv', index=False)
    print(f'\nСетка сохранена: backtest_campaign_grid.csv ({len(grid_df)} строк)')
    print(f'\nВремя: {(time.time()-t0)/60:.1f} мин')
