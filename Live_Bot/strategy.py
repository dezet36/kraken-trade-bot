import pandas as pd
import config
import settings_store as settings
from logger import log


def get_htf_trend(df_4h, return_strength=False):
    """
    Определяет направление тренда на 4H по EMA50/EMA200.

    BULLISH  — цена выше EMA50 И EMA50 выше EMA200 (восходящий тренд)
    BEARISH  — цена ниже EMA50 И EMA50 ниже EMA200 (нисходящий тренд)
    NEUTRAL  — смешанные сигналы (боковик или переходная фаза)

    return_strength=True -> возвращает (trend, strength), где strength =
    |EMA50-EMA200|/EMA200 (нормированное расстояние — для скоринга кандидатов).
    """
    if df_4h is None or len(df_4h) < config.HTF_EMA_SLOW + 10:
        return ('NEUTRAL', 0.0) if return_strength else 'NEUTRAL'

    close = df_4h['close']
    ema_fast = close.ewm(span=config.HTF_EMA_FAST, adjust=False).mean()
    ema_slow = close.ewm(span=config.HTF_EMA_SLOW, adjust=False).mean()

    last_close    = close.iloc[-1]
    last_ema_fast = ema_fast.iloc[-1]
    last_ema_slow = ema_slow.iloc[-1]

    if last_close > last_ema_fast and last_ema_fast > last_ema_slow:
        trend = 'BULLISH'
    elif last_close < last_ema_fast and last_ema_fast < last_ema_slow:
        trend = 'BEARISH'
    else:
        trend = 'NEUTRAL'

    if not return_strength:
        return trend
    strength = abs(last_ema_fast - last_ema_slow) / last_ema_slow if last_ema_slow else 0.0
    return trend, strength


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

    def _build_setup(direction, s_idx, e_idx, s_price, e_price, source):
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
        # ── Происхождение импульса ───────────────────────────────────────────
        # Записывается, не проверяется: поведение прежнее. Нужно затем, что
        # «импульс» здесь — это отрезок между последним swing-low и последним
        # swing-high, а между ними МОГУТ лежать другие свинги. Тогда отрезок
        # уже не импульс, а кусок пилы, и сетка Фибоначчи натягивается не на
        # то. Сколько таких — вопрос к данным, а не к рассуждению, поэтому
        # число промежуточных свингов едет с сетапом.
        between = sum(1 for p in (local_highs + local_lows)
                      if s_idx < p['index'] < e_idx)
        return {
            'type': direction,
            'start_price': s_price,
            'end_price': e_price,
            'size': size,
            'start_time': segment.loc[s_idx, 'timestamp'],
            'end_time': segment.loc[e_idx, 'timestamp'],
            'source': source,
            'swings_between': between,
            'candles': num,
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
            setup = _build_setup(direction, s_idx, e_idx, s_price, e_price, 'swing')
            if setup:
                return setup

    # ── Fallback: глобальный max/min (старый метод) ──────────────────────────
    max_idx = segment['high'].idxmax()
    min_idx = segment['low'].idxmin()
    max_price = segment.loc[max_idx, 'high']
    min_price = segment.loc[min_idx, 'low']

    if min_idx < max_idx:
        return _build_setup('LONG', min_idx, max_idx, min_price, max_price, 'range')
    elif max_idx < min_idx:
        return _build_setup('SHORT', max_idx, min_idx, max_price, min_price, 'range')
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


def entry_zone_name(entry_price, zone_a, zone_b):
    """
    Как называется место, где стоит лимит. Считается, а не объявляется.

    ОТКУДА ЭТО. В сигнале стояло `'zone': 'Zone_A'` литералом, и это делало
    мёртвыми сразу трёх потребителей:

      * статистика в trade_manager отбирает сделки по `zone == 'Zone_B'` —
        список не мог наполниться НИКОГДА;
      * Telegram показывает по ней значок и описание, и ветка 🅱️ была
        недостижима;
      * панель и сводка обещали сравнение зоны A с зоной B, которого не
        существовало.

    Разбор 193 сделок FIBO с сервера: зона B нарисована на всех графиках,
    входов в ней ноль. Выглядело как «рынок туда не доходит», а на деле код
    никогда её и не называл.

    Само место входа задаёт ENTRY_RETRACE (по умолчанию 0.5 — половина
    отката). Зона A это откат 38.2–61.8%, зона B — 78.6–88.6%. При 0.5 вход
    честно попадает в зону A; поставив 0.8, человек окажется в зоне B, и
    теперь отчёты об этом скажут.

    ПОВЕДЕНИЕ НЕ МЕНЯЕТСЯ: имя зоны нигде не решает, торговать ли и по какой
    цене. Меняется только то, правду ли о себе говорит бот.
    """
    for zone in (zone_a, zone_b):
        if not zone:
            continue
        if zone['bottom'] <= entry_price <= zone['top']:
            return zone['name']
    # Между зонами (откат 61.8–78.6%) или мельче 38.2%. Врать «зона B» здесь
    # нельзя — это делала прежняя ветка `else`.
    return 'Zone_MID'


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

def calculate_trade_params(setup, entry_price, balance, trading_pair=None, log_reject=True):
    """
    Параметры сделки (геометрия v2, 2026-07-05):
    - entry_price = граница зоны A (38.2% уровень коррекции)
    - SL за уровнем config.SL_LEVEL_R (0.886 = инвалидация сетапа) с буфером 1%
    - TP1 = -TP1_LEVEL за B (-25%, единственный тейк, закрывает 100%)
    - be_level = уровень B импульса (0%, конец импульса) — для безубытка
    """
    start_price = setup['start_price']
    end_price   = setup['end_price']
    size        = setup['size']

    # Минимальный стоп и риск берём из настроек: их меняют из дашборда на ходу,
    # и перезапускать бота ради этого не нужно.
    min_stop = settings.min_stop_pct('FIBO')
    risk_pct = settings.risk_pct('FIBO')

    if setup['type'] == 'LONG':
        # SL ниже: за уровнем SL_LEVEL_R коррекции от B (0.886 = инвалидация)
        sl_price = end_price - size * config.SL_LEVEL_R - (size * config.SL_BUFFER)
        min_sl = entry_price * min_stop
        if entry_price - sl_price < min_sl:
            sl_price = entry_price - min_sl
        tp1 = end_price + size * config.TP1_LEVEL   # -25% расширение за B
        tp2 = end_price + size * config.TP2_LEVEL   # (мёртвое поле, совместимость)
    else:
        # SL выше: за уровнем SL_LEVEL_R коррекции от B
        sl_price = end_price + size * config.SL_LEVEL_R + (size * config.SL_BUFFER)
        min_sl = entry_price * min_stop
        if sl_price - entry_price < min_sl:
            sl_price = entry_price + min_sl
        tp1 = end_price - size * config.TP1_LEVEL
        tp2 = end_price - size * config.TP2_LEVEL

    sl_distance = abs(entry_price - sl_price)
    rr = abs(tp1 - entry_price) / sl_distance if sl_distance > 0 else 0
    if rr < config.MIN_RR:
        if log_reject:
            tag = f"{trading_pair}: " if trading_pair else ""
            log(f"   {tag}нет сигнала — RR {rr:.2f} < MIN_RR {config.MIN_RR} (entry ${entry_price:.6f})")
        return None

    risk_amount = balance * (risk_pct / 100)
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
        # ── План выхода едет ВМЕСТЕ с сигналом ───────────────────────────────
        # Раньше исполнитель брал доли закрытия из глобального config, поэтому
        # план одной стратегии молча применялся к другой. У фибо план простой:
        # одна цель -25%, закрывает всю позицию.
        'tp_targets':    [tp1],
        'tp_fractions':  list(getattr(config, 'TP_CLOSE_FRACTIONS', [1.0]))[:1] or [1.0],
        'breakeven_after_tp': bool(getattr(config, 'BREAKEVEN_AT_B', True)),
        'max_same_direction': getattr(config, 'MAX_SAME_DIRECTION', 0),
        # Процент риска едет с сигналом: исполнитель пересчитывает размер по
        # своему балансу и обязан использовать ту же настройку, что и расчёт.
        'risk_pct': risk_pct,
    }

