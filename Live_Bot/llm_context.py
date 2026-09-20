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

import numpy as np
import pandas as pd

import config
import market_regime
import positioning
from liquidity import core as liq

# Сколько уровней максимум уходит в модель. Больше — не лучше: список на сорок
# позиций модель разбирает хуже, чем на десять, а грамматика вырастает линейно.
MAX_LEVELS = 20

# Больше 16 стало 19.09.2026: к пивотам добавились точки ликвидности из
# снимка (экстремумы дня и недели, Азия, равные экстремумы, нетронутые пулы,
# границы зон). Стоп и цель выбираются ТОЛЬКО из этого списка, и когда между
# входом и следующим уровнем 4.7% (ETH, 19.09), модель ставит стоп туда — и
# план умирает на R:R. Реальные точки, за которые цепляется стоп, в списке
# быть обязаны.
#
# 20 стало 20.09.2026: в списке появились края имбалансов, свинги старших
# ТФ и края кластеров ликвидаций. За 11 часов 33 плана ставили стоп на
# пивоты в 1.5-2% от входа — не потому что там структура, а потому что
# из 16 мест 12 занимали пивоты, и ближайший разрешённый пивот побеждал
# по R:R. Стоп обязан стоять за элементом структуры, значит эти элементы
# должны быть в списке с именами.

# Ближе этого к текущей цене уровни не показываем: вход вплотную к цене даёт
# стоп, который не проходит предел издержек, и модель будет исправно предлагать
# то, что предохранитель всё равно отвергнет.
MIN_DISTANCE_PCT = 0.15


# Доля ATR, ниже которой стоп — это шум свечи, а не защита идеи. Фиксированные
# 1.5% тесны для монеты с ATR 3% (одна свеча) и щедры для BTC с ATR 0.5%.
# Минимум берётся как большее из порога по издержкам и этой доли ATR.
STOP_ATR_SHARE = 0.5


def min_stop_pct(atr_pct=None):
    """
    Минимальная дистанция стопа: по издержкам и, если известен, по размаху.

    По издержкам: доля = ставка_туда-обратно / стоп, значит стоп = ставка /
    доля. При 0.075% туда-обратно и пределе 5% риска это 1.5%. По размаху —
    STOP_ATR_SHARE × ATR%. Берётся большее.

    Ноль в пределе означает «не проверять» — тогда минимум только по ATR.
    """
    limit = getattr(config, 'MAX_ENTRY_COST_SHARE_PCT', 0) or 0
    floor = 0.0 if limit <= 0 else config.ENTRY_COST_ROUND_TRIP / (limit / 100) * 100
    if atr_pct:
        floor = max(floor, STOP_ATR_SHARE * float(atr_pct))
    return round(floor, 3)


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


