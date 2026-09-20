"""
Решение по сетапу: спросить модель и проверить, что она ответила.

ПОРЯДОК ВАЖЕН. Сначала грамматика не даёт выдумать уровень, потом код
проверяет всё остальное. Разделение такое: грамматика отвечает за то, ЧТО
может быть написано, а этот модуль — за то, имеет ли написанное смысл.

ЧЕГО ГРАММАТИКА НЕ ЛОВИТ, И ПОЭТОМУ ЛОВИТСЯ ЗДЕСЬ

    геометрия     у лонга стоп обязан быть НИЖЕ входа, а цель выше. Модель
                  вольна выбрать L2 стопом и L7 целью для лонга, и грамматика
                  это пропустит: оба идентификатора законны.
    издержки      стоп теснее минимального не окупает комиссию
    отношение     R:R к первой цели
    ожидание      EV с НАСТОЯЩЕЙ комиссией в R, а не с постоянной 0.002
    конфлюенс     порог считается по полям, а не со слов модели

ПОЧЕМУ КОНФЛЮЕНС СЧИТАЕТ КОД. Попроси модель саму поставить себе оценку X/5 —
она натянет её под сетап, который ей понравился. Это не злой умысел, а
свойство: объяснение подгоняется под уже принятое решение. Поэтому модель
отмечает пять отдельных признаков, а порог применяется здесь.

КАЖДЫЙ ОТКАЗ ИМЕЕТ ИМЯ. Оно уходит в refused.csv, и по нему потом проверяется,
правильно ли предохранитель отсекал. Безымянный отказ превращает разбор в
гадание — так уже вышло с зоной B и развилкой THIN_STOP.
"""

import json

import config
import llm_context
import llm_grammar
from logger import log

# Порог совпадения факторов. Четыре из пяти — из промта, который проверялся
# отдельно; число намеренно не пять, иначе сделок не будет вовсе.
MIN_CONFLUENCE = 4

# Ниже этого отношения сделка не берётся. При винрейте около трети меньшее
# отношение не окупает даже без комиссий.
MIN_RR = 2.5

FACTORS = ('poi', 'vp', 'der', 'smc', 'flow')

# Условия входа, которые умеет исполнять код (см. strategy_llm._armed).
TRIGGERS = ('now', 'close_above', 'close_below', 'retest', 'sweep_reclaim',
            'close_above_with_volume', 'close_below_with_volume')

# Отказы, за которыми стоит НЕИСПРАВНОСТЬ, а не суждение модели. Разница не
# косметическая: «мало конфлюенса» — это работа, законченная ответом, а «ответ
# обрезан» означает, что ответа не было вовсе. Их нельзя ни считать вместе, ни
# показывать одинаково, ни одинаково запоминать: по сетапу, на который модель
# не ответила, спросить надо СНОВА, а не через час.
#
# Список здесь, а не в каждом из трёх мест, где он нужен: стратегии, панели и
# странице. Правило, записанное трижды, расходится — в этом проекте так уже
# вышло с дневным стоп-краном.
BROKEN_GATES = ('модель упала', 'модель зависла', 'ответ обрезан', 'ответ не разобран', 'ответ пуст',
                'окно контекста мало', 'модель недоступна')


def cost_in_r(entry, stop):
    """
    Во сколько R обойдётся круг комиссий при таком стопе.

    Это то самое число, которое готовый промт заменял постоянной 0.002. При
    стопе 0.3% настоящая величина 0.25R — занижение более чем в сто раз, и
    ровно на тесных стопах, где издержки и решают.
    """
    if not entry or not stop:
        return 0.0
    distance = abs(entry - stop) / entry
    if distance <= 0:
        return 0.0
    return config.ENTRY_COST_ROUND_TRIP / distance


def expected_value(probability, rr, cost_r):
    """
    Ожидание сделки в R.

    Выигрыш даёт rr, проигрыш стоит ровно 1R (стоп на то и стоп), издержки
    платятся в обоих случаях.
    """
    return probability * rr - (1 - probability) * 1.0 - cost_r


def _geometry_ok(side, entry, stop, targets):
    """
    Стоп с нужной стороны, цели с противоположной.

    Грамматика этого выразить не может: она знает, что L2 и L7 — законные
    идентификаторы, но не знает, какой из них выше.
    """
    if side == 'LONG':
        return stop < entry and all(t > entry for t in targets)
    return stop > entry and all(t < entry for t in targets)


