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
    """Находит последний импульс за последние N свечей"""
    if len(df) < lookback_candles:
        return None

    segment = df.tail(lookback_candles).reset_index(drop=True)

    max_idx = segment['high'].idxmax()
    min_idx = segment['low'].idxmin()

    max_price = segment.loc[max_idx, 'high']
    min_price = segment.loc[min_idx, 'low']

    if min_idx < max_idx:
        direction = 'LONG'
        impulse_start, impulse_end = min_idx, max_idx
    elif max_idx < min_idx:
        direction = 'SHORT'
        impulse_start, impulse_end = max_idx, min_idx
    else:
        return None

    # Улучшение B: фильтр концентрации импульса.
    # Медленный дрейф (>15 свечей) должен иметь ≥60% направленных свечей,
    # иначе это боковик, а не настоящий импульс.
    num_candles = impulse_end - impulse_start + 1
    if num_candles > 15:
        impulse_seg = segment.iloc[impulse_start:impulse_end + 1]
        if direction == 'LONG':
            directional = (impulse_seg['close'] > impulse_seg['open']).sum()
        else:
            directional = (impulse_seg['close'] < impulse_seg['open']).sum()
        if directional / num_candles < 0.60:
            return None

    if direction == 'LONG':
        return {
            'type': 'LONG',
            'start_price': min_price,
            'end_price': max_price,
            'size': max_price - min_price,
            'start_time': segment.loc[min_idx, 'timestamp'],
            'end_time': segment.loc[max_idx, 'timestamp']
        }
    else:
        return {
            'type': 'SHORT',
            'start_price': max_price,
            'end_price': min_price,
            'size': max_price - min_price,
            'start_time': segment.loc[max_idx, 'timestamp'],
            'end_time': segment.loc[min_idx, 'timestamp']
        }

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

def detect_break_of_structure(df_5m, setup, zones):
    """Обнаруживает слом структуры (BoS)"""
    if len(df_5m) < 10:
        return None

    # Улучшение A: n=3 вместо n=2 — swing-уровень требует 3 свечи с каждой стороны.
    local_highs, local_lows = find_local_extremes(df_5m, n=3)
    if not local_highs or not local_lows:
        return None

    # Улучшение D: окно 60 вместо 30 (5 часов вместо 2.5).
    recent_window = 60
    start_idx = max(0, len(df_5m) - recent_window)

    # Volume confirmation: EMA20 объёма на 5M
    vol_ema = df_5m['volume'].ewm(span=20, adjust=False).mean()
    vol_mult = config.VOLUME_CONFIRM_MULT

    zone_visited = False
    active_zone = None

    for i in range(start_idx, len(df_5m)):
        candle = df_5m.iloc[i]

        if not zone_visited:
            for zone in zones:
                if (price_in_zone(candle['close'], zone) or
                        price_in_zone(candle['low'], zone) or
                        price_in_zone(candle['high'], zone)):
                    zone_visited = True
                    active_zone = zone
                    break

        if not zone_visited:
            continue

        avg_vol = vol_ema.iloc[i]

        if setup['type'] == 'LONG':
            prev_highs = [h for h in local_highs if h['index'] < i]
            if prev_highs and candle['close'] > prev_highs[-1]['price']:
                if avg_vol > 0 and candle['volume'] < avg_vol * vol_mult:
                    continue  # BoS на слабом объёме — ищем дальше
                return {
                    'type': 'LONG',
                    'trigger_price': candle['close'],
                    'trigger_time': candle['timestamp'],
                    'bos_level': prev_highs[-1]['price'],
                    'zone': active_zone['name'],
                    'volume_ratio': round(candle['volume'] / avg_vol, 2) if avg_vol > 0 else 0,
                }
        else:
            prev_lows = [l for l in local_lows if l['index'] < i]
            if prev_lows and candle['close'] < prev_lows[-1]['price']:
                if avg_vol > 0 and candle['volume'] < avg_vol * vol_mult:
                    continue  # BoS на слабом объёме — ищем дальше
                return {
                    'type': 'SHORT',
                    'trigger_price': candle['close'],
                    'trigger_time': candle['timestamp'],
                    'bos_level': prev_lows[-1]['price'],
                    'zone': active_zone['name'],
                    'volume_ratio': round(candle['volume'] / avg_vol, 2) if avg_vol > 0 else 0,
                }

    return None