def extra_levels(market):
    """
    Точки ликвидности из снимка как кандидаты в уровни: цена, имя, вес.

    Вес — сколько «касаний» они стоят при отборе: экстремум недели и равные
    экстремумы — как скопление из трёх, дневные и зоны — из двух, Азия — как
    одиночный пивот. Это не замер, а порядок предпочтения при нехватке мест.
    """
    if not market:
        return []
    out = []
    s = market.get('sessions') or {}
    for key, name, weight in (('pwh', 'максимум недели', 3), ('pwl', 'минимум недели', 3),
                              ('pdh', 'максимум вчера', 2), ('pdl', 'минимум вчера', 2),
                              ('asia_high', 'максимум Азии', 1), ('asia_low', 'минимум Азии', 1)):
        if s.get(key):
            out.append({'price': float(s[key]), 'kind': name, 'touches': weight, 'last': 0})
    smc = market.get('smc') or {}
    for row in (smc.get('equal_levels') or []):
        if row.get('price'):
            out.append({'price': float(row['price']),
                        'kind': f"равные экстремумы {row.get('source', '')}".strip(),
                        'touches': 3, 'last': 0})
    for row in (smc.get('untapped') or []):
        if row.get('price'):
            out.append({'price': float(row['price']), 'kind': 'нетронутое скопление',
                        'touches': 2, 'last': 0})
    for z in (market.get('pois') or []):
        if z.get('top') and z.get('bottom'):
            kind = {'ORDER_BLOCK': 'ордер-блок', 'BREAKER': 'брейкер',
                    'MITIGATION': 'mitigation'}.get(z.get('type'), 'зона')
            out.append({'price': float(z['top']), 'kind': f'верх зоны {kind}', 'touches': 2, 'last': 0})
            out.append({'price': float(z['bottom']), 'kind': f'низ зоны {kind}', 'touches': 2, 'last': 0})
    # Края имбалансов: вход — у ближнего края, стоп — за дальним.
    for g in (market.get('fvgs') or []):
        if g.get('top') and g.get('bottom'):
            out.append({'price': float(g['top']), 'kind': 'верх имбаланса', 'touches': 2, 'last': 0})
            out.append({'price': float(g['bottom']), 'kind': 'низ имбаланса', 'touches': 2, 'last': 0})
    # Свинги старших ТФ: за ними стоят стопы позиций, живущих днями.
    for tf, name in (('4h', '4ч'), ('1d', 'дня')):
        row = (market.get('htf') or {}).get(tf) or {}
        if row.get('swing_high'):
            out.append({'price': float(row['swing_high']), 'kind': f'свинг-максимум {name}', 'touches': 3, 'last': 0})
        if row.get('swing_low'):
            out.append({'price': float(row['swing_low']), 'kind': f'свинг-минимум {name}', 'touches': 3, 'last': 0})
    # Кластеры ликвидаций (оценка): ближний край — магнит и цель, дальний —
    # место, за которое прячут стоп. Два самых тяжёлых на сторону.
    liq = market.get('liquidations') or {}
    for side_key, who in (('below', 'лонгов'), ('above', 'шортов')):
        rows = sorted(liq.get(side_key) or [], key=lambda c: -c.get('share_pct', 0))[:2]
        for c in rows:
            lo, hi = float(c['from']), float(c['to'])
            near, far = (hi, lo) if side_key == 'below' else (lo, hi)
            out.append({'price': near, 'kind': f'ближний край ликвидаций {who}', 'touches': 2, 'last': 0})
            if abs(far - near) / near * 100 >= 0.2:
                out.append({'price': far, 'kind': f'дальний край ликвидаций {who}', 'touches': 2, 'last': 0})
    return out


def _dedupe(found, span):
    """
    Один уровень — одна строка: пивот-максимум 2615.00 и пивот-минимум
    2615.11 занимали два места из шестнадцати. Сильнейший остаётся, имя
    второго дописывается через « / », чтобы модель знала, что здесь сошлось.
    """
    kept = []
    for lv in sorted(found, key=lambda lv: -lv['touches']):
        twin = next((k for k in kept if abs(k['price'] - lv['price']) <= span), None)
        if twin is None:
            kept.append(dict(lv))
            continue
        if lv['kind'] not in twin['kind'] and twin['kind'].count(' / ') < 1:
            twin['kind'] = f"{twin['kind']} / {lv['kind']}"
        twin['touches'] = max(twin['touches'], lv['touches'])
    return kept