def _refusal(gate, detail, extra=None):
    """Отказ с именем. Имя уходит в журнал отказов и там проверяется."""
    out = {'ok': False, 'gate': gate, 'detail': detail}
    out.update(extra or {})
    return out


def split_thought(answer):
    """(мысль, ответ): блок <think>…</think> отделяется от JSON."""
    text = answer or ''
    if '<think>' in text and '</think>' in text:
        start = text.index('<think>') + len('<think>')
        end = text.index('</think>')
        return text[start:end].strip(), text[end + len('</think>'):].strip()
    return '', text


def parse(answer, levels):
    """
    Разбирает ответ модели в цены. Возвращает словарь или None при поломке.

    Грамматика гарантирует форму, поэтому ошибок разбора быть не должно. Но
    вызов может пройти и без грамматики — например, если её отключат ради
    отладки, — и тогда сюда придёт что угодно.
    """
    thought, answer = split_thought(answer)
    try:
        data = json.loads(answer)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or 'd' not in data:
        return None

    out = {'decision': data.get('d'), 'why': data.get('why', ''),
           'risk': data.get('risk', ''),
           'thought': thought,
           # За что стоит стоп и почему цель именно там — отдельными полями:
           # «вход тут, стоп минус 1.5%» без ответа на эти два вопроса —
           # не план.
           'stop_why': data.get('stop_why', ''),
           'tp_why': data.get('tp_why', ''),
           # Разбор и режим рынка модель пишет ПЕРЕД решением — это её
           # рассуждение вслух, и оно нужно и при отказе: по нему видно, что
           # именно она разглядела в данных, а не только чем кончила.
           'regime': data.get('regime', ''),
           'analysis': analysis_text(data.get('analysis', '')),
           'analysis_parts': data.get('analysis') if isinstance(data.get('analysis'), dict) else None,
           # Куда рынок — обязательно и при отказе: по нему отказ становится
           # проверяемым (сказала up, цена ушла вниз — ошибка направления).
           'bias': data.get('bias') if data.get('bias') in ('up', 'down', 'flat') else '',
           'alt': data.get('alt', ''),
           'confluence': {f: bool((data.get('cf') or {}).get(f))
                          for f in FACTORS}}
    out.update(_trigger(data.get('trigger'), levels))
    if data.get('d') != 'enter':
        return out

    out['side'] = data.get('side')
    out['p'] = float(data.get('p') or 0)
    out['entry'] = llm_context.price_of(levels, data.get('entry'))
    out['stop'] = llm_context.price_of(levels, data.get('stop'))
    out['inval'] = llm_context.price_of(levels, data.get('inval'))
    out['targets'] = [llm_context.price_of(levels, t)
                      for t in (data.get('tp') or [])]
    out['ids'] = {'entry': data.get('entry'), 'stop': data.get('stop'),
                  'tp': data.get('tp'), 'inval': data.get('inval')}
    return out


ANALYSIS_TITLES = {'direction': 'Куда рынок', 'liquidity': 'Ликвидность',
                   'structure': 'Структура и зоны', 'flow': 'Поток и деривативы',
                   'conflicts': 'Противоречия', 'plan': 'План: за что и куда'}


def analysis_text(raw):
    """Разбор одной строкой: шесть полей — шестью пронумерованными абзацами."""
    if isinstance(raw, dict):
        return '\n'.join(f"{n}. {ANALYSIS_TITLES.get(k, k)}: {v}"
                         for n, (k, v) in enumerate(raw.items(), 1) if v)
    return raw or ''


def _trigger(raw, levels):
    """
    Условие входа: объект {when, level, note} или, по-старому, строка.

    Строка означает «сейчас»: так отвечала модель до того, как условие стало
    исполняемым, и так до сих пор отвечают проверки. В журнал уходит одна
    строка «when Lx: note» — её читает человек; коду нужны when и цена.
    """
    if isinstance(raw, dict):
        when = raw.get('when') or 'now'
        level_id = raw.get('level')
        note = raw.get('note') or ''
    else:
        when, level_id, note = 'now', None, (raw or '')
    if when not in TRIGGERS:
        when = 'now'
    price = llm_context.price_of(levels, level_id) if level_id else None
    if when != 'now' and price is None:
        # Условие без уровня исполнить нельзя — считаем, что входим сейчас,
        # и пишем об этом в текст: это видно в журнале.
        note = f'[уровень условия не найден] {note}'
        when = 'now'
    text = note if when == 'now' else f'{when} {level_id}: {note}'
    return {'trigger': text, 'trigger_when': when,
            'trigger_level': price, 'trigger_id': level_id}


