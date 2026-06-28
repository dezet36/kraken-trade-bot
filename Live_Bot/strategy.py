import pandas as pd
import config
from logger import log


def get_htf_trend(df_4h):
    """
    Определяет направление тренда на 4H по EMA50/EMA200.

    BULLISH  — цена выше EMA50 И EMA50 выше EMA200 (восходящий тренд)
    BEARISH  — цена ниже EMA50 И EMA50 ниже EMA200 (нисходящий тренд)
    NEUTRAL  — смешанные сигналы (боковик или переходная фаза)
    """
    if df_4h is None or len(df_4h) < config.HTF_EMA_SLOW + 10:
        return 'NEUTRAL'

    close = df_4h['close']
    ema_fast = close.ewm(span=config.HTF_EMA_FAST, adjust=False).mean()
    ema_slow = close.ewm(span=config.HTF_EMA_SLOW, adjust=False).mean()

    last_close    = close.iloc[-1]
    last_ema_fast = ema_fast.iloc[-1]
    last_ema_slow = ema_slow.iloc[-1]

    if last_close > last_ema_fast and last_ema_fast > last_ema_slow:
        return 'BULLISH'
    if last_close < last_ema_fast and last_ema_fast < last_ema_slow:
        return 'BEARISH'
    return 'NEUTRAL'


def find_recent_impulse(df, lookback_candles=48):
    """
    Находит последний структурный импульс за последние N свечей.

    Приоритет: свинговый метод (локальные экстремумы n=2) — ищет самый
    свежий направленный ход между последним swing-low и swing-high.
    Fallback: глобальный max/min за весь период (старый метод).

    Дополнительные фильтры:
    - Точка B (конец импульса) должна быть в последних 24 свечах (свежесть)
    - Для импульсов >15 свечей: ≥60% свечей должны быть направленными
    """
    if len(df) < lookback_candles:
        return None

    segment = df.tail(lookback_candles).reset_index(drop=True)

    def _build_setup(direction, s_idx, e_idx, s_price, e_price):
        size = abs(e_price - s_price)
        if size <= 0:
            return None
        num = e_idx - s_idx + 1
        # W12: импульс, а не долгая торговля — лимит длительности и мин. скорость
        if num > config.MAX_IMPULSE_CANDLES:
            return None
        size_pct = size / e_price * 100
        if size_pct / num < config.MIN_IMPULSE_VELOCITY:
            return None
        if num > 15:
            seg = segment.iloc[s_idx:e_idx + 1]
            directional = (
                (seg['close'] > seg['open']).sum() if direction == 'LONG'
                else (seg['close'] < seg['open']).sum()
            )
            if directional / num < 0.60:
                return None
        return {
            'type': direction,
            'start_price': s_price,
            'end_price': e_price,
            'size': size,
            'start_time': segment.loc[s_idx, 'timestamp'],
            'end_time': segment.loc[e_idx, 'timestamp'],
        }

    # ── Свинговый метод: ищем последний чистый направленный ход ─────────────
    local_highs, local_lows = find_local_extremes(segment, n=2)
    if local_highs and local_lows:
        last_high = local_highs[-1]
        last_low  = local_lows[-1]

        if last_low['index'] < last_high['index']:
            direction = 'LONG'
            s_idx, e_idx = last_low['index'], last_high['index']
            s_price, e_price = last_low['price'], last_high['price']
        else:
            direction = 'SHORT'
            s_idx, e_idx = last_high['index'], last_low['index']
            s_price, e_price = last_high['price'], last_low['price']

        # Проверка свежести: точка B в последних 24 свечах
        if e_idx >= len(segment) - 24:
            setup = _build_setup(direction, s_idx, e_idx, s_price, e_price)
            if setup:
                return setup

    # ── Fallback: глобальный max/min (старый метод) ──────────────────────────
    max_idx = segment['high'].idxmax()
    min_idx = segment['low'].idxmin()
    max_price = segment.loc[max_idx, 'high']
    min_price = segment.loc[min_idx, 'low']

    if min_idx < max_idx:
        return _build_setup('LONG', min_idx, max_idx, min_price, max_price)
    elif max_idx < min_idx:
        return _build_setup('SHORT', max_idx, min_idx, max_price, min_price)
    return None

def get_zones(setup):
    """Рассчитывает зоны интереса"""
    start_price = setup['start_price']
    end_price = setup['end_price']
    size = setup['size']

    if setup['type'] == 'LONG':
        # Zone A: 38.2%-61.8% коррекции от HIGH (измеряем от LOW вверх)
        zone_a = {
            'name': 'Zone_A',
            'top': start_price + size * config.ZONE_A_TOP,       # 61.8% от LOW = 38.2% коррекции от HIGH
            'bottom': start_price + size * config.ZONE_A_BOTTOM, # 38.2% от LOW = 61.8% коррекции от HIGH
        }
        # Zone B: 78.6%-88.6% коррекции от HIGH (глубокая, близко к LOW — измеряем от HIGH вниз)
        zone_b = {
            'name': 'Zone_B',
            'top': end_price - size * config.ZONE_B_BOTTOM,      # HIGH - 78.6% = 21.4% от LOW
            'bottom': end_price - size * config.ZONE_B_TOP,      # HIGH - 88.6% = 11.4% от LOW
        }
    else:
        # Zone A: 38.2%-61.8% коррекции от LOW (измеряем от HIGH вниз)
        zone_a = {
            'name': 'Zone_A',
            'top': start_price - size * config.ZONE_A_BOTTOM,    # HIGH - 38.2%
            'bottom': start_price - size * config.ZONE_A_TOP,    # HIGH - 61.8%
        }
        # Zone B: 78.6%-88.6% коррекции от LOW (глубокая, близко к HIGH — измеряем от LOW вверх)
        zone_b = {
            'name': 'Zone_B',
            'top': end_price + size * config.ZONE_B_TOP,         # LOW + 88.6%
            'bottom': end_price + size * config.ZONE_B_BOTTOM,   # LOW + 78.6%
        }

    return zone_a, zone_b

