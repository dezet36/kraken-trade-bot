"""
Разметка рынка в том виде, в каком её читает языковая модель.

ЗАЧЕМ ЭТОТ ФАЙЛ. Модель не умеет смотреть на свечи. Дай ей двести строк OHLCV
текстом — она вернёт правдоподобную цену, которой в данных нет, и объяснение к
ней будет таким же складным, как к настоящей. Отличить это глазом невозможно.

Поэтому числа считает код, а модель получает ГОТОВЫЙ ПРОНУМЕРОВАННЫЙ СПИСОК
уровней и выбирает из него. Выдуманная цена становится невозможной не по
договорённости, а технически: грамматика разрешает только идентификаторы
отсюда.

ЧТО СЮДА ВХОДИТ И ПОЧЕМУ ИМЕННО ЭТО

    скопления ликвидности   liquidity.find_pools — где стоят чужие стопы
    пивоты                  liquidity.pivots — экстремумы, не вошедшие в пулы
    размах                  market_regime.volatility_pct
    расстановка участников  positioning: OI, фандинг, лонг/шорт, премия

Последняя строка здесь главная. Разбор семи семейств ценовых стратегий дал
один пограничный выживший, и общее у них ровно одно: все предсказывают
направление по форме графика. Позиционирование отвечает на другой вопрос —
кто и как расставлен. Оно копилось с первого дня и до сих пор НЕ ЧИТАЛОСЬ
НИКЕМ: этот модуль его первый потребитель.

ЗАГЛЯДЫВАНИЯ ВПЕРЁД НЕТ, И ЭТО ГЛАВНАЯ ОПАСНОСТЬ. Пивот определяется окном
±PIVOT_BARS и становится известен лишь через PIVOT_BARS баров после себя.
Разметка, собранная без учёта этого, содержит будущее — и стратегия, настроенная
на неё, в бою не воспроизводится. Поэтому `at` протаскивается насквозь: и в
find_pools, и в чтение позиционирования. Проверка на это — test_llm_context.

ВТОРОЙ ЗАХОД — СНИМОК РЫНКА (llm_market). Первые живые разборы дали
конфлюенс 1/5 и 3/5 не потому, что рынок был пуст, а потому что модель
спрашивали о профиле объёма и потоке, которых ей не показывали. Снимок
приносит их: профиль объёма, поглощение, дельту агрессора, стакан и структуру
из пакета smc. Собирается он В ЦИКЛЕ, в нём есть запросы к бирже, и сюда
приходит готовым — этот модуль его только печатает. Отсутствие снимка или
любого его куска — законное состояние: на месте пропажи стоит прочерк, и в
шапке каждого блока сказано, факт это или прокси.
"""

import config
import market_regime
import positioning
from liquidity import core as liq
from logger import log

# Сколько уровней максимум уходит в модель. Больше — не лучше: список на сорок
# позиций модель разбирает хуже, чем на десять, а грамматика вырастает линейно.
MAX_LEVELS = 12

# Ближе этого к текущей цене уровни не показываем: вход вплотную к цене даёт
# стоп, который не проходит предел издержек, и модель будет исправно предлагать
# то, что предохранитель всё равно отвергнет.
MIN_DISTANCE_PCT = 0.15


def min_stop_pct():
    """
    Минимальная дистанция стопа, при которой сделка окупает комиссии.

    Выводится из предела расхода: доля = ставка_туда-обратно / стоп, значит
    стоп = ставка / доля. При 0.075% туда-обратно и пределе 5% риска это 1.5%.

    Ноль в пределе означает «не проверять» — тогда и минимума нет.
    """
    limit = getattr(config, 'MAX_ENTRY_COST_SHARE_PCT', 0) or 0
    if limit <= 0:
        return 0.0
    return round(config.ENTRY_COST_ROUND_TRIP / (limit / 100) * 100, 3)