def calculate_trade_params(setup, trigger, zone_name, balance):
    """Рассчитывает параметры сделки"""
    start_price = setup['start_price']
    end_price = setup['end_price']
    size = setup['size']
    trigger_price = trigger['trigger_price']
    
    if setup['type'] == 'LONG':
        if zone_name == 'Zone_A':
            # SL ниже зоны A: за уровень 61.8% (ZONE_A_BOTTOM от LOW)
            sl_price = start_price + size * config.ZONE_A_BOTTOM - (size * config.SL_BUFFER)
        else:
            # SL ниже зоны B: за уровень 100% (ниже LOW)
            sl_price = start_price - (size * config.SL_BUFFER)

        min_sl = trigger_price * config.MIN_SL_PERCENT
        if trigger_price - sl_price < min_sl:
            sl_price = trigger_price - min_sl

        # TP уровни: расширения вверх от HIGH (-18%, -27%, -61.8%, -100%)
        tp1 = end_price + size * config.TP1_LEVEL
        tp2 = end_price + size * config.TP2_LEVEL
        tp3 = end_price + size * config.TP3_LEVEL
        tp4 = end_price + size * config.TP4_LEVEL

    else:
        if zone_name == 'Zone_A':
            # SL выше зоны A: за уровень 61.8% (ZONE_A_BOTTOM от HIGH)
            sl_price = start_price - size * config.ZONE_A_BOTTOM + (size * config.SL_BUFFER)
        else:
            # SL выше зоны B: за уровень 100% (выше HIGH)
            sl_price = start_price + (size * config.SL_BUFFER)

        min_sl = trigger_price * config.MIN_SL_PERCENT
        if sl_price - trigger_price < min_sl:
            sl_price = trigger_price + min_sl

        # TP уровни: расширения вниз от LOW (-18%, -27%, -61.8%, -100%)
        tp1 = end_price - size * config.TP1_LEVEL
        tp2 = end_price - size * config.TP2_LEVEL
        tp3 = end_price - size * config.TP3_LEVEL
        tp4 = end_price - size * config.TP4_LEVEL
    
    sl_distance = abs(trigger_price - sl_price)
    tp1_distance = abs(tp1 - trigger_price)
    rr = tp1_distance / sl_distance if sl_distance > 0 else 0
    
    if rr < config.MIN_RR:
        return None
    
    risk_amount = balance * (config.RISK_PER_PAIR / 100)
    position_size = risk_amount / sl_distance
    
    return {
        'entry': trigger_price,
        'stop_loss': sl_price,
        'take_profit_1': tp1,
        'take_profit_2': tp2,
        'take_profit_3': tp3,
        'take_profit_4': tp4,
        'position_size': position_size,
        'risk_amount': risk_amount,
        'rr': rr,
        'sl_distance': sl_distance
    }

def analyze_market(df_1h, df_5m, trading_pair, balance):
    """Основная функция анализа рынка"""
    setup = find_recent_impulse(df_1h, lookback_candles=config.LOOKBACK_CANDLES)
    if not setup:
        return None
    
    zone_a, zone_b = get_zones(setup)
    zones = [zone_a, zone_b]

    current_price = df_1h.iloc[-1]['close']

    # Инвалидация: свеча закрылась ниже/выше уровня 88.6% → сетка недействительна
    end_price = setup['end_price']
    size = setup['size']
    if setup['type'] == 'LONG':
        invalidation_level = end_price - size * config.ZONE_B_TOP
        if current_price < invalidation_level:
            log(f"[{trading_pair}] Setup LONG инвалидирован: цена {current_price:.4f} < 88.6% ({invalidation_level:.4f})")
            return None
    else:
        invalidation_level = end_price + size * config.ZONE_B_TOP
        if current_price > invalidation_level:
            log(f"[{trading_pair}] Setup SHORT инвалидирован: цена {current_price:.4f} > 88.6% ({invalidation_level:.4f})")
            return None

    in_zone = False
    for zone in zones:
        if price_in_zone(current_price, zone):
            in_zone = True
            break

    if not in_zone:
        return None
    
    trigger = detect_break_of_structure(df_5m, setup, zones)
    if not trigger:
        return None
    
    params = calculate_trade_params(setup, trigger, trigger['zone'], balance)
    if not params:
        return None
    
    signal = {
        'trading_pair': trading_pair,
        'setup': setup,
        'trigger': trigger,
        'params': params,
        'zone_a': zone_a,
        'zone_b': zone_b
    }
    
    return signal