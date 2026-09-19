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
MIN_RR = 2.0

FACTORS = ('poi', 'vp', 'der', 'smc', 'flow')

# Условия входа, которые умеет исполнять код (см. strategy_llm._armed).
TRIGGERS = ('now', 'close_above', 'close_below')

# Отказы, за которыми стоит НЕИСПРАВНОСТЬ, а не суждение модели. Разница не
# косметическая: «мало конфлюенса» — это работа, законченная ответом, а «ответ
# обрезан» означает, что ответа не было вовсе. Их нельзя ни считать вместе, ни
# показывать одинаково, ни одинаково запоминать: по сетапу, на который модель
# не ответила, спросить надо СНОВА, а не через час.
#
# Список здесь, а не в каждом из трёх мест, где он нужен: стратегии, панели и
# странице. Правило, записанное трижды, расходится — в этом проекте так уже
# вышло с дневным стоп-краном.
BROKEN_GATES = ('ответ обрезан', 'ответ не разобран', 'ответ пуст',
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


def parse(answer, levels):
    """
    Разбирает ответ модели в цены. Возвращает словарь или None при поломке.

    Грамматика гарантирует форму, поэтому ошибок разбора быть не должно. Но
    вызов может пройти и без грамматики — например, если её отключат ради
    отладки, — и тогда сюда придёт что угодно.
    """
    try:
        data = json.loads(answer)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or 'd' not in data:
        return None

    out = {'decision': data.get('d'), 'why': data.get('why', ''),
           'risk': data.get('risk', ''),
           # Разбор и режим рынка модель пишет ПЕРЕД решением — это её
           # рассуждение вслух, и оно нужно и при отказе: по нему видно, что
           # именно она разглядела в данных, а не только чем кончила.
           'regime': data.get('regime', ''),
           'analysis': data.get('analysis', ''),
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
    text = (answer or '').strip()
    return bool(text) and not text.endswith('}')


# Ближе этой доли цены стоп считается «под скоплением»: свип чужих стопов
# обычно проходит 0.05-0.3% за уровень (см. smc SWEEP_MIN_PENETRATION_PCT).
STOP_HUNT_PCT = 0.25


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


def stop_in_liquidity(side, stop, market):
    """
    Стоп вплотную за скоплением чужих стопов. Возвращает описание или ''.

    Для лонга опасен пул стопов лонгов (SSL) ЧУТЬ ВЫШЕ нашего стопа: цену
    повезут снимать его, и наш стоп в 0.1% ниже снимут тем же ходом. Для
    шорта зеркально — BSL чуть ниже стопа. Стоп ЗА пулом с запасом — как
    раз правильное место, и он не трогается.
    """
    if not market or not stop:
        return ''
    for price, pool_side, source in _pools(market):
        if side == 'LONG' and pool_side == 'SSL' and price > stop:
            gap = (price - stop) / price * 100
            if gap <= STOP_HUNT_PCT:
                return (f'стоп {stop:.6g} на {gap:.2f}% ниже скопления стопов '
                        f'лонгов {price:.6g} ({source}) — снимут вместе с ними')
        if side == 'SHORT' and pool_side == 'BSL' and price < stop:
            gap = (stop - price) / price * 100
            if gap <= STOP_HUNT_PCT:
                return (f'стоп {stop:.6g} на {gap:.2f}% выше скопления стопов '
                        f'шортов {price:.6g} ({source}) — снимут вместе с ними')
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


def check(parsed, levels, answer=None, market=None):
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
            'why': parsed.get('why', ''), 'risk': parsed.get('risk', ''),
            'regime': parsed.get('regime', ''),
            'analysis': parsed.get('analysis', ''),
            'trigger': parsed.get('trigger', ''),
            'trigger_when': parsed.get('trigger_when', 'now'),
            'trigger_level': parsed.get('trigger_level'),
            'trigger_id': parsed.get('trigger_id'),
            'alt': parsed.get('alt', '')}

    if parsed['decision'] != 'enter':
        return _refusal('модель пропустила', parsed.get('why', ''), base)

    entry, stop = parsed.get('entry'), parsed.get('stop')
    targets = [t for t in (parsed.get('targets') or []) if t is not None]
    if not entry or not stop or not targets:
        return _refusal('уровень не найден',
                        'ответ ссылается на то, чего нет в разметке', base)

    if votes < MIN_CONFLUENCE:
        return _refusal('мало конфлюенса', f'{votes} из {len(FACTORS)}', base)

    side = parsed.get('side')
    if not _geometry_ok(side, entry, stop, targets):
        return _refusal('геометрия неверна',
                        f'{side}: вход {entry:.6g}, стоп {stop:.6g}, '
                        f'цели {[round(t, 6) for t in targets]}', base)

    stop_pct = abs(entry - stop) / entry * 100
    floor = llm_context.min_stop_pct()
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
    hunted = stop_in_liquidity(side, stop, market)
    if hunted:
        return _refusal('стоп в скоплении стопов', hunted, base)
    base['obstacles'] = obstacles_to_target(side, entry, targets[0], market)

    cost_r = cost_in_r(entry, stop)
    ev = expected_value(parsed['p'], rr, cost_r)
    numbers = {'side': side, 'entry': entry, 'stop': stop, 'targets': targets,
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

    grammar = llm_grammar.build([lv['id'] for lv in levels])
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
                    market=context['facts'].get('market'))
    verdict['pair'] = pair
    verdict['levels'] = levels
    verdict['raw'] = answer
    if verdict.get('ok') and critic_enabled():
        verdict = review(verdict, context['text'], ask)
    return verdict


def critic_enabled():
    """Включён ли проверяющий. Настройка, а не константа: его можно снять."""
    return bool(getattr(config, 'LLM_CRITIC', True))


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
