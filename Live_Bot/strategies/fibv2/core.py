"""
Фибоначчи V2: решения по импульсу из общего слоя (analysis/impulse).

Импульс, его длину, силу и имбалансы описывает общий слой; здесь — какие
импульсы брать и где вход, стоп и цели. Чистая логика: ни биржи, ни настроек
бота — только свечи и правила (params этой стратегии).
"""

from analysis import impulse


def _passes(q, rules):
    """Быстрый ли это импульс — или долгое движение. (да/нет, причина)."""
    if q['size_pct'] < rules.MIN_IMPULSE_PCT:
        return False, f"импульс {q['size_pct']:.1f}% меньше {rules.MIN_IMPULSE_PCT:g}%"
    if not rules.MIN_BARS <= q['bars'] <= rules.MAX_BARS:
        return False, f"импульс {q['bars']} свечей — нужно {rules.MIN_BARS}–{rules.MAX_BARS}"
    if q['atr_per_bar'] < rules.MIN_ATR_PER_BAR:
        return False, f"медленно: {q['atr_per_bar']:.2f} ATR за свечу"
    if q['dir_share'] < rules.MIN_DIR_SHARE:
        return False, f"свечей по ходу {q['dir_share'] * 100:.0f}%"
    if len(q['gaps']) < rules.MIN_GAPS:
        return False, 'нет имбаланса внутри импульса'
    return True, 'ok'


def _side_allowed(q, h, l, btc_return, rules):
    """Условие стороны: шорт — BTC за 30 дней рос; лонг — пробит максимум 7 суток."""
    if q['direction'] == -1 and rules.SHORT_NEEDS_BTC_RISE:
        if btc_return is None:
            return False, 'нет данных BTC за 30 дней'
        if btc_return <= 0:
            return False, 'шорт: BTC за 30 дней не рос'
    if q['direction'] == 1 and rules.LONG_NEEDS_BREAKOUT:
        s, n = q['s'], rules.BREAKOUT_LOOKBACK_HOURS
        if s < n:
            return False, 'мало истории для пробоя'
        if not q['end'] > max(h[s - n:s]):
            return False, 'лонг: максимум 7 суток не пробит'
    return True, 'ok'


def _stop_depth(q, rules):
    """Глубина стопа в долях импульса от конца."""
    if rules.STOP_MODE == 'start':
        return rules.STOP_BEYOND_START
    far = [deep for _, deep in q['gaps'] if rules.GAP_STOP_FROM <= deep <= rules.GAP_STOP_TO]
    if far:
        return min(far) + rules.GAP_STOP_BUFFER_PCT / 100 * q['end'] / q['size']
    return rules.STOP_LEVEL


def levels(q, rules):
    """Вход, стоп, цели в ценах."""
    d, ep, size = q['direction'], q['end'], q['size']
    at = lambda depth: ep - d * size * depth
    return {'entry': at(rules.ENTRY_LEVEL), 'stop': at(_stop_depth(q, rules)),
            'targets': [at(x) for x in rules.TP_LEVELS], 'stop_depth': _stop_depth(q, rules)}


def evaluate(o, h, l, c, bar_high, bar_low, price, btc_return, rules):
    """
    Сетап, который ФИКСИРУЕТ формирующаяся свеча, или (None, причина).
    o, h, l, c — закрытые часы; bar_high/bar_low/price — формирующаяся свеча
    на сейчас; btc_return — изменение BTC за 30 дней (доля) или None.
    Если в одной свече зафиксированы обе стороны — берётся более быстрая.
    """
    if len(c) < max(rules.START_LOOKBACK, rules.BREAKOUT_LOOKBACK_HOURS) + rules.MAX_BARS + 2:
        return None, 'мало свечей'
    atr = impulse.atr_sma(h, l, c, rules.ATR_PERIOD)
    found, reason = [], 'импульс не зафиксирован'
    for d in (1, -1):
        _, state = impulse.legs(h, l, d, rules.FIX_LEVEL, rules.START_LOOKBACK, rules.MAX_TRACK_BARS)
        if not impulse.fixed_now(h, l, d, state, rules.FIX_LEVEL, bar_high, bar_low):
            continue
        q = impulse.quality(o, h, l, c, atr, d, state['s'], state['e'])
        ok, reason = _passes(q, rules)
        if ok:
            ok, reason = _side_allowed(q, h, l, btc_return, rules)
        if not ok:
            continue
        lv = levels(q, rules)
        # Цена к постановке: за стопом — сетап сломан; вернулась к концу — импульс тянется.
        if (price - lv['stop']) * d <= 0:
            reason = 'откат уже за стопом'
            continue
        if (price - q['end']) * d >= 0:
            reason = 'цена вернулась к концу импульса'
            continue
        limit = lv['entry'] * (1 + d * rules.LIMIT_OFFSET_PCT)
        stop_pct = abs(limit - lv['stop']) / limit * 100
        if stop_pct < rules.MIN_STOP_PCT:
            reason = f'стоп {stop_pct:.2f}% теснее минимума {rules.MIN_STOP_PCT:g}%'
            continue
        found.append({**q, **lv, 'stop_pct': stop_pct, 'kind': state.get('kind'),
                      'side': 'LONG' if d == 1 else 'SHORT'})
    if not found:
        return None, reason
    return max(found, key=lambda x: x['atr_per_bar']), 'ok'