def truncated(answer):
    """
    Похож ли ответ на обрубок, а не на отказ разбирать.

    ОТЛИЧИТЬ ОДНО ОТ ДРУГОГО ОБЯЗАТЕЛЬНО, И ВОТ ПОЧЕМУ. С 18 по 19 сентября
    2026 журнал сервера 119 раз подряд написал «модель вернула не JSON». Имя
    отказа было честным и совершенно бесполезным: по нему думалось, что модель
    отвечает чепухой, и чинить полезли бы промт. На деле окно контекста было
    2048 при вопросе в 1703 токена, ответ обрывался на 345-м, и JSON не
    закрывался — модель отвечала правильно, ей не давали договорить.

    Признак прямой: грамматика заканчивает ответ закрывающей скобкой. Нет её —
    значит генерация оборвалась, а не модель написала мусор.
    """
    _, answer = split_thought(answer)
    text = (answer or '').strip()
    return bool(text) and not text.endswith('}')


# Ближе этого стоп считается «под скоплением»: свип чужих стопов проходит
# за уровень на десятые процента, а на волатильной монете — на доли ATR.
# Порог динамический: 0.3% + 0.1 × ATR% (при ATR 0.7% это 0.37%, при 3% —
# 0.6%). Было 0.25% фиксированно — по замечанию стороннего разбора мало.
STOP_HUNT_BASE_PCT = 0.3
STOP_HUNT_ATR_SHARE = 0.1


def stop_hunt_pct(atr_pct=None):
    return STOP_HUNT_BASE_PCT + STOP_HUNT_ATR_SHARE * float(atr_pct or 0.0)


def _pools(market):
    """Скопления чужих стопов из снимка: нетронутые пулы и равные экстремумы."""
    smc = (market or {}).get('smc') or {}
    out = []
    for row in (smc.get('untapped') or []):
        if row.get('price'):
            out.append((float(row['price']), row.get('side'), row.get('source') or 'пул'))
    for row in (smc.get('equal_levels') or []):
        if row.get('price'):
            out.append((float(row['price']), row.get('side'), row.get('source') or 'EQ'))
    return out


def trigger_against_idea(side, entry, parsed):
    """Условие входа, ждущее хода ПРОТИВ сделки. Описание или ''."""
    when = parsed.get('trigger_when') or 'now'
    level = parsed.get('trigger_level')
    if when == 'now' or not level or not entry:
        return ''
    # Исключение «close_below для лонга с уровнем ниже входа» снято 20.09:
    # ждать выноса под уровень — это sweep_reclaim, а второе имя для того же
    # путало и модель, и читателя.
    if side == 'LONG' and when in ('close_below', 'close_below_with_volume'):
        return (f'лонг от {entry:.6g} после закрытия ниже {level:.6g} — '
                f'это ожидание слома идеи, а не подтверждения; вынос под уровень — sweep_reclaim')
    if side == 'SHORT' and when in ('close_above', 'close_above_with_volume'):
        return (f'шорт от {entry:.6g} после закрытия выше {level:.6g} — '
                f'это ожидание слома идеи, а не подтверждения; вынос над уровнем — sweep_reclaim')
    return ''


def stop_in_liquidity(side, stop, market, atr_pct=None, stop_level=None):
    """
    Стоп вплотную за скоплением чужих стопов. Возвращает описание или ''.

    Для лонга опасен пул стопов лонгов (SSL) ЧУТЬ ВЫШЕ нашего стопа: цену
    повезут снимать его, и наш стоп в 0.1% ниже снимут тем же ходом. Для
    шорта зеркально — BSL чуть ниже стопа. Стоп ЗА пулом с запасом — как
    раз правильное место, и он не трогается.

    stop_level — уровень, который модель назвала, а код отступил за него:
    пул на самом этом уровне — не ловушка, а то, за что стоп и спрятан.
    """
    if not market or not stop:
        return ''
    threshold = stop_hunt_pct(atr_pct)
    for price, pool_side, source in _pools(market):
        if stop_level and abs(price - stop_level) / price * 100 < 0.05:
            continue
        if side == 'LONG' and pool_side == 'SSL' and price > stop:
            gap = (price - stop) / price * 100
            if gap <= threshold:
                return (f'стоп {stop:.6g} на {gap:.2f}% ниже скопления стопов '
                        f'лонгов {price:.6g} ({source}) — снимут вместе с ними')
        if side == 'SHORT' and pool_side == 'BSL' and price < stop:
            gap = (stop - price) / price * 100
            if gap <= threshold:
                return (f'стоп {stop:.6g} на {gap:.2f}% выше скопления стопов '
                        f'шортов {price:.6g} ({source}) — снимут вместе с ними')
    return ''