def price_in_zone(price, zone):
    """Проверяет, находится ли цена в зоне"""
    return zone['bottom'] <= price <= zone['top']

def find_local_extremes(df, n=2):
    """Находит локальные экстремумы"""
    local_highs = []
    local_lows = []
    
    for i in range(n, len(df) - n):
        if all(df.iloc[i]['high'] > df.iloc[i - j]['high'] and df.iloc[i]['high'] > df.iloc[i + j]['high'] for j in range(1, n + 1)):
            local_highs.append({'index': i, 'price': df.iloc[i]['high'], 'time': df.iloc[i]['timestamp']})
        
        if all(df.iloc[i]['low'] < df.iloc[i - j]['low'] and df.iloc[i]['low'] < df.iloc[i + j]['low'] for j in range(1, n + 1)):
            local_lows.append({'index': i, 'price': df.iloc[i]['low'], 'time': df.iloc[i]['timestamp']})
    
    return local_highs, local_lows

# detect_break_of_structure удалён в W11: основной вход — лимит в зоне A без BoS
# (бэктест показал, что BoS-подтверждение основного входа ухудшает результат).

def calculate_trade_params(setup, entry_price, balance):
    """
    Параметры сделки для W11 (Фибо-лимит, конфиг D3):
    - entry_price = граница зоны A (38.2% уровень)
    - SL за уровнем 61.8% с буфером 1%
    - TP1 = -18% (50%), TP2 = -27% (50%)
    - be_level = уровень B импульса (0%, конец импульса) — для безубытка
    """
    start_price = setup['start_price']
    end_price   = setup['end_price']
    size        = setup['size']

    if setup['type'] == 'LONG':
        # SL ниже зоны A: за уровень 61.8% (ZONE_A_BOTTOM от LOW)
        sl_price = start_price + size * config.ZONE_A_BOTTOM - (size * config.SL_BUFFER)
        min_sl = entry_price * config.MIN_SL_PERCENT
        if entry_price - sl_price < min_sl:
            sl_price = entry_price - min_sl
        tp1 = end_price + size * config.TP1_LEVEL   # -18%
        tp2 = end_price + size * config.TP2_LEVEL   # -27%
    else:
        # SL выше зоны A: за уровень 61.8% (ZONE_A_BOTTOM от HIGH)
        sl_price = start_price - size * config.ZONE_A_BOTTOM + (size * config.SL_BUFFER)
        min_sl = entry_price * config.MIN_SL_PERCENT
        if sl_price - entry_price < min_sl:
            sl_price = entry_price + min_sl
        tp1 = end_price - size * config.TP1_LEVEL
        tp2 = end_price - size * config.TP2_LEVEL

    sl_distance = abs(entry_price - sl_price)
    rr = abs(tp1 - entry_price) / sl_distance if sl_distance > 0 else 0
    if rr < config.MIN_RR:
        return None

    risk_amount = balance * (config.RISK_PER_PAIR / 100)
    position_size = risk_amount / sl_distance

    return {
        'entry':         entry_price,
        'stop_loss':     sl_price,
        'take_profit_1': tp1,
        'take_profit_2': tp2,
        'be_level':      end_price,   # уровень B (0%) — пробой => SL в безубыток
        'position_size': position_size,
        'risk_amount':   risk_amount,
        'rr':            rr,
        'sl_distance':   sl_distance,
    }

def analyze_market(df_1h, df_5m, trading_pair, balance):
    """
    W11 (Фибо-лимит, конфиг D3): вход — GTC-лимит на границе зоны A (38.2%),
    БЕЗ ожидания BoS. df_5m оставлен в сигнатуре для совместимости (не используется).
    """
    setup = find_recent_impulse(df_1h, lookback_candles=config.LOOKBACK_CANDLES)
    if not setup:
        return None

    zone_a, zone_b = get_zones(setup)
    current_price = df_1h.iloc[-1]['close']
    end_price = setup['end_price']
    size = setup['size']

    # Инвалидация: цена закрылась за уровнем 88.6% → сетка недействительна
    if setup['type'] == 'LONG':
        if current_price < end_price - size * config.ZONE_B_TOP:
            return None
    else:
        if current_price > end_price + size * config.ZONE_B_TOP:
            return None

    # Вход = лимит на 38.2%-границе зоны A. Лимит должен «отдыхать» по ходу коррекции:
    #   LONG  — цена ещё ВЫШЕ границы (zone_a top) и не выше B
    #   SHORT — цена ещё НИЖЕ границы (zone_a bottom) и не ниже B
    if setup['type'] == 'LONG':
        entry_price = zone_a['top']
        if not (entry_price <= current_price <= end_price):
            return None
    else:
        entry_price = zone_a['bottom']
        if not (end_price <= current_price <= entry_price):
            return None

    params = calculate_trade_params(setup, entry_price, balance)
    if not params:
        return None

    return {
        'trading_pair': trading_pair,
        'setup':   setup,
        'trigger': {'zone': 'Zone_A', 'entry_type': 'ZONE_LIMIT', 'trigger_price': entry_price},
        'params':  params,
        'zone_a':  zone_a,
        'zone_b':  zone_b,
    }