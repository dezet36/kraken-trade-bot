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
    цена в 1% от одного из уровней разметки                            +2
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
NEAR_LEVEL_PCT = 1.0
OI_JUMP_PCT = 1.0

# Порог, с которого пара считается срочной.
URGENT = 3

# Порог ПОВОДА: ниже него пару модели не отдают вовсе. Один балл не даёт
# ничего, любое одно событие (цена у уровня, всплеск ликвидаций, скачок ОИ,
# слом) — даёт. До 20.09.2026 круг шёл по всем 21 парам подряд, и половина
# разборов приходилась на пары посреди диапазона, где сетапа быть не может.
REASON = 2
# Пара без повода всё равно разбирается раз в столько часов: карта
# устаревает, а событие могло пройти между циклами.
STALE_HOURS = 8


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
        # Цена уровня в тексте — не для человека, а для signature(): номера
        # уровней перенумеровываются между разборами, цена — нет.
        price = nearest.get('price')
        tag = f" {float(price):g}" if price is not None else ''
        return f"цена у {nearest['id']}{tag} ({nearest['dist_pct']:+.2f}%)"
    return ''


# Сколько часов повод считается «тем же» после отказа: уровень стоит на
# месте до планового прохода; слом свежий три свечи; всплеск ликвидаций и
# скачок ОИ считаются за час и через час — уже новые.
SAME_REASON_HOURS = {'level': STALE_HOURS, 'break': FRESH_BREAK_BARS, 'liq': 1, 'oi': 1}


def signature(why):
    """
    Повод как множество сравнимых ключей: ('level', цена), ('break', тип),
    ('liq',), ('oi',). Возраст слома и кратность всплеска не в счёт — это
    тот же повод, а не новый.
    """
    import re
    keys = set()
    for text in why or ():
        text = str(text)
        if text.startswith('цена у'):
            m = re.match(r'цена у (L\d+)(?: ([0-9.eE+-]+))?', text)
            keys.add(('level', (m.group(2) or m.group(1)) if m else text))
        elif text.startswith('ликвидации'):
            keys.add(('liq',))
        elif text.startswith('ОИ'):
            keys.add(('oi',))
        else:
            keys.add(('break', text.split(' ')[0]))
    return frozenset(keys)


def same_reason(refused_at, refused_sig, why, now=None):
    """
    Тот же повод, что при отказе, — переспрашивать незачем.

    True, когда каждый нынешний ключ повода уже был в поводе отказа и отказ
    моложе своего срока (SAME_REASON_HOURS по виду ключа). Любой новый ключ —
    новый уровень у цены, слом, всплеск — делает повод новым.
    """
    now = now if now is not None else time.time()
    if refused_at is None:
        return False
    current = signature(why)
    if not current:
        return False
    age_h = (now - refused_at) / 3600
    for key in current:
        if key not in refused_sig:
            return False
        if age_h >= SAME_REASON_HOURS.get(key[0], 0):
            return False
    return True


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