def _pools(pivot_list, at, atr_now):
    """Скопления с обеих сторон, помеченные стороной."""
    out = []
    # Имя по стороне ПИВОТОВ, а не по положению относительно цены: скопление
    # максимумов вполне может стоять ниже текущей цены, и подпись «сверху»
    # тогда прямо вводит в заблуждение того, кто читает список.
    for side, name in (('H', 'скопление максимумов'), ('L', 'скопление минимумов')):
        for pool in liq.find_pools(pivot_list, at, atr_now, side):
            out.append({'price': pool['price'], 'kind': name,
                        'touches': pool['touches'], 'last': pool['last']})
    return out


def _lone_pivots(pivot_list, at, pools, span):
    """
    Экстремумы, не вошедшие ни в одно скопление.

    Одиночный пивот — тоже уровень, просто более слабый: за ним стоят стопы
    одного захода, а не трёх. Без них список из одних скоплений на спокойном
    рынке оказывается пустым, и модели не из чего выбирать.
    """
    out = []
    for index, price, side, known_at in pivot_list:
        if known_at > at:
            continue                       # ещё не существует на баре решения
        if any(abs(price - p['price']) <= span for p in pools):
            continue
        out.append({'price': price, 'touches': 1, 'last': index,
                    'kind': 'пивот-максимум' if side == 'H' else 'пивот-минимум'})
    return out


