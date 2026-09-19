"""
Срочность пары: кого модель должна разобрать раньше очереди.

ЗАЧЕМ. Разбор занимает 9-12 минут, круг по двадцати парам — три-четыре
часа. Пара, где только что сломалась структура или за час ликвидировали
вдвое больше обычного, ждала своей очереди наравне с мёртвым флэтом — модель
была слепа к быстрым движениям. Срочность считается КОДОМ и дёшево: из
кэшированного контекста SMC (строится сканером четвёртой стратегии каждый
цикл), из файлов ликвидаций и открытого интереса, из уровней по тем же
свечам. Никаких новых запросов к бирже.

ЧТО СЧИТАЕТСЯ СОБЫТИЕМ (баллы складываются):
    свежий слом структуры (BOS/CHoCH) не старше 3 закрытых свечей     +3
    всплеск ликвидаций за последний час, больше 2× медианы по часам   +2
    цена в 0.5% от одного из уровней разметки                          +2
    открытый интерес за последний час вырос больше чем на 1%           +2

Пара с срочностью ≥ 3 идёт впереди круга; если её разбирали недавно, окно
повтора сокращается вдвое — событие важнее расписания. Причина ставится в
журнал, чтобы приоритет был виден, а не подразумевался.

ПРОВЕРКА ЭФФЕКТА: среднее время от слома структуры до разбора пары. До —
половина круга, ~2 часа; ожидание после — ближе к одному циклу для пар с
событием.
"""

import time

from logger import log

FRESH_BREAK_BARS = 3
LIQ_BURST_X = 2.0
NEAR_LEVEL_PCT = 0.5
OI_JUMP_PCT = 1.0

# Порог, с которого пара считается срочной.
URGENT = 3


def score(pair, context=None, now_ms=None):
    """
    (баллы, причины) по паре. Без контекста считает только то, что можно
    без него (ликвидации, ОИ). Ничего не бросает: срочность — подсказка,
    а не условие работы.
    """
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    points, why = 0, []

    try:
        if context is not None:
            b = _fresh_break(context)
            if b:
                points += 3
                why.append(b)
            n = _near_level(context)
            if n:
                points += 2
                why.append(n)
    except Exception:                                  # noqa: BLE001
        pass
    try:
        l = _liq_burst(pair, now_ms)
        if l:
            points += 2
            why.append(l)
    except Exception:                                  # noqa: BLE001
        pass
    try:
        o = _oi_jump(pair, now_ms)
        if o:
            points += 2
            why.append(o)
    except Exception:                                  # noqa: BLE001
        pass
    return points, why


def _fresh_break(context):
    from smc import structure as structure_mod
    df = context.frames.get('poi')
    if df is None or not len(df):
        return ''
    index = len(df) - 1
    state = structure_mod.state_at(context.structure, index)
    event = state.get('last_event') or {}
    if not event:
        return ''
    age = index - int(event.get('index', index))
    if age <= FRESH_BREAK_BARS:
        return f"{event.get('type')} {age} св. назад"
    return ''


def _near_level(context):
    import llm_context
    df = context.frames.get('poi')
    if df is None or len(df) < 100:
        return ''
    found, _atr = llm_context.levels(df)
    if not found:
        return ''
    nearest = min(found, key=lambda lv: abs(lv['dist_pct']))
    if abs(nearest['dist_pct']) <= NEAR_LEVEL_PCT:
        return f"цена у {nearest['id']} ({nearest['dist_pct']:+.2f}%)"
    return ''


def _liq_burst(pair, now_ms):
    import liquidations
    rows = liquidations.rows(pair, since=now_ms - 24 * 3_600_000, upto=now_ms)
    if len(rows) < 10:
        return ''
    edge = now_ms - 3_600_000
    recent = sum(float(r['size']) for r in rows if int(r['ts']) >= edge)
    hours = {}
    for r in rows:
        if int(r['ts']) < edge:
            key = int(r['ts']) // 3_600_000
            hours[key] = hours.get(key, 0.0) + float(r['size'])
    if len(hours) < 4:
        return ''
    past = sorted(hours.values())
    median = past[len(past) // 2]
    if median > 0 and recent >= LIQ_BURST_X * median:
        return f"ликвидации ×{recent / median:.1f} к медиане часа"
    return ''


def _oi_jump(pair, now_ms):
    import positioning
    rows = positioning.series('open_interest', pair, upto=now_ms)
    if len(rows) < 2:
        return ''
    rows = sorted(rows, key=lambda r: int(r['ts']))
    last, prev = rows[-1], rows[-2]
    if now_ms - int(last['ts']) > 2 * 3_600_000:
        return ''                                      # ряд устарел
    try:
        change = (float(last['value']) / float(prev['value']) - 1) * 100
    except (TypeError, ValueError, ZeroDivisionError):
        return ''
    if change >= OI_JUMP_PCT:
        return f"ОИ +{change:.1f}% за час"
    return ''


def rank(pairs, context_of, now_ms=None):
    """
    Пары с баллами и причинами, срочные — первыми, порядок остальных не
    меняется. context_of(pair) -> контекст SMC или None.
    """
    scored = []
    for pair in pairs:
        try:
            context = context_of(pair)
        except Exception:                              # noqa: BLE001
            context = None
        points, why = score(pair, context, now_ms)
        scored.append((pair, points, why))
    urgent = [s for s in scored if s[1] >= URGENT]
    rest = [s for s in scored if s[1] < URGENT]
    urgent.sort(key=lambda s: -s[1])
    for pair, points, why in urgent:
        log(f"   срочность {pair}: {points} — {', '.join(why)}")
    return urgent + rest