def analyze_market(df_1h, df_5m, trading_pair, balance):
    """
    Текущий конфиг (Фибо-лимит): вход — GTC-лимит на границе зоны A (38.2%),
    БЕЗ ожидания BoS. df_5m оставлен в сигнатуре для совместимости (не используется).
    """
    setup = find_recent_impulse(df_1h, lookback_candles=config.LOOKBACK_CANDLES)
    if not setup:
        log(f"   {trading_pair}: нет сигнала — импульс не найден")
        return None

    zone_a, zone_b = get_zones(setup)
    current_price = df_1h.iloc[-1]['close']
    end_price = setup['end_price']
    size = setup['size']

    # Инвалидация: цена закрылась за уровнем 88.6% → сетка недействительна
    # ПРОВЕРКИ ИНВАЛИДАЦИИ ЗДЕСЬ БОЛЬШЕ НЕТ, И ВОТ ПОЧЕМУ.
    #
    # Она стояла выше окна входа и отклоняла сигнал, если цена ушла за уровень
    # 88.6% коррекции. Отклонить она не могла НИЧЕГО: окно входа, идущее
    # следом, строго уже — оно требует, чтобы цена была между входом и концом
    # импульса, а это далеко выше уровня инвалидации. Любая цена, проходящая
    # окно, проходила и её.
    #
    # Вреда от мёртвого кода нет, но он обещает защиту, которой не
    # существует, — и однажды кто-то на неё положится. Настоящая инвалидация
    # уже стоящей заявки делается сроком её жизни, в исполнителе, а не здесь.

    # Вход = лимит на 38.2%-границе зоны A. Лимит должен «отдыхать» по ходу коррекции:
    #   LONG  — цена ещё ВЫШЕ границы (zone_a top) и не выше B
    #   SHORT — цена ещё НИЖЕ границы (zone_a bottom) и не ниже B
    # Глубина входа — отдельный параметр, а не граница зоны A. Считается от
    # КОНЦА импульса: 0.5 означает лимит на половине отката. Раньше вход был
    # жёстко привязан к ближней границе зоны (38.2%), и поменять его можно
    # было только сдвинув саму зону — вместе с её подписью на графике и с
    # проверкой инвалидации.
    depth = getattr(config, 'ENTRY_RETRACE', 1 - config.ZONE_A_TOP)

    if setup['type'] == 'LONG':
        entry_price = end_price - size * depth
        if not (entry_price <= current_price <= end_price):
            log(f"   {trading_pair}: нет сигнала — цена {current_price:.6f} вне окна входа "
                f"[{entry_price:.6f}, {end_price:.6f}]")
            return None
    else:
        entry_price = end_price + size * depth
        if not (end_price <= current_price <= entry_price):
            log(f"   {trading_pair}: нет сигнала — цена {current_price:.6f} вне окна входа "
                f"[{end_price:.6f}, {entry_price:.6f}]")
            return None

    params = calculate_trade_params(setup, entry_price, balance, trading_pair=trading_pair)
    if not params:
        return None   # причина (RR < MIN_RR) уже залогирована внутри calculate_trade_params

    return {
        'trading_pair': trading_pair,
        'setup':   setup,
        # Имя зоны СЧИТАЕТСЯ по месту лимита. Здесь стоял литерал 'Zone_A', и
        # из-за него статистика по зоне B не могла наполниться никогда —
        # см. entry_zone_name.
        'trigger': {'zone': entry_zone_name(entry_price, zone_a, zone_b),
                    'entry_type': 'ZONE_LIMIT', 'trigger_price': entry_price},
        'params':  params,
        'zone_a':  zone_a,
        'zone_b':  zone_b,
    }