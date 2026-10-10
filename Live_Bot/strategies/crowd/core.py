"""
Против толпы: чистая логика — ставка, событие, геометрия сделки.

Повторяет замер (research/crowdz/run.py): ставка приводится к 8 ч по шагу
между выплатами (шаг больше 8 ч или неизвестен — 8 ч); событие — выплата, на
которой ставка впервые достигла порога после выплаты ниже порога; ATR —
Уайлдера по закрытым барам 4ч (как smcz.structure.atr); стоп и цель — от цены
входа. Совпадение с исследовательским расчётом держит tests/test_crowd_wiring.py.
"""

import math

STEP_MS = 8 * 3_600_000


def normalized(rows, step_h=8.0):
    """Записи журнала фандинга {'ts': мс, 'value': доля за выплату} по возрастанию
    -> [(ts, ставка в б.п. за step_h часов)]."""
    out = []
    prev = None
    for row in rows:
        ts, value = row.get('ts'), row.get('value')
        if ts is None or value is None:
            continue
        try:
            value = float(value)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(value):
            continue
        gap = (ts - prev) if prev is not None else STEP_MS
        if gap <= 0 or gap > STEP_MS:
            gap = STEP_MS
        out.append((int(ts), value * 1e4 * step_h * 3_600_000 / gap))
        prev = ts
    return out


def event(rows, now_ms, threshold_bp=5.0, max_age_min=60.0):
    """Свежее событие «толпа в лонгах» или (None, причина).
    -> ({'ts', 'bp', 'prev_bp'}, None)."""
    pts = normalized(rows)
    if len(pts) < 2:
        return None, 'мало выплат фандинга в журнале'
    (ts_prev, bp_prev), (ts, bp) = pts[-2], pts[-1]
    if bp < threshold_bp:
        return None, f'фандинг {bp:+.2f} б.п. ниже порога {threshold_bp:g}'
    if bp_prev >= threshold_bp:
        return None, 'толпа в лонгах уже не первую выплату — событие было раньше'
    age_min = (now_ms - ts) / 60_000
    if age_min > max_age_min:
        return None, f'выплата {age_min:.0f} мин назад — позже {max_age_min:g} мин это не та сделка'
    if age_min < 0:
        return None, 'выплата в будущем — часы не сходятся'
    return {'ts': ts, 'bp': bp, 'prev_bp': bp_prev}, None


def atr(h, l, c, n=14):
    """ATR Уайлдера; первые n баров — среднее истинных диапазонов (как smcz)."""
    out = [0.0] * len(h)
    if not h:
        return out
    a = h[0] - l[0]
    out[0] = a
    for i in range(1, len(h)):
        tr = max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
        if i < n:
            a = (a * i + tr) / (i + 1)
        else:
            a = (a * (n - 1) + tr) / n
        out[i] = a
    return out


def setup(price, atr_now, stop_atr=1.5, target_r=2.0, min_stop_pct=0.5):
    """Шорт от цены входа: стоп выше на stop_atr·ATR, цель ниже на target_r·риск.
    -> (сетап, None) или (None, причина)."""
    if not (price > 0) or not (atr_now > 0):
        return None, 'нет цены или ATR'
    risk = stop_atr * atr_now
    stop_pct = risk / price * 100
    if stop_pct < min_stop_pct:
        return None, f'стоп {stop_pct:.2f}% теснее {min_stop_pct:g}% — издержки съедят сделку'
    return {'direction': 'SHORT', 'ref': price, 'stop': price + risk,
            'target': price - target_r * risk, 'risk': risk, 'stop_pct': stop_pct,
            'atr': atr_now}, None