# Подписи уровней, за которыми стоят чужие стопы. Вход на таком уровне без
# условия «вынос и возврат» — продажа в дно выноса или покупка на его
# вершине: 20.09.2026 BNB, шорт «на EQL, где стопы лонгов».
POOL_KINDS = ('равные экстремумы', 'скопление', 'нетронутое скопление',
              'максимум вчера', 'минимум вчера', 'максимум недели', 'минимум недели',
              'максимум Азии', 'минимум Азии', 'край ликвидаций')
# Подписи, при которых уровень — зона инициатора, и вход от него законен,
# даже если рядом сошёлся пул.
ZONE_KINDS = ('зоны', 'имбаланса', 'ордер-блок', 'брейкер', 'свинг', 'POC')


# Чьи стопы стоят за пулом — по подписи. Минимумы, EQL, ликвидации лонгов —
# стопы лонгов (SSL); максимумы, EQH, ликвидации шортов — стопы шортов (BSL).
_SSL_WORDS = ('минимум', 'EQL', 'ликвидаций лонгов')
_BSL_WORDS = ('максимум', 'EQH', 'ликвидаций шортов')


def _pool_sides(kind):
    sides = set()
    if any(w in kind for w in _SSL_WORDS):
        sides.add('SSL')
    if any(w in kind for w in _BSL_WORDS):
        sides.add('BSL')
    return sides


def entry_on_pool(parsed, levels):
    """
    Вход стоит на пуле чужих стопов. Описание или ''.

    Пул НЕ ТОЙ стороны — всегда отказ: лонг на EQH или шорт на EQL — это
    покупка на вершине выноса или продажа в его дно. 20.09.2026 BNB, второй
    заход: шорт от EQL «после выноса с возвратом ВВЕРХ» — после такого
    выноса рынок идёт вверх, а не вниз. Пул СВОЕЙ стороны (лонг на EQL,
    шорт на EQH) законен только с условием sweep_reclaim: дождаться выноса
    и возврата — и войти по ходу возврата.
    """
    entry_id = (parsed.get('ids') or {}).get('entry')
    level = next((lv for lv in (levels or []) if lv.get('id') == entry_id), None)
    if not level:
        return ''
    kind = str(level.get('kind', ''))
    names = [n.strip() for n in kind.split('/')]
    if any(any(z in n for z in ZONE_KINDS) for n in names):
        return ''
    if not any(any(p in n for p in POOL_KINDS) for n in names):
        return ''
    side = parsed.get('side')
    sides = _pool_sides(kind)
    own = 'SSL' if side == 'LONG' else 'BSL'
    wrong = 'BSL' if side == 'LONG' else 'SSL'
    head = f'вход {entry_id} {level.get("price"):.6g} — {kind}: там чужие стопы'
    if wrong in sides and own not in sides:
        return (f'{head}; {"лонг на стопах шортов — покупка на вершине выноса" if side == "LONG" else "шорт на стопах лонгов — продажа в дно выноса"}; '
                f'для {side} это цель, а вход — из зоны инициатора')
    if (parsed.get('trigger_when') or 'now') == 'sweep_reclaim':
        return ''
    return f'{head}, это цель или место выноса; вход — из зоны инициатора или после sweep_reclaim'


def _mentioned_levels(text, levels):
    """Идентификаторы уровней, названные в тексте — по имени или по цене."""
    import re
    found = set(re.findall(r'\bL\d{1,2}\b', text or ''))
    numbers = [float(x) for x in re.findall(r'\d+(?:\.\d+)?', text or '')]
    for lv in levels or []:
        price = float(lv.get('price') or 0)
        if price and any(abs(n - price) / price < 0.0005 for n in numbers):
            found.add(lv.get('id'))
    return found