def levels(df, at=None):
    """
    Пронумерованный список уровней: сверху вниз, L1 — самый высокий.

    Нумерация ПО ЦЕНЕ, а не по силе. Так модель видит карту рынка в привычном
    порядке и не путает «первый по списку» с «главный».
    """
    high = df['high'].values
    low = df['low'].values
    close = df['close'].values
    at = len(close) - 1 if at is None else at

    atr = liq.atr_series(high, low, close)
    atr_now = float(atr[at]) if atr[at] == atr[at] else 0.0
    if atr_now <= 0:
        return [], 0.0

    pivot_list = liq.pivots(high, low)
    pools = _pools(pivot_list, at, atr_now)
    span = liq.params.POOL_TOLERANCE_ATR * atr_now
    found = pools + _lone_pivots(pivot_list, at, pools, span)

    price_now = float(close[at])
    near = [lv for lv in found
            if abs(lv['price'] - price_now) / price_now * 100 >= MIN_DISTANCE_PCT]

    # ОТБОР ИДЁТ ПО КАЖДОЙ СТОРОНЕ ОТДЕЛЬНО. Обе поправки ниже сделаны по
    # живым свечам BTC, и ни одна не была видна на глаз до запуска.
    #
    # 1. Скопления вперёд одиночных пивотов. Отбор по одной близости к цене
    #    выдавил все четыре скопления (до пяти касаний) в хвост списка, а
    #    первые восемь мест заняли одиночные пивоты. Одиночный пивот — след
    #    одного захода, скопление из пяти — место, где стопы копились пять раз.
    #
    # 2. Половина мест сверху, половина снизу. Сортировка по силе на той же
    #    выборке оставила ВСЕ ДВЕНАДЦАТЬ уровней ниже цены: сильные скопления
    #    там и стояли. Список без верхней стороны бесполезен — у лонга нет ни
    #    цели, ни стопа у шорта, и модель вынуждена выбирать из невозможного.
    #    Рынок бывает односторонним, но меню решений — не должно.
    half = max(1, MAX_LEVELS // 2)
    above = sorted((lv for lv in near if lv['price'] > price_now),
                   key=lambda lv: (-lv['touches'], lv['price'] - price_now))[:half]
    below = sorted((lv for lv in near if lv['price'] <= price_now),
                   key=lambda lv: (-lv['touches'], price_now - lv['price']))[:half]
    near = sorted(above + below, key=lambda lv: -lv['price'])

    for number, level in enumerate(near, start=1):
        level['id'] = f'L{number}'
        level['dist_pct'] = round((level['price'] - price_now) / price_now * 100, 2)
    return near, atr_now


def _positioning_facts(pair, upto=None):
    """Расстановка участников. Отсутствующий ряд остаётся None, а не нулём."""
    return {
        'oi': positioning.latest('open_interest', pair, upto),
        'oi_24h': positioning.change_pct('open_interest', pair, 24, upto),
        'oi_4h': positioning.change_pct('open_interest', pair, 4, upto),
        'funding': positioning.latest('funding', pair, upto),
        'long_short': positioning.latest('long_short', pair, upto),
        'long_short_24h': positioning.change_pct('long_short', pair, 24, upto),
        'premium': positioning.latest('premium', pair, upto),
    }


def _fmt(value, digits=2, suffix=''):
    """Число или прочерк. Прочерк означает «не знаем», и это видно."""
    if value is None:
        return '—'
    return f'{value:.{digits}f}{suffix}'


def _signed(value):
    """Процент со знаком: «+2.00%» и «−0.30%» читаются, «2.00%» — нет."""
    return '—' if value is None else f'{value:+.2f}%'


def build(pair, df, at=None, news=None, market=None, history=None):
    """
    Всё, что нужно модели для одного решения.

    Возвращает {'levels', 'text', 'facts'}. `text` уходит в промпт, `levels`
    нужен грамматике и разбору ответа: по идентификатору восстанавливается цена.

    news — готовый дайджест или None. None означает «фона нет», и в тексте это
    прямо сказано: иначе модель начнёт рассуждать о новостях, которых не видела.

    market — снимок llm_market.snapshot или None. Печатается всегда, даже
    пустым: блок с прочерками говорит модели «не измерено», а отсутствие
    блока она прочтёт как «на рынке этого нет».

    history — прошлые строки журнала разборов (llm_journal.last) или None.
    """
    found, atr_now = levels(df, at)
    if not found:
        return {'levels': [], 'text': '', 'facts': {}}

    close = df['close'].values
    at = len(close) - 1 if at is None else at
    price_now = float(close[at])

    upto = None
    try:
        upto = int(df['timestamp'].iloc[at].timestamp() * 1000)
    except Exception:                                  # noqa: BLE001
        pass                                           # без отсечки — только для живого бара

    atr_pct = market_regime.volatility_pct(df['high'].values, df['low'].values,
                                           close[:at + 1])
    facts = _positioning_facts(pair, upto)
    facts.update({'price': price_now, 'atr_pct': atr_pct,
                  'min_stop_pct': min_stop_pct()})

    # Ход цены — то, чего модель просила в первом же живом разборе: «данные
    # не показывают динамику цены, только уровень». Без него рост открытого
    # интереса нечем сопоставить, а именно из этого сопоставления берутся
    # противоречия, ради которых её и спрашивают.
    def change(bars):
        if at < bars or close[at - bars] <= 0:
            return None
        return (price_now / float(close[at - bars]) - 1) * 100
    facts['change_4h'] = change(4)
    facts['change_24h'] = change(24)

    lines = [
        f'Пара: {pair}   Цена: {price_now:.6g}   '
        f'Размах (ATR): {_fmt(atr_pct, 2, "%")}   {_when(df, at)}',
        f'Ход цены: за 4ч {_signed(facts["change_4h"])}   '
        f'за 24ч {_signed(facts["change_24h"])}',
        f'Минимальный стоп по издержкам: {facts["min_stop_pct"]:.2f}%',
        '',
        'УРОВНИ (сверху вниз, расстояние от текущей цены)',
    ]
    for level in found:
        touches = (f", касаний {level['touches']}" if level['touches'] > 1 else '')
        lines.append(f"  {level['id']:<4}{level['price']:>14.6g}   "
                     f"{level['dist_pct']:+6.2f}%   {level['kind']}{touches}")

    lines += [
        '',
        'РАССТАНОВКА УЧАСТНИКОВ',
        f"  Открытый интерес: {_fmt(facts['oi'], 0)}   "
        f"за 4ч {_fmt(facts['oi_4h'], 2, '%')}   за 24ч {_fmt(facts['oi_24h'], 2, '%')}",
        f"  Фандинг: {_fmt(facts['funding'], 6)}"
        f"   (плюс — платят лонги)",
        f"  Лонг/шорт: {_fmt(facts['long_short'], 3)}   "
        f"за 24ч {_fmt(facts['long_short_24h'], 2, '%')}",
        f"  Премия перпа: {_fmt(facts['premium'], 6)}",
        '',
    ]

    lines += market_lines(market, price_now)
    facts['market'] = market

    # Новостной фон печатается только когда он есть. Блок «не получен»
    # стоял в каждом вопросе с первого дня и ни разу не был заполнен:
    # источника нет, а токены и внимание модели на него уходили.
    if news:
        lines += ['НОВОСТНОЙ ФОН', f"  {news}", '']

    lines += _history_lines(pair, history)

    return {'levels': found, 'text': '\n'.join(lines), 'facts': facts}


def _when(df, at):
    """Время бара решения и торговая сессия — из правил smc, не свои."""
    try:
        ts = df['timestamp'].iloc[at]
        from smc import sessions
        zone = sessions.killzone_of(ts) or 'вне сессий'
        import pandas as pd
        stamp = pd.Timestamp(ts)
        stamp = stamp.tz_localize('UTC') if stamp.tzinfo is None else stamp.tz_convert('UTC')
        return f'Время: {stamp.strftime("%a %H:%M")} UTC, {zone}'
    except Exception:                                  # noqa: BLE001
        return ''


def _history_lines(pair, history):
    """
    Прошлые разборы этой пары — решение и причина. Иначе модель каждый раз
    видит пару впервые и не может ни подтвердить прошлую мысль, ни признать,
    что рынок её опроверг.

    history — список строк журнала (llm_journal.last) или None. Берутся до
    двух последних по этой паре.
    """
    rows = [r for r in (history or []) if r.get('pair') == pair][:2]
    if not rows:
        return []
    out = ['ПРОШЛЫЕ РАЗБОРЫ ЭТОЙ ПАРЫ']
    for r in rows:
        when = (r.get('at') or '')[11:16]
        decision = ('вход ' + (r.get('side') or '') if r.get('decision') == 'enter'
                    else 'отказ')
        why = (r.get('why') or r.get('detail') or '')[:160]
        out.append(f"  {when} UTC: {decision} — {why}")
    out.append('')
    return out


# ── Снимок рынка текстом ─────────────────────────────────────────────────────

_DIRECTION = {'BULLISH': 'вверх', 'BEARISH': 'вниз'}
_SIDE = {'BSL': 'над ценой, стопы шортов', 'SSL': 'под ценой, стопы лонгов'}
_RANGE_SIDE = {'DISCOUNT': 'дисконт', 'PREMIUM': 'премия',
               'EQUILIBRIUM': 'равновесие'}
_NONE = '  —'


def _pct(price, price_now):
    """Расстояние уровня от цены, со знаком."""
    if price is None or not price_now:
        return '—'
    return f'{(float(price) / price_now - 1) * 100:+.2f}%'


def _p(price):
    """Цена в шесть значащих цифр или прочерк."""
    return '—' if price is None else f'{float(price):.6g}'


def _profile_lines(profile, price_now):
    if not profile:
        return [_NONE]
    thin = ', '.join(f'{_p(lo)}..{_p(hi)}' for lo, hi in (profile.get('thin') or []))
    return [
        f"  Пик объёма (POC) {_p(profile.get('poc'))} "
        f"({_pct(profile.get('poc'), price_now)})   "
        f"зона стоимости {_p(profile.get('value_low'))}"
        f"..{_p(profile.get('value_high'))}   "
        f"по {profile.get('bars', '—')} свечам",
        f"  Разрежения (цена проскакивает): {thin or 'нет'}",
    ]


def _absorption_lines(rows):
    if not rows:
        return [_NONE]
    return [f"  {row.get('side')} {_p(row.get('price'))}   "
            f"объём ×{row.get('volume_x')}   {row.get('bars_ago')} св. назад"
            for row in rows]


def _delta_lines(delta):
    if not delta:
        return [_NONE]
    if delta.get('stale'):
        return [f"  — (последняя запись {delta.get('fresh_min')} мин назад, "
                f"не измерено)"]

    def cell(name):
        win = delta.get(name)
        if not win or win.get('share_pct') is None:
            return '—'
        # ПОКРЫТИЕ ПЕЧАТАЕТСЯ РЯДОМ С ЧИСЛОМ. Лента берётся опросом раз в
        # четыре минуты по тысяче сделок: у BTC это одна-две минуты из
        # четырёх, и «−18% за час» может стоять на пятнадцати минутах из
        # шестидесяти. Без покрытия модель читала его как факт и отказывала.
        minutes = {'h1': 60, 'h4': 240, 'h24': 1440}[name]
        rows = int(win.get('rows') or 0)
        return f"{win['share_pct']:+.1f}% ({min(rows, minutes)}/{minutes} мин)"

    fresh = delta.get('fresh_min')
    lines = [f"  Перевес агрессора от оборота:  1ч {cell('h1')}   "
             f"4ч {cell('h4')}   24ч {cell('h24')}"
             + (f"   (данные {fresh} мин назад)" if fresh is not None else '')]
    if delta.get('divergence'):
        lines.append(f"  РАСХОЖДЕНИЕ: {delta['divergence']}")
    return lines


def _book_lines(book):
    if not book:
        return [_NONE]

    def walls(rows):
        if not rows:
            return 'нет'
        return ', '.join(f"{_p(w['price'])} (×{w['volume_x']}, "
                         f"{w['dist_pct']:+.3f}%)" for w in rows)

    return [
        f"  Перекос bid/ask {book.get('imbalance', 0):.2f} "
        f"(больше 1 — заявок на покупку больше)   "
        f"спред {book.get('spread_pct', 0):.4f}%",
        f"  Плиты ниже: {walls(book.get('walls_below'))}",
        f"  Плиты выше: {walls(book.get('walls_above'))}",
    ]


def _smc_lines(smc, price_now):
    if not smc:
        return [_NONE]
    lines = []

    brk = smc.get('last_break')
    lines.append(
        f"  Старший порядок: {_DIRECTION.get(smc.get('bias'), '—')}   "
        f"тренд рабочего ТФ: {_DIRECTION.get(smc.get('trend'), '—')}   "
        + (f"последний слом: {brk.get('type')} "
           f"{_DIRECTION.get(brk.get('direction'), '')} у {_p(brk.get('price'))}, "
           f"{brk.get('bars_ago')} св. назад" if brk else 'слома нет'))

    sweep = smc.get('sweep')
    if sweep:
        lines.append(
            f"  Вынос за {_p(sweep.get('price'))} "
            f"({_SIDE.get(sweep.get('side'), sweep.get('side'))}), "
            f"{'с возвратом' if sweep.get('reclaimed') else 'без возврата'}, "
            f"{sweep.get('bars_ago')} св. назад")
    else:
        lines.append('  Свежего выноса за уровень нет')

    fvg = smc.get('fvg')
    if fvg:
        lines.append(f"  Незакрытый имбаланс {_p(fvg.get('bottom'))}"
                     f"..{_p(fvg.get('top'))} "
                     f"({_DIRECTION.get(fvg.get('direction'), '')})")

    leg = smc.get('leg')
    if leg:
        lines.append(
            f"  Нога {_DIRECTION.get(leg.get('direction'), '')} "
            f"{_p(leg.get('from'))} → {_p(leg.get('to'))}   "
            f"равновесие {_p(smc.get('equilibrium'))}   "
            f"цена в зоне: {_RANGE_SIDE.get(smc.get('side_of_range'), '—')}")

    equal = smc.get('equal_levels')
    if equal:
        lines.append('  Равные экстремумы (прокси чужих стопов, не замер): '
                     + ', '.join(f"{_p(e['price'])} {e['source']} "
                                 f"({_pct(e['price'], price_now)})"
                                 for e in equal))

    untapped = smc.get('untapped')
    if untapped:
        lines.append('  Нетронутые скопления: '
                     + ', '.join(f"{_p(u['price'])} "
                                 f"({_SIDE.get(u.get('side'), u.get('side'))}, "
                                 f"{_pct(u['price'], price_now)})"
                                 for u in untapped))
    return lines


_ZONE = {'ORDER_BLOCK': 'ордер-блок', 'BREAKER': 'брейкер',
         'MITIGATION': 'mitigation-блок', 'WICK': 'зона фитиля'}


def _poi_lines(pois, price_now):
    if not pois:
        return [_NONE]
    out = []
    for z in pois:
        where = ('цена внутри' if z.get('inside') else
                 f"{_pct((z['top'] + z['bottom']) / 2, price_now)}")
        out.append(f"  {_ZONE.get(z.get('type'), z.get('type'))} "
                   f"{_DIRECTION.get(z.get('direction'), '')} "
                   f"{_p(z.get('bottom'))}..{_p(z.get('top'))}   {where}   "
                   f"касаний {z.get('touches', 0)}, {z.get('bars_ago')} св. назад")
    return out


def _htf_lines(htf, price_now):
    if not htf:
        return [_NONE]
    out = []
    for key, name in (('htf', '4ч'), ('bias', 'День')):
        frame = htf.get(key)
        if not frame:
            continue
        brk = frame.get('break')
        out.append(
            f"  {name}: тренд {_DIRECTION.get(frame.get('trend'), '—')}   "
            + (f"слом {brk.get('type')} {_DIRECTION.get(brk.get('direction'), '')} "
               f"у {_p(brk.get('price'))}, {brk.get('bars_ago')} св. назад   "
               if brk else 'слома нет   ')
            + f"свинги: верх {_p(frame.get('swing_high'))} "
              f"({_pct(frame.get('swing_high'), price_now)}), "
              f"низ {_p(frame.get('swing_low'))} "
              f"({_pct(frame.get('swing_low'), price_now)})")
    return out or [_NONE]


def _oi_flow_lines(flow):
    if not flow:
        return [_NONE]
    bars = [b or '—' for b in (flow.get('bars') or [])]
    line = f"  Последние {len(bars)} св. (старые → новые): " + ' → '.join(bars)
    if flow.get('change_pct') is not None:
        line += f"   ОИ за окно {flow['change_pct']:+.2f}%"
    return [line]


def _liq_lines(liq):
    if not liq:
        return [_NONE]

    def side(rows):
        if not rows:
            return 'нет'
        return ', '.join(
            (f"{_p(c['from'])}" if c['from'] == c['to'] else f"{_p(c['from'])}..{_p(c['to'])}")
            + f" ({c['dist_pct']:+.1f}%, {c['share_pct']:.0f}% объёма стороны)"
            for c in rows)
    return [f"  Лонги ликвидируются ниже: {side(liq.get('below'))}",
            f"  Шорты ликвидируются выше: {side(liq.get('above'))}"]


def _liq_fact_lines(liq):
    if not liq:
        return [_NONE]
    if liq.get('young_min') is not None:
        return [f"  сбор идёт {liq['young_min']} мин, истории ещё нет "
                f"(событий: {liq.get('events', 0)})"]

    def tot(t):
        return (f"лонгов {t['long_n']} (объём {t['long_size']:.4g}), "
                f"шортов {t['short_n']} (объём {t['short_size']:.4g})")
    out = [f"  За 24ч: {tot(liq['h24'])}   за 4ч: {tot(liq['h4'])}"]
    if liq.get('clusters'):
        out.append('  Где ликвидировали больше всего: ' + ', '.join(
            (f"{_p(c['from'])}" if c['from'] == c['to'] else f"{_p(c['from'])}..{_p(c['to'])}")
            + f" ({c['mostly']}, {c['dist_pct']:+.1f}%, {c['share_pct']:.0f}%)"
            for c in liq['clusters']))
    if liq.get('hours', 24) < 24:
        out.append(f"  История пока {liq['hours']:.0f} ч из 24")
    return out


def _benchmark_lines(bench):
    if not bench:
        return [_NONE]
    line = (f"  BTC за 4ч {_signed(bench.get('btc_4h'))}   "
            f"за 24ч {_signed(bench.get('btc_24h'))}")
    if bench.get('relative_24h') is not None:
        rel = bench['relative_24h']
        line += (f"   пара относительно BTC за 24ч {rel:+.2f} п.п. "
                 f"({'сильнее' if rel > 0 else 'слабее'})")
    return [line]


def market_lines(market, price_now):
    """
    Снимок рынка пятью блоками. Прочерк — «не измерено», не «нет».

    В ШАПКЕ КАЖДОГО БЛОКА СКАЗАНО, ФАКТ ЭТО ИЛИ ДОГАДКА. Стакан и дельта —
    настоящие заявки и настоящие сделки. Поглощение и равные экстремумы —
    прокси по свечам: чужие стопы биржа не отдаёт, а ленты в снимке нет.
    Модель обязана видеть разницу, иначе догадка в её разборе станет фактом.
    """
    market = market or {}
    book = market.get('book') or {}
    reach = (f"видно ±{book['range_pct']:.3g}% от цены"
             if book.get('range_pct') is not None else 'глубина не измерена')
    out = ['ПРОФИЛЬ ОБЪЁМА (по свечам)']
    out += _profile_lines(market.get('profile'), price_now)
    out += ['', 'ПОГЛОЩЕНИЕ (прокси по фитилям на аномальном объёме — догадка)']
    out += _absorption_lines(market.get('absorption'))
    out += ['', 'ДЕЛЬТА АГРЕССОРА (по ленте сделок — факт)']
    out += _delta_lines(market.get('delta'))
    out += ['', f'СТАКАН (факт, но снимок момента: заявку снимают за секунды; '
                f'{reach})']
    out += _book_lines(market.get('book'))
    out += ['', 'СТРУКТУРА (SMC по рабочему ТФ)']
    out += _smc_lines(market.get('smc'), price_now)
    out += ['', 'ЗОНЫ ИНТЕРЕСА (ордер-блоки и брейкеры по 1ч, только живые)']
    out += _poi_lines(market.get('pois'), price_now)
    out += ['', 'СТАРШИЕ ТАЙМФРЕЙМЫ (закрытые свечи)']
    out += _htf_lines(market.get('htf'), price_now)
    out += ['', 'ОТКРЫТЫЙ ИНТЕРЕС ПО СВЕЧАМ (кто набирает, кто выходит)']
    out += _oi_flow_lines(market.get('oi_flow'))
    out += ['', 'КАРТА ЛИКВИДАЦИЙ (ОЦЕНКА по приросту ОИ и типичным плечам — '
                'не факт; чужих позиций биржа не отдаёт)']
    out += _liq_lines(market.get('liquidations'))
    out += ['', 'ЛИКВИДАЦИИ ПО ФАКТУ (поток биржи, последние 24ч)']
    out += _liq_fact_lines(market.get('liq_fact'))
    if market.get('benchmark') is not None:
        out += ['', 'BTC КАК ОРИЕНТИР']
        out += _benchmark_lines(market.get('benchmark'))
    out.append('')
    return out


def price_of(found, level_id):
    """Цена по идентификатору. None, если такого уровня в списке не было."""
    for level in found or []:
        if level.get('id') == level_id:
            return level['price']
    return None