def levels(df, at=None, extra=None):
    """
    Пронумерованный список уровней: сверху вниз, L1 — самый высокий.

    Нумерация ПО ЦЕНЕ, а не по силе. Так модель видит карту рынка в привычном
    порядке и не путает «первый по списку» с «главный».

    extra — дополнительные кандидаты (extra_levels): вливаются в общий отбор,
    дубли в пределах допуска скопления отбрасываются в пользу уже найденного.
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
    found = _dedupe(pools + _lone_pivots(pivot_list, at, pools, span)
                    + [dict(c) for c in (extra or [])], span)

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
    #
    # 3. Ближайшие — всегда. Отбор по силе на живом ETH 19.09.2026 отдал все
    #    восемь мест снизу скоплениям на −4…−7%, а равные экстремумы в 0.3%,
    #    открытие дня и минимум Азии в 1-1.3% от цены выпали: стоп ставить
    #    было не на что, кроме уровня в 4.7%, и план умер на R:R. Три
    #    ближайших уровня с каждой стороны входят вне конкурса, остальные
    #    места — по силе.
    half = max(1, MAX_LEVELS // 2)
    nearest_keep = 3

    def pick(candidates, distance):
        by_distance = sorted(candidates, key=distance)
        kept = by_distance[:nearest_keep]
        rest = sorted((lv for lv in candidates if lv not in kept),
                      key=lambda lv: (-lv['touches'], distance(lv)))
        return kept + rest[:max(0, half - len(kept))]

    above = pick([lv for lv in near if lv['price'] > price_now],
                 lambda lv: lv['price'] - price_now)
    below = pick([lv for lv in near if lv['price'] <= price_now],
                 lambda lv: price_now - lv['price'])
    near = sorted(above + below, key=lambda lv: -lv['price'])

    # ОБЪЁМ У УРОВНЯ. Уровень, у которого торговали втрое больше обычного, и
    # уровень, который цена проскочила на пустом объёме, в списке выглядели
    # одинаково. Отношение среднего объёма свечей, касавшихся уровня за 200
    # свечей, к медианному объёму свечи: ×3 — узел, ×0.5 — пустота.
    volume = df['volume'].values
    start = max(0, at - 199)
    window_h, window_l, window_v = high[start:at + 1], low[start:at + 1], volume[start:at + 1]
    median_v = float(np.median(window_v)) if len(window_v) else 0.0
    for number, level in enumerate(near, start=1):
        level['id'] = f'L{number}'
        level['dist_pct'] = round((level['price'] - price_now) / price_now * 100, 2)
        if median_v > 0:
            tol = level['price'] * 0.0025
            touched = window_v[(window_l <= level['price'] + tol) & (window_h >= level['price'] - tol)]
            level['volume_x'] = round(float(np.mean(touched)) / median_v, 1) if len(touched) else None
        else:
            level['volume_x'] = None
    return near, atr_now


def _stop_buffer(atr_pct):
    """Отступ стопа за уровень — тот же, что применяет llm_decide."""
    import llm_decide
    return llm_decide.stop_hunt_pct(atr_pct)


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


def _gap_bars(df, at, window=200):
    """Сколько свечей не хватает в последних `window` (по шагу времени)."""
    try:
        ts = df['timestamp'].iloc[max(0, at - window + 1):at + 1]
        if len(ts) < 3:
            return 0
        deltas = ts.diff().dropna()
        step = deltas.median()
        if step <= pd.Timedelta(0):
            return 0
        missing = ((deltas / step).round() - 1).clip(lower=0)
        return int(missing.sum())
    except Exception:                                  # noqa: BLE001
        return 0


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
    found, atr_now = levels(df, at, extra=extra_levels(market))
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
                  'min_stop_pct': min_stop_pct(atr_pct)})

    # Ход цены — то, чего модель просила в первом же живом разборе: «данные
    # не показывают динамику цены, только уровень». Без него рост открытого
    # интереса нечем сопоставить, а именно из этого сопоставления берутся
    # противоречия, ради которых её и спрашивают.
    def change(bars):
        if at < bars or close[at - bars] <= 0:
            return None
        return (price_now / float(close[at - bars]) - 1) * 100
    # ДЫРЫ В СВЕЧАХ. Пропуск часовых свечей внутри окна разметки означает,
    # что пивоты, профиль и структура посчитаны по обрезку; такой разбор в
    # статистике должен быть помечен, а не растворён среди честных.
    facts['data_gap_bars'] = _gap_bars(df, at)

    facts['change_4h'] = change(4)
    facts['change_24h'] = change(24)
    facts['change_7d'] = change(24 * 7)
    lo30 = float(np.min(df['low'].values[max(0, at - 24 * 30):at + 1]))
    hi30 = float(np.max(df['high'].values[max(0, at - 24 * 30):at + 1]))
    facts['range_30d_pos'] = ((price_now - lo30) / (hi30 - lo30) * 100
                              if hi30 > lo30 else None)

    lines = [
        f'Пара: {pair}   Цена: {price_now:.6g}   '
        f'Размах (ATR): {_fmt(atr_pct, 2, "%")}   {_when(df, at)}',
        f'Ход цены: за 4ч {_signed(facts["change_4h"])}   '
        f'за 24ч {_signed(facts["change_24h"])}   за 7д {_signed(facts["change_7d"])}   '
        f'в диапазоне 30д: {_fmt(facts["range_30d_pos"], 0, "%")} от низа',
        _activity_line((market or {}).get('activity')),
        f'Минимальный стоп: {facts["min_stop_pct"]:.2f}% '
        f'(издержки {min_stop_pct():.2f}%, половина ATR {STOP_ATR_SHARE * (atr_pct or 0):.2f}%)'
        f'   Отступ стопа за уровень: {_stop_buffer(atr_pct):.2f}% (ставит код)',
        '',
        'УРОВНИ (сверху вниз, расстояние от цены; объём у уровня — к медианной свече, '
        '×3 узел, ×0.5 пустота)',
    ]
    for level in found:
        touches = (f", касаний {level['touches']}" if level['touches'] > 1 else '')
        vol = (f"   объём у уровня ×{level['volume_x']}" if level.get('volume_x') else '')
        lines.append(f"  {level['id']:<4}{level['price']:>14.6g}   "
                     f"{level['dist_pct']:+6.2f}%   {level['kind']}{touches}{vol}")

    lines += [
        '',
        'РАССТАНОВКА УЧАСТНИКОВ',
        f"  Открытый интерес: {_fmt(facts['oi'], 0)}   "
        f"за 4ч {_fmt(facts['oi_4h'], 2, '%')}   за 24ч {_fmt(facts['oi_24h'], 2, '%')}"
        f"   за 7д {_fmt((market or {}).get('oi_week'), 2, '%')}",
        f"  Фандинг: {_fmt(facts['funding'], 6)}"
        f"   (плюс — платят лонги)"
        + _funding_trend((market or {}).get('funding_trend')),
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


def _activity_line(act):
    if not act:
        return 'Активность: —'
    day = f"   сутки к средним за неделю ×{act['day_x']:.2f}" if act.get('day_x') else ''
    return (f"Активность: последняя свеча ×{act['last_x']:.1f} к медиане, "
            f"последние 4 св. ×{act['last4_x']:.1f}{day}")


def _funding_trend(values):
    if not values or len(values) < 2:
        return ''
    return '   последние выплаты: ' + ' → '.join(f'{v:.6f}' for v in values)


def _session_lines(s, price_now):
    if not s:
        return [_NONE]
    out = []

    def item(label, key):
        if s.get(key) is not None:
            return f"{label} {_p(s[key])} ({_pct(s[key], price_now)})"
        return None
    row1 = [x for x in (item('вчера макс', 'pdh'), item('вчера мин', 'pdl'),
                        item('открытие дня', 'day_open')) if x]
    row2 = [x for x in (item('неделя макс', 'pwh'), item('неделя мин', 'pwl')) if x]
    row3 = [x for x in (item('Азия макс', 'asia_high'), item('Азия мин', 'asia_low')) if x]
    for row in (row1, row2, row3):
        if row:
            out.append('  ' + '   '.join(row))
    return out or [_NONE]


def _fvg_lines(gaps):
    if not gaps:
        return [_NONE]
    return [f"  {_p(g['bottom'])}..{_p(g['top'])} {_DIRECTION.get(g.get('direction'), '')}"
            f"   {'цена внутри' if g.get('inside') else ''}   {g.get('bars_ago')} св. назад"
            for g in gaps]


def _delta_hours_line(hours):
    if not hours:
        return None
    cells = []
    for h in hours:
        if h.get('share_pct') is None:
            cells.append('—')
        else:
            cells.append(f"{h['share_pct']:+.0f}%" + ('' if h.get('rows', 60) >= 30 else '?'))
    return '  По часам (старые → новые, ? — покрытие меньше половины): ' + ' → '.join(cells)


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
    hourly = _delta_hours_line(market.get('delta_hours'))
    if hourly:
        out.append(hourly)
    out += ['', f'СТАКАН (факт, но снимок момента: заявку снимают за секунды; '
                f'{reach})']
    out += _book_lines(market.get('book'))
    out += ['', 'ЭКСТРЕМУМЫ ДНЯ, НЕДЕЛИ И АЗИИ (пулы стопов, UTC)']
    out += _session_lines(market.get('sessions'), price_now)
    out += ['', 'СТРУКТУРА (SMC по рабочему ТФ)']
    out += _smc_lines(market.get('smc'), price_now)
    out += ['', 'НЕЗАКРЫТЫЕ ИМБАЛАНСЫ (1ч, живые)']
    out += _fvg_lines(market.get('fvgs'))
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