def justification_mismatch(parsed, levels):
    """
    Обоснование говорит об одном уровне, план — о другом. Описание или ''.

    20.09.2026 BNB: в мысли и в tp_why цель — L12 746.5 (EQL), а в плане —
    L20 710.4: грамматика не пустила близкую цель по R:R, и модель молча
    взяла первую разрешённую, продолжая писать про 746.5. План, в который
    модель сама не верит, хуже отказа.
    """
    ids = parsed.get('ids') or {}
    checks = (('stop_why', 'стоп', {ids.get('stop')}),
              ('tp_why', 'цель', set(ids.get('tp') or [])))
    for field, name, chosen in checks:
        chosen = {c for c in chosen if c}
        mentioned = _mentioned_levels(parsed.get(field, ''), levels)
        if not chosen or not mentioned:
            continue
        if not (mentioned & chosen):
            return (f'{name} в плане — {", ".join(sorted(chosen))}, а обоснование говорит о '
                    f'{", ".join(sorted(mentioned))}: модель выбрала не тот уровень, о котором думала')
    return ''


def stop_inside_entry_zone(side, entry, stop_level, market):
    """
    Вход из зоны (ордер-блок, имбаланс), а стоп — внутри неё. Описание или ''.

    Зона держит цену целиком: стоп между входом и её дальним краем снимает
    обычный заход внутрь зоны, который идею не отменяет. Стоп ставится ЗА
    дальний край — для лонга ниже низа, для шорта выше верха.
    """
    if not market or not entry or not stop_level:
        return ''
    zones = []
    for z in (market.get('pois') or []):
        if z.get('top') and z.get('bottom'):
            zones.append((float(z['bottom']), float(z['top']), 'зона'))
    for g in (market.get('fvgs') or []):
        if g.get('top') and g.get('bottom'):
            zones.append((float(g['bottom']), float(g['top']), 'имбаланс'))
    tol = entry * 0.0005
    for bottom, top, name in zones:
        if not (bottom - tol <= entry <= top + tol):
            continue
        if side == 'LONG' and stop_level > bottom + tol:
            return (f'вход {entry:.6g} из {name} {bottom:.6g}..{top:.6g}, а стоп '
                    f'{stop_level:.6g} внутри неё — ставить за низ {bottom:.6g}')
        if side == 'SHORT' and stop_level < top - tol:
            return (f'вход {entry:.6g} из {name} {bottom:.6g}..{top:.6g}, а стоп '
                    f'{stop_level:.6g} внутри неё — ставить за верх {top:.6g}')
    return ''


def obstacles_to_target(side, entry, target, market):
    """
    Что стоит между входом и первой целью: плиты, встречные зоны, пулы.

    Не отказ, а список для критика и журнала: плита в стакане может быть
    снята, зона — пробита. Но план, который об этом не знает, хуже плана,
    который знает.
    """
    if not market or not entry or not target:
        return []
    lo, hi = min(entry, target), max(entry, target)

    def between(price):
        return price is not None and lo < float(price) < hi

    out = []
    book = market.get('book') or {}
    walls = book.get('walls_above' if side == 'LONG' else 'walls_below') or []
    for wall in walls:
        if between(wall.get('price')):
            out.append(f"плита {wall['price']:.6g} (×{wall.get('volume_x')})")
    for zone in (market.get('pois') or []):
        against = 'BEARISH' if side == 'LONG' else 'BULLISH'
        if zone.get('direction') == against and zone.get('bottom') is not None:
            if between(zone['bottom']) or between(zone['top']):
                out.append(f"{zone.get('type', 'зона').lower()} "
                           f"{zone['bottom']:.6g}..{zone['top']:.6g} против")
    for price, pool_side, source in _pools(market):
        if between(price):
            out.append(f"скопление {price:.6g} ({source})")
    liq = market.get('liquidations') or {}
    for cluster in (liq.get('above' if side == 'LONG' else 'below') or []):
        if between(cluster.get('from')) or between(cluster.get('to')):
            out.append(f"ликвидации {cluster['from']:.6g}..{cluster['to']:.6g} "
                       f"(оценка, {cluster.get('share_pct', 0):.0f}%)")
    return out[:6]


def check(parsed, levels, answer=None, market=None, min_stop=None, atr_pct=None):
    """
    Проверяет разобранный ответ. Возвращает решение со всеми числами.

    Ничего не «исправляет»: сомнительный сетап отвергается, а не подгоняется.
    Подгонка здесь означала бы, что в журнал попадёт сделка, которой модель не
    предлагала, и разобрать потом будет нечего.

    `answer` — сырой текст. Нужен ровно для одного: назвать поломку по-разному,
    когда ответ оборвался и когда он пришёл целиком, но не разобрался. None
    означает «текста не дали» — тогда имя остаётся общим, потому что пустая
    строка и несказанное это разные вещи, и выдавать одно за другое нельзя.
    """
    if parsed is None:
        if answer is None:
            return _refusal('ответ не разобран', 'модель вернула не JSON')
        if truncated(answer):
            # Хвост в детали — по нему сразу видно, на каком поле оборвалось.
            return _refusal('ответ обрезан',
                            'окно контекста кончилось раньше ответа; '
                            f'конец: …{answer.strip()[-80:]}')
        if not answer.strip():
            return _refusal('ответ пуст', 'модель не вернула ни одного токена')
        return _refusal('ответ не разобран',
                        f'модель вернула не JSON: {answer.strip()[:80]}…')

    votes = sum(1 for f in FACTORS if parsed['confluence'].get(f))
    base = {'confluence': parsed['confluence'], 'votes': votes,
            'thought': parsed.get('thought', ''),
            'why': parsed.get('why', ''), 'risk': parsed.get('risk', ''),
            'stop_why': parsed.get('stop_why', ''), 'tp_why': parsed.get('tp_why', ''),
            'regime': parsed.get('regime', ''),
            'analysis': parsed.get('analysis', ''),
            'bias': parsed.get('bias', ''),
            'trigger': parsed.get('trigger', ''),
            'trigger_when': parsed.get('trigger_when', 'now'),
            'trigger_level': parsed.get('trigger_level'),
            'trigger_id': parsed.get('trigger_id'),
            'alt': parsed.get('alt', '')}

    if parsed['decision'] != 'enter':
        return _refusal('модель пропустила', parsed.get('why', ''), base)

    entry, stop_level = parsed.get('entry'), parsed.get('stop')
    # Одна и та же цель дважды — это одна цель: грамматика повтор не
    # запрещает, а брокер делил бы позицию на две одинаковые части.
    targets = []
    for t in (parsed.get('targets') or []):
        if t is not None and t not in targets:
            targets.append(t)
    if not entry or not stop_level or not targets:
        return _refusal('уровень не найден',
                        'ответ ссылается на то, чего нет в разметке', base)

    if votes < MIN_CONFLUENCE:
        return _refusal('мало конфлюенса', f'{votes} из {len(FACTORS)}', base)

    side = parsed.get('side')
    if not _geometry_ok(side, entry, stop_level, targets):
        return _refusal('геометрия неверна',
                        f'{side}: вход {entry:.6g}, стоп {stop_level:.6g}, '
                        f'цели {[round(t, 6) for t in targets]}', base)

    # СТОП — ЗА УРОВНЕМ, А НЕ НА НЁМ. Модель называет структуру, которая
    # защищает идею; код отступает за неё на буфер охоты за стопами (база
    # плюс доля ATR). До 20.09.2026 стоп стоял ровно на уровне — то есть
    # ровно там, где стоят чужие стопы, которые снимают первыми.
    buffer = stop_hunt_pct(atr_pct)
    stop = stop_level * (1 - buffer / 100) if side == 'LONG' else stop_level * (1 + buffer / 100)
    inside = stop_inside_entry_zone(side, entry, stop_level, market)
    if inside:
        return _refusal('стоп внутри зоны входа', inside, base)
    pooled = entry_on_pool(parsed, levels)
    if pooled:
        return _refusal('вход на пуле стопов', pooled, base)
    mismatch = justification_mismatch(parsed, levels)
    if mismatch:
        return _refusal('обоснование не о том плане', mismatch, base)

    stop_pct = abs(entry - stop) / entry * 100
    floor = min_stop if min_stop is not None else llm_context.min_stop_pct(atr_pct)
    if floor and stop_pct < floor:
        return _refusal('стоп теснее минимального',
                        f'{stop_pct:.2f}% при минимуме {floor:.2f}%', base)

    rr = abs(targets[0] - entry) / abs(entry - stop)
    if rr < MIN_RR:
        return _refusal('низкое отношение', f'R:R {rr:.2f} при минимуме {MIN_RR}',
                        base)

    # ГЕОМЕТРИЯ ПРОТИВ ЛИКВИДНОСТИ — КОДОМ, А НЕ ТОЛЬКО КРИТИКОМ. Стоп,
    # стоящий вплотную под скоплением стопов, снимут вместе с ними; это
    # проверяется арифметикой, и отдавать её модели значило бы платить пять
    # минут за то, что считается за микросекунду. Препятствия на пути к цели
    # не запрещают вход — они уходят критику и в журнал.
    hunted = stop_in_liquidity(side, stop, market, atr_pct, stop_level=stop_level)
    if hunted:
        return _refusal('стоп в скоплении стопов', hunted, base)

    # Условие входа, которое противоречит идее: лонг «после закрытия НИЖЕ»
    # уровня не ниже входа — это ожидание инвалидации, а не подтверждения.
    # 19 сентября 2026 DOGE: лонг от L5 с условием close_below L5.
    against = trigger_against_idea(side, entry, parsed)
    if against:
        return _refusal('условие противоречит входу', against, base)
    base['obstacles'] = obstacles_to_target(side, entry, targets[0], market)

    cost_r = cost_in_r(entry, stop)
    ev = expected_value(parsed['p'], rr, cost_r)
    numbers = {'side': side, 'entry': entry, 'stop': stop, 'stop_level': stop_level,
               'stop_buffer_pct': round(buffer, 3), 'targets': targets,
               'inval': parsed.get('inval'), 'p': parsed['p'],
               'rr': round(rr, 2), 'cost_r': round(cost_r, 4),
               'ev': round(ev, 4), 'stop_pct': round(stop_pct, 2),
               'ids': parsed.get('ids', {})}
    if ev <= 0:
        return _refusal('ожидание не положительно',
                        f'EV {ev:.3f} при вероятности {parsed["p"]:.2f}, '
                        f'R:R {rr:.2f}, издержках {cost_r:.3f}R',
                        {**base, **numbers})

    return {'ok': True, 'gate': '', 'detail': '', **base, **numbers}


def decide(pair, df, ask, news=None, at=None, max_tokens=None, market=None,
           history=None):
    """
    Полный проход: разметка -> грамматика -> модель -> проверка.

    `ask(prompt, grammar, max_tokens)` возвращает текст ответа. Отдельным
    параметром, а не импортом llama_cpp: так проверки обходятся без модели,
    а замена модели не трогает эту логику.

    MAX_TOKENS ПО УМОЛЧАНИЮ ПУСТ, И ЭТО ИСПРАВЛЕНИЕ, А НЕ МЕЛОЧЬ. Здесь стояло
    400 — своё число, не связанное с настройкой. strategy_llm зовёт decide без
    этого параметра, поэтому боевой путь получал 400 всегда, чем бы ни был
    LLM_MAX_TOKENS: поднятый до 1200 предел не доходил до модели вовсе, и
    поднимали его дважды, глядя на обрезанные ответы.

    Пустое значение означает «решает настройка»: предел один и лежит в одном
    месте. Правило, записанное дважды, расходится.

    market — снимок llm_market.snapshot, собранный ЗАРАНЕЕ и в цикле: здесь
    запросов к бирже быть не может, этот код идёт в потоке разбора минутами,
    а клиент биржи на параллельные обращения не рассчитан.
    """
    context = llm_context.build(pair, df, at=at, news=news, market=market,
                                history=history)
    levels = context['levels']
    if not levels:
        return _refusal('нет разметки', 'уровней на этом баре не найдено')

    facts = context['facts']
    grammar = llm_grammar.build([lv['id'] for lv in levels],
                                prices=[lv['price'] for lv in levels],
                                min_stop_pct=facts.get('min_stop_pct') or llm_context.min_stop_pct(),
                                min_rr=MIN_RR,
                                stop_buffer_pct=stop_hunt_pct(facts.get('atr_pct')),
                                think_chars=getattr(config, 'LLM_THINK_CHARS', 0))
    # Разметка без задачи — таблица без вопроса. Первый прогон по живому рынку
    # отдавал модели только context['text'], и она отвечала «no news, no
    # comment»: её просто не спросили.
    import llm_prompt
    try:
        answer = ask(llm_prompt.build(context['text']), grammar, max_tokens)
    except Exception as exc:                           # noqa: BLE001
        # ИМЯ ОТКАЗА ПРИНОСИТ С СОБОЙ САМО ИСКЛЮЧЕНИЕ. «Модель не загрузилась»
        # и «вопрос не оставил места ответу» лечатся в разных местах, и общее
        # имя на двоих отправило бы чинить не туда — как уже отправило имя
        # «модель вернула не JSON», за которым стояло тесное окно контекста.
        #
        # Метка, а не отдельный класс исключения: ask передаётся параметром
        # именно ради того, чтобы этот модуль ничего не знал о llama_cpp, и
        # импорт ради одного имени свёл бы развязку на нет.
        log(f'⚠️ {pair}: модель не ответила — {exc}')
        return _refusal(getattr(exc, 'llm_gate', 'модель недоступна'),
                        str(exc)[:300])

    verdict = check(parse(answer, levels), levels, answer,
                    market=facts.get('market'),
                    min_stop=facts.get('min_stop_pct'), atr_pct=facts.get('atr_pct'))
    verdict['pair'] = pair
    verdict['levels'] = levels
    verdict['raw'] = answer
    verdict['data_gap_bars'] = int(facts.get('data_gap_bars') or 0)
    if verdict.get('ok') and critic_enabled():
        verdict = review(verdict, context['text'], ask)
    return verdict


def critic_enabled():
    """
    Включён ли проверяющий. Тумблер на панели (settings_store), запасной
    ответ — config.LLM_CRITIC. Оба должны разрешать: .env выключает
    жёстко, панель — на время.
    """
    if not getattr(config, 'LLM_CRITIC', True):
        return False
    try:
        import settings_store
        return bool(settings_store.critic_enabled())
    except Exception:                              # noqa: BLE001
        return True


def plan_text(verdict):
    """План аналитика в том виде, в каком его читает проверяющий."""
    ids = verdict.get('ids') or {}
    targets = ', '.join(f'{t:.6g}' for t in verdict.get('targets') or [])
    lines = [
        f"Направление: {verdict.get('side')}",
        f"Вход: {ids.get('entry')} {verdict.get('entry'):.6g}   "
        f"Стоп: {ids.get('stop')} {verdict.get('stop'):.6g} "
        f"({verdict.get('stop_pct')}%)   Цели: {targets}",
        f"Инвалидация: {verdict.get('inval'):.6g}" if verdict.get('inval') else '',
        f"Условие входа: {verdict.get('trigger') or 'сейчас'}",
        f"Вероятность по аналитику: {verdict.get('p')}   R:R {verdict.get('rr')}   "
        f"EV {verdict.get('ev')}R   факторы {verdict.get('votes')}/5",
        ('Между входом и первой целью (посчитано кодом): '
         + '; '.join(verdict['obstacles'])) if verdict.get('obstacles') else
        'Между входом и первой целью код препятствий не нашёл',
        f"Режим: {verdict.get('regime', '')}",
        f"Разбор: {verdict.get('analysis', '')}",
        f"Почему: {verdict.get('why', '')}",
        f"Стоп за: {verdict.get('stop_why', '')}",
        f"Цель там: {verdict.get('tp_why', '')}",
        f"Риск по аналитику: {verdict.get('risk', '')}",
    ]
    return '\n'.join(line for line in lines if line)


def review(verdict, context_text, ask):
    """
    Второе мнение о плане: тот же движок, другая роль.

    ТОЛЬКО НА «ВОЙТИ». Проверять каждый отказ значило бы удвоить время
    каждого разбора ради ответа «да, отказ верный». Входы редки, и пять
    минут на каждый — цена, которую стоит платить: ошибочный вход стоит
    дороже.

    Отклонение — полноценный отказ с именем «критик отклонил»; всё, что
    сказал аналитик, остаётся в вердикте и уходит в журнал вместе с
    возражениями. Поломка критика входа не отменяет и не подтверждает —
    она записывается как поломка, а решение остаётся за проверками кода.
    """
    import llm_grammar
    import llm_prompt
    try:
        answer = ask(llm_prompt.build_critic(context_text, plan_text(verdict)),
                     llm_grammar.critic(), None)
        data = json.loads(answer)
        if not isinstance(data, dict) or data.get('verdict') not in ('confirm', 'reject'):
            raise ValueError(f'ответ критика не разобран: {str(answer)[:80]}')
    except Exception as exc:                           # noqa: BLE001
        log(f'⚠️ {verdict.get("pair")}: проверяющий не ответил — {exc}')
        verdict['critic'] = {'verdict': 'broken', 'issues': str(exc)[:300], 'worst': ''}
        return verdict

    verdict['critic'] = {'verdict': data['verdict'],
                         'issues': data.get('issues', ''),
                         'worst': data.get('worst', '')}
    if data['verdict'] == 'reject':
        out = dict(verdict)
        out.update({'ok': False, 'gate': 'критик отклонил',
                    'detail': (data.get('worst') or data.get('issues') or '')[:300]})
        return out
    return verdict
