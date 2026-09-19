"""
Всё, что можно узнать о паре, — одним снимком для языковой модели.

ЗАЧЕМ ЭТОТ МОДУЛЬ ПОЯВИЛСЯ. Модели задают пять факторов конфлюенса, а данные
давали ровно для трёх. Профиль объёма (vp) и характер потока (flow) она
отмечала ВСЛЕПУЮ: в разметке не было ни того, ни другого. Отсюда и конфлюенс
1/5 и 3/5 в первых живых разборах — не потому что рынок пустой, а потому что
спрашивали о том, чего не показали.

ПОЧТИ ВСЁ ЭТО УЖЕ БЫЛО ПОСЧИТАНО. Имбалансы, блоки ордеров, структура, свипы,
равные экстремумы, зоны фибо, killzone — всё это считает пакет `smc` для
четвёртой стратегии, с тестами и с 2024 года. Не хватало только одного: отдать
это модели. Новых запросов к бирже почти не нужно — контекст SMC за этот цикл
уже построен и лежит в кэше.

ЧТО ДЕЙСТВИТЕЛЬНО НОВОЕ ЗДЕСЬ: профиль объёма, дельта агрессора (собирается с
18 сентября 2026 и до сих пор не имела ни одного потребителя), поглощение по
фитилям и стакан.

ЧЕГО НЕ БУДЕТ, СКОЛЬКО НИ ПРОСИ. Стоп-лоссы других участников биржа не
публикует — ни одна. Карты ликвидаций по REST у Bybit тоже нет: `fetchLiquidations`
отвечает None, есть только `fetchMyLiquidations` про свои же. Значит про чужие
стопы можно говорить лишь прокси-уровнями — скоплениями экстремумов, за
которыми эти стопы стоят. Такой уровень помечается прямо в разметке, и модель
видит, где факт, а где догадка.

ВСЁ СОБИРАЕТСЯ В ЦИКЛЕ, А НЕ В ПОТОКЕ РАЗБОРА. Клиент биржи на параллельные
обращения не рассчитан, а разбор идёт минутами в своём потоке. Поэтому снимок
делается заранее и передаётся готовым — та же дисциплина, что со свечами.

ОТСУТСТВИЕ ЛЮБОГО КУСКА — ЗАКОННОЕ СОСТОЯНИЕ. Каждый блок возвращает None,
если посчитать не вышло, и в разметке на его месте стоит прочерк. «Не измерено»
и «нет» — разные утверждения, и путать их нельзя: на этом уже погорел
открытый интерес трёхнедельной давности, попавший в разбор как сегодняшний.
"""

import time

import numpy as np

import config
import positioning
from logger import log

# Сколько свечей рабочего ТФ берём под профиль объёма. Двести часов — это
# восемь суток: достаточно, чтобы зона нахождения цены сложилась, и не столько,
# чтобы в неё попал позапрошлый рынок.
PROFILE_BARS = int(config.__dict__.get('LLM_PROFILE_BARS', 0) or 200)

# На сколько корзин делим диапазон цены. Больше — точнее пик, но мельче
# статистика в каждой корзине; сорок это компромисс, проверенный глазом на
# BTCUSDT и SHIB1000USDT (разный порядок цены, одинаково читаемый профиль).
PROFILE_BINS = int(config.__dict__.get('LLM_PROFILE_BINS', 0) or 40)

# Доля объёма, которую считаем «зоной стоимости» (value area). 70% — канон
# рыночного профиля, менять без замера незачем.
VALUE_AREA = 0.70

# Поглощение: фитиль не меньше этой доли всей свечи И объём не ниже этого
# множителя к медиане. Оба порога вместе: длинный фитиль на пустом объёме
# означает тонкий рынок, а не чью-то лимитную стену.
WICK_SHARE = 0.60
WICK_VOLUME_X = 2.0

# Глубина стакана, которую разбираем: заявки в пределах этого процента от цены.
# Дальше стоят заявки, которые снимут раньше, чем цена до них дойдёт.
BOOK_RANGE_PCT = 2.0

# Сколько уровней просим у биржи — предел Bybit для бессрочных контрактов.
# ±2% ЭТО НЕ ПОКРЫВАЕТ, и покрыть не может: у BTC при тике 0.1 пятьсот
# уровней — это ±25 долларов, три сотых процента. Первый живой снимок с
# двумястами уровнями подписал такой стакан «±2%», и это была ложь в шапке.
# Поэтому охват не обещается, а ИЗМЕРЯЕТСЯ по самой дальней заявке и
# печатается как есть.
BOOK_LIMIT = 500

# Старше этого дельта считается НЕ ИЗМЕРЕННОЙ, сколько бы строк ни лежало в
# файле. Первый живой снимок на машине разработки показал «1ч −18.5%» по
# ряду, оборвавшемуся три недели назад: окна считаются от последней записи,
# и без этого порога простой сборщика выглядел бы как сегодняшний рынок —
# ровно так открытый интерес трёхнедельной давности уже попадал в разбор.
DELTA_MAX_AGE_MIN = int(config.__dict__.get('LLM_DELTA_MAX_AGE_MIN', 0) or 180)

# Плитой считаем уровень с объёмом не ниже этого множителя к медиане по своей
# стороне. Это и есть единственная ВИДИМАЯ ликвидность: в отличие от стопов,
# лимитные заявки биржа показывает.
WALL_X = 5.0


def _number(value):
    """
    Обычное число из чего угодно. None остаётся None.

    numpy-типы здесь не косметика: np.float64 печатается как «np.float64(2.6)»
    и не сериализуется в JSON. В разметке такое увидела бы модель, а в журнале
    — человек.
    """
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe(name, call, *args, **kwargs):
    """
    Вызывает кусок сборки и отдаёт None, если он упал.

    Снимок собирается из шести независимых источников. Падение одного не имеет
    права лишить модель пяти остальных — но и молчать о нём нельзя, иначе
    пропажа данных выглядит как их отсутствие на рынке.
    """
    try:
        return call(*args, **kwargs)
    except Exception as exc:                       # noqa: BLE001
        log(f'   разметка: {name} не собран — {exc}')
        return None


# ── Профиль объёма ───────────────────────────────────────────────────────────

def volume_profile(df, at=None, bars=None, bins=None):
    """
    Где рынок провёл объём: пик (POC), зона стоимости, разрежения.

    ЗАЧЕМ. Это второй фактор конфлюенса, и до сих пор модель отмечала его
    наугад. Уровень, у которого простояли неделю, и уровень, который цена
    проскочила за час, ведут себя по-разному, а по списку экстремумов они
    неразличимы.

    Объём свечи раскладывается по её диапазону РАВНОМЕРНО, а не вешается на
    цену закрытия. Свеча в полтора процента размахом — это не сделка по одной
    цене, и приписывать ей одну корзину значит рисовать частокол там, где был
    сплошной поток.
    """
    bars = bars or PROFILE_BARS
    bins = bins or PROFILE_BINS
    high = np.asarray(df['high'].values, dtype=float)
    low = np.asarray(df['low'].values, dtype=float)
    volume = np.asarray(df['volume'].values, dtype=float)

    end = len(high) - 1 if at is None else int(at)
    start = max(0, end - bars + 1)
    high, low, volume = high[start:end + 1], low[start:end + 1], volume[start:end + 1]
    if len(high) < 10:
        return None

    top, bottom = float(np.max(high)), float(np.min(low))
    if not np.isfinite(top) or not np.isfinite(bottom) or top <= bottom:
        return None

    edges = np.linspace(bottom, top, bins + 1)
    weight = np.zeros(bins)
    for h, l, v in zip(high, low, volume):
        if v <= 0:
            continue
        first = int(np.searchsorted(edges, l, side='right') - 1)
        last = int(np.searchsorted(edges, h, side='right') - 1)
        first = min(max(first, 0), bins - 1)
        last = min(max(last, 0), bins - 1)
        weight[first:last + 1] += v / (last - first + 1)

    if weight.sum() <= 0:
        return None

    centers = (edges[:-1] + edges[1:]) / 2
    poc_index = int(np.argmax(weight))

    # Зона стоимости растёт от пика в обе стороны, каждый раз в ту, где объём
    # больше. Так её и строят в рыночном профиле: это не просто перцентиль, а
    # непрерывная область вокруг пика.
    taken = {poc_index}
    collected = weight[poc_index]
    target = weight.sum() * VALUE_AREA
    low_i = high_i = poc_index
    while collected < target and (low_i > 0 or high_i < bins - 1):
        down = weight[low_i - 1] if low_i > 0 else -1
        up = weight[high_i + 1] if high_i < bins - 1 else -1
        if up >= down:
            high_i += 1
            taken.add(high_i)
            collected += weight[high_i]
        else:
            low_i -= 1
            taken.add(low_i)
            collected += weight[low_i]

    median = float(np.median(weight[weight > 0])) if np.any(weight > 0) else 0.0
    # Соседние разреженные корзины сливаются в один диапазон: четыре цены
    # подряд через шаг корзины — это одна дыра, а не четыре уровня.
    thin, run = [], None
    for i in range(bins):
        if 0 < weight[i] < median * 0.25:
            if run is None:
                run = [float(edges[i]), float(edges[i + 1])]
            else:
                run[1] = float(edges[i + 1])
        elif run is not None:
            thin.append(run)
            run = None
    if run is not None:
        thin.append(run)

    return {
        'poc': float(centers[poc_index]),
        'value_low': float(centers[min(taken)]),
        'value_high': float(centers[max(taken)]),
        'thin': thin[:3],
        'bars': int(len(high)),
    }


# ── Поглощение ───────────────────────────────────────────────────────────────

def absorption(df, at=None, lookback=48):
    """
    Свечи с длинным фитилём на аномальном объёме — след лимитного поглощения.

    ЭТО ПРОКСИ, И ОН ОБЪЯВЛЕН ТАКОВЫМ. Настоящее поглощение видно только в
    ленте: агрессор бьёт в стену, а цена не идёт. По свечам об этом судят
    косвенно — длинный фитиль на большом объёме означает, что цену туда
    отвезли и вернули. Ошибиться можно, и модель должна знать, что это
    догадка, а не замер.
    """
    high = np.asarray(df['high'].values, dtype=float)
    low = np.asarray(df['low'].values, dtype=float)
    open_ = np.asarray(df['open'].values, dtype=float)
    close = np.asarray(df['close'].values, dtype=float)
    volume = np.asarray(df['volume'].values, dtype=float)

    end = len(high) - 1 if at is None else int(at)
    start = max(0, end - lookback + 1)
    if end - start < 10:
        return None

    window = slice(start, end + 1)
    median = float(np.median(volume[window])) if len(volume[window]) else 0.0
    if median <= 0:
        return None

    out = []
    for i in range(start, end + 1):
        span = high[i] - low[i]
        if span <= 0 or volume[i] < median * WICK_VOLUME_X:
            continue
        body_top = max(open_[i], close[i])
        body_bottom = min(open_[i], close[i])
        upper = (high[i] - body_top) / span
        lower = (body_bottom - low[i]) / span
        if upper >= WICK_SHARE:
            out.append({'price': float(high[i]), 'side': 'сверху',
                        'volume_x': round(_number(volume[i] / median), 1),
                        'bars_ago': int(end - i)})
        elif lower >= WICK_SHARE:
            out.append({'price': float(low[i]), 'side': 'снизу',
                        'volume_x': round(_number(volume[i] / median), 1),
                        'bars_ago': int(end - i)})
    # Свежие важнее: неделю назад поглощённая заявка давно снята.
    out.sort(key=lambda row: row['bars_ago'])
    return out[:4] or None


# ── Дельта агрессора ─────────────────────────────────────────────────────────

def delta_facts(pair, upto=None, price_change_pct=None):
    """
    Кто бил по рынку — покупатель или продавец, и сходится ли это с ценой.

    ПЕРВЫЙ ПОТРЕБИТЕЛЬ ЭТОГО РЯДА. Дельта копится поминутно с 18 сентября 2026
    и до сих пор не читалась ни одним модулем. А это единственный источник,
    которого нет в истории у бирж: его нельзя взять задним числом, только
    собирать живьём — и он единственный не исчерпан в этом проекте (семейство
    пробоя закрыто именно потому, что все свечные признаки исхода не
    разделяют).

    РАСХОЖДЕНИЕ ВАЖНЕЕ ЗНАКА. Цена растёт, а дельта отрицательная — растут не
    покупками, а отсутствием продавца: такой рост не поддержан и рвётся легко.
    Поэтому считается и то, и другое.
    """
    rows = positioning.series('delta', pair, upto=upto)
    if not rows:
        return None

    newest = max(int(row['ts']) for row in rows)
    out = {'fresh_min': None}
    if upto:
        out['fresh_min'] = int((int(upto) - newest) / 60000)
        if out['fresh_min'] > DELTA_MAX_AGE_MIN:
            # Возраст остаётся: «не измерено» и «измерено три недели назад» —
            # разные утверждения, и второе модели полезнее.
            return {'fresh_min': out['fresh_min'], 'stale': True}

    for hours, name in ((1, 'h1'), (4, 'h4'), (24, 'h24')):
        edge = newest - hours * 3600 * 1000
        window = [row for row in rows if int(row['ts']) >= edge]
        if not window:
            out[name] = None
            continue
        buy = sum(float(row.get('buy') or 0) for row in window)
        sell = sum(float(row.get('sell') or 0) for row in window)
        total = buy + sell
        out[name] = {
            'delta': buy - sell,
            # Доля перевеса от всего оборота. Абсолютная дельта несравнима
            # между парами: у SHIB объём в миллионах монет, у BTC в единицах.
            'share_pct': (buy - sell) / total * 100 if total else None,
            # Строк, а не минут: сборщик пишет раз в свой интервал.
            'rows': len(window),
        }

    if price_change_pct is not None and out.get('h4'):
        share = out['h4'].get('share_pct')
        if share is not None:
            out['divergence'] = (
                'цена растёт на продажах' if price_change_pct > 0 and share < -5 else
                'цена падает на покупках' if price_change_pct < 0 and share > 5 else
                None)
    return out


# ── Стакан ───────────────────────────────────────────────────────────────────

def book_facts(client, pair, limit=None, range_pct=None):
    """
    Видимая ликвидность: плиты и перекос сторон.

    ЭТО ЕДИНСТВЕННЫЕ НАСТОЯЩИЕ ЗАЯВКИ, которые отдаёт биржа. Всё остальное про
    «ликвидность» в этом файле — прокси по свечам. Поэтому стакан помечается
    как факт, а скопления экстремумов — как догадка.

    И у него есть своя слабость, о которой модель обязана знать: стакан — это
    СНИМОК. Заявку снимают за миллисекунды, и плита в двух процентах от цены
    может не дожить до подхода цены. Историей он не становится: ни одна биржа
    не отдаёт стакан задним числом.
    """
    if client is None:
        return None
    import exchange

    symbol = exchange.market_symbol(pair, client)
    book = client.fetch_order_book(symbol, limit=limit or BOOK_LIMIT)
    bids = [(float(p), float(v)) for p, v in (book.get('bids') or [])]
    asks = [(float(p), float(v)) for p, v in (book.get('asks') or [])]
    if not bids or not asks:
        return None

    mid = (bids[0][0] + asks[0][0]) / 2
    reach = (range_pct or BOOK_RANGE_PCT) / 100

    near_bids = [(p, v) for p, v in bids if p >= mid * (1 - reach)]
    near_asks = [(p, v) for p, v in asks if p <= mid * (1 + reach)]
    bid_sum = sum(v for _, v in near_bids)
    ask_sum = sum(v for _, v in near_asks)
    if bid_sum <= 0 or ask_sum <= 0:
        return None

    # Сколько на самом деле видно. Биржа отдаёт ограниченное число уровней, и
    # на дорогой монете они кончаются задолго до запрошенного процента.
    covered = min(reach,
                  (mid - near_bids[-1][0]) / mid,
                  (near_asks[-1][0] - mid) / mid) * 100

    def walls(side, sign):
        volumes = [v for _, v in side]
        median = float(np.median(volumes)) if volumes else 0.0
        if median <= 0:
            return []
        found = [{'price': _number(p),
                  'volume_x': round(_number(v / median), 1),
                  'dist_pct': _number((p / mid - 1) * 100)}
                 for p, v in side if v >= median * WALL_X]
        found.sort(key=lambda row: abs(row['dist_pct']))
        return found[:3]

    return {
        'mid': mid,
        'bid_volume': bid_sum,
        'ask_volume': ask_sum,
        # Больше единицы — покупателей в стакане больше. Само по себе это не
        # сигнал: крупные заявки ставят и для того, чтобы их увидели.
        'imbalance': bid_sum / ask_sum,
        'spread_pct': (asks[0][0] - bids[0][0]) / mid * 100,
        'walls_below': walls(near_bids, -1),
        'walls_above': walls(near_asks, 1),
        # Реальный охват, а не запрошенный: см. BOOK_LIMIT.
        'range_pct': covered,
        'levels': len(near_bids) + len(near_asks),
    }


# ── Структура рынка из пакета smc ────────────────────────────────────────────

def smc_facts(pair, price, client=None, context=None):
    """
    Структура, свипы, имбалансы и зона фибо — из пакета, который их уже считает.

    ЭТО ЗАИМСТВОВАНИЕ КАРТЫ, А НЕ МАРШРУТА, и разница принципиальна. В bot.py
    записано правило: стратегия не должна молча получать чужих кандидатов и
    торговать их под своим именем. Здесь берутся не сетапы SMC и не её оценки,
    а замеры рынка: где сломалась структура, где остался незакрытый имбаланс,
    был ли вынос за уровень. Эти числа не принадлежат стратегии — они
    описывают рынок, и считать их второй раз своим кодом значило бы завести
    второе определение имбаланса, которое однажды разойдётся с первым.

    Контекст берётся из общего кэша: SMC строит его в том же цикле по тем же
    парам, и обычно он уже готов — новых запросов к бирже не будет.
    """
    if context is None:
        import strategy_smc
        context = strategy_smc.get_context(pair, client=client)
    if context is None:
        return None
    return _smc_from_context(context, price)


def _smc_from_context(context, price):
    """Тело smc_facts, отделённое, чтобы контекст брался один раз на снимок."""

    from smc import fib as smc_fib
    from smc import imbalance
    from smc import liquidity as smc_liquidity
    from smc import structure as structure_mod

    df = context.frames.get('poi')
    if df is None or not len(df):
        return None
    index = len(df) - 1
    out = {}

    # Направление старшего порядка: оно считается по дневному и четырёхчасовому
    # фреймам, а не по рабочему.
    out['bias'] = _safe('bias', context.bias_at, df['timestamp'].iloc[index])

    # Состояние структуры рабочего ТФ: тренд и последнее событие слома.
    state = _safe('структура', structure_mod.state_at, context.structure, index)
    if state:
        event = state.get('last_event') or {}
        out['trend'] = state.get('trend')
        out['last_break'] = {
            # 'type' у события — BOS или CHoCH: продолжение структуры или её
            # смена. Разница в одно слово, а смысл противоположный.
            'type': event.get('type'),
            'direction': event.get('direction'),
            'price': _number(event.get('level')),
            'bars_ago': index - int(event.get('index', index)),
        } if event else None

    # Свежий вынос за уровень с возвратом: снятая ликвидность.
    sweep = _safe('свип', smc_liquidity.recent_sweep, context.sweeps, index)
    if sweep:
        pool = sweep.get('pool') or {}
        out['sweep'] = {
            'side': sweep.get('side'),
            'price': pool.get('price'),
            'penetration_pct': sweep.get('penetration_pct'),
            'reclaimed': sweep.get('reclaimed_at') is not None,
            'bars_ago': index - int(sweep.get('index', index)),
        }

    # Незакрытые имбалансы рядом с ценой — куда рынок возвращается чаще всего.
    active = _safe('имбалансы', imbalance.active_fvgs, df, context.fvgs, index)
    near = _safe('ближайший имбаланс', imbalance.nearest_fvg, active or [], price)
    if near:
        out['fvg'] = {'top': near.get('top'), 'bottom': near.get('bottom'),
                      'direction': near.get('direction')}

    # Дисконт или премия: где цена внутри последней импульсной ноги. Сетка
    # фибо тянется от начала ноги к её концу — этим же занята четвёртая
    # стратегия, и второго определения заводить нельзя.
    leg = _safe('нога', structure_mod.last_leg, context.structure, index)
    if leg:
        out['side_of_range'] = _safe('сторона', smc_fib.market_side, price, leg)
        out['equilibrium'] = _safe('равновесие', smc_fib.equilibrium, leg)
        # От ноги берём только направление и две цены: остальное — служебные
        # индексы и метки времени, модели они не нужны, а токены стоят минут.
        out['leg'] = {
            'direction': leg.get('direction'),
            'from': _number((leg.get('start') or {}).get('price')),
            'to': _number((leg.get('end') or {}).get('price')),
        }

    # Равные экстремумы — самый честный прокси чужих стопов: за ними их и
    # ставят. Сами стопы биржа не отдаёт и не отдаст.
    pools = [p for p in (context.pools or [])
             if p.get('source') in ('EQH', 'EQL') and p.get('price')
             # Живой снимок стоит на последнем баре, и здесь фильтр ничего не
             # режет. Но та же функция годится и для разбора прошлого, а там
             # кластер, подтверждённый позже бара решения, — это будущее.
             and int(p.get('confirmed_at', index)) <= index]
    pools.sort(key=lambda p: abs(float(p['price']) - price))
    out['equal_levels'] = [
        {'price': float(p['price']), 'source': p['source'],
         # BSL — ликвидность покупателей НАД ценой (стопы шортов), SSL — под.
         'side': p.get('side')}
        for p in pools[:3]]

    # Нетронутые пулы: ликвидность, которую ещё не снимали. К ней рынок и
    # ходит чаще всего.
    untapped = _safe('нетронутые пулы', smc_liquidity.untapped_pools,
                     context.pools, context.sweeps, index)
    if untapped:
        untapped = sorted(untapped,
                          key=lambda p: abs(float(p.get('price') or 0) - price))
        out['untapped'] = [{'price': float(p['price']), 'side': p.get('side'),
                            'source': p.get('source')}
                           for p in untapped[:3] if p.get('price')]

    return out or None




# ── Зоны интереса: ордер-блоки, брейкеры, mitigation ────────────────────────

def poi_facts(context, price, index, limit=3):
    """
    Ближайшие к цене живые зоны интереса из пакета smc.

    Считались каждый цикл для четвёртой стратегии и до сих пор модели не
    отдавались — при том что «ордер-блок» первое, о чём спрашивают, когда
    речь о структуре. Берутся только АКТИВНЫЕ: подтверждённые к этому бару,
    не пробитые насквозь и не перетестированные — те же правила, по которым
    их берёт SMC, второго определения зоны здесь нет.
    """
    from smc import poi as poi_mod

    df = context.frames.get('poi')
    if df is None or not len(df) or not context.pois:
        return None
    active = poi_mod.active_pois(df, context.pois, index)
    if not active:
        return None

    def distance(zone):
        if zone['bottom'] <= price <= zone['top']:
            return 0.0
        return min(abs(price - zone['top']), abs(price - zone['bottom']))

    active.sort(key=distance)
    return [{
        'type': z.get('type'),
        'direction': z.get('direction'),
        'top': _number(z.get('top')),
        'bottom': _number(z.get('bottom')),
        'touches': int(z.get('touches') or 0),
        'bars_ago': int(index - int(z.get('confirmed_at', index))),
        'inside': bool(z['bottom'] <= price <= z['top']),
    } for z in active[:limit]]


# ── Структура старших таймфреймов ────────────────────────────────────────────

def htf_facts(context, timestamp):
    """
    Тренд, последний слом и крайние свинги на 4ч и на дне.

    Модели уходило одно слово «старший порядок: вверх». Но за дневным
    свингом стоят стопы позиционных игроков, а не внутридневных, и слом
    дневной структуры — событие другого веса, чем часовой BOS. Индекс
    старшей свечи выравнивается по ЗАКРЫТИЮ, как в bias_at: дневная свеча
    сегодняшнего дня ещё не закрыта, и читать её значило бы читать будущее.
    """
    import pandas as pd
    from smc import structure as structure_mod
    from smc.signal import align_index

    decision = pd.Timestamp(timestamp)
    decision = (decision.tz_localize('UTC') if decision.tzinfo is None
                else decision.tz_convert('UTC'))
    decision += pd.Timedelta(context._durations.get('poi', 0), unit='ns')

    out = {}
    for key, struct in (('htf', getattr(context, 'htf_structure', None)),
                        ('bias', getattr(context, 'bias_structure', None))):
        df = context.frames.get(key)
        if struct is None or df is None:
            continue
        idx = align_index(df, decision, duration_ns=context._durations.get(key))
        if idx < 0:
            continue
        state = structure_mod.state_at(struct, idx)
        event = state.get('last_event') or {}
        points = structure_mod.visible_points(struct, idx)
        last_high = next((p for p in reversed(points) if p['kind'] == 'high'), None)
        last_low = next((p for p in reversed(points) if p['kind'] == 'low'), None)
        out[key] = {
            'trend': state.get('trend'),
            'break': {'type': event.get('type'),
                      'direction': event.get('direction'),
                      'price': _number(event.get('level')),
                      'bars_ago': int(idx - int(event.get('index', idx)))}
            if event else None,
            'swing_high': _number(last_high['price']) if last_high else None,
            'swing_low': _number(last_low['price']) if last_low else None,
        }
    return out or None


# ── Открытый интерес против цены: кто набирает, кто выходит ─────────────────

def _oi_by_bar(pair, df, index, upto, bars):
    """ОИ на закрытие каждой из последних `bars` свечей: последняя запись
    не позже конца свечи. Ряд собирается раз в час, свечи часовые."""
    rows = positioning.series('open_interest', pair, upto=upto)
    if not rows:
        return None
    stamps = [int(r['ts']) for r in rows]
    values = [float(r['value']) for r in rows]
    start = max(1, index - bars + 1)
    out = []
    step = None
    try:
        step = int((df['timestamp'].iloc[1] - df['timestamp'].iloc[0]).total_seconds() * 1000)
    except Exception:                                  # noqa: BLE001
        step = 3_600_000
    for i in range(start - 1, index + 1):
        try:
            end_ms = int(df['timestamp'].iloc[i].timestamp() * 1000) + step
        except Exception:                              # noqa: BLE001
            return None
        pos = int(np.searchsorted(stamps, end_ms, side='right')) - 1
        # Запись старше двух свечей — это не ОИ этой свечи, а последнее, что
        # успел записать сборщик до простоя. Первый прогон на машине
        # разработки показал шесть свечей «ОИ без изменений» по ряду
        # трёхнедельной давности — ровно та подмена, которой быть не должно.
        fresh = pos >= 0 and end_ms - stamps[pos] <= 2 * step
        out.append((i, values[pos] if fresh else None,
                    float(df['close'].iloc[i])))
    return out


def oi_flow(pair, df, index, upto=None, bars=6):
    """
    Последние свечи как «цена × ОИ»: набор лонгов, набор шортов, закрытие.

    Четыре сочетания, и каждое — другой рынок. Рост на растущем ОИ — новые
    лонги; рост на падающем — закрываются шорты, то есть топливо кончается.
    Ряд ОИ копится с первого дня, а читался только как «за 24ч +4%».
    """
    series = _oi_by_bar(pair, df, index, upto, bars)
    if not series or len(series) < 2:
        return None
    labels = []
    for (_, oi_prev, px_prev), (i, oi, px) in zip(series, series[1:]):
        if oi is None or oi_prev is None or oi_prev <= 0 or px_prev <= 0:
            labels.append(None)
            continue
        d_oi = (oi / oi_prev - 1) * 100
        d_px = (px / px_prev - 1) * 100
        if abs(d_oi) < 0.05:
            labels.append('ОИ без изменений')
        elif d_px >= 0 and d_oi > 0:
            labels.append('набор лонгов')
        elif d_px < 0 and d_oi > 0:
            labels.append('набор шортов')
        elif d_px >= 0:
            labels.append('закрытие шортов')
        else:
            labels.append('закрытие лонгов')
    known = [l for l in labels if l]
    if not known:
        return None
    first_oi = next((oi for _, oi, _ in series if oi), None)
    last_oi = series[-1][1]
    return {
        'bars': labels,
        'change_pct': ((last_oi / first_oi - 1) * 100
                       if first_oi and last_oi else None),
    }


# ── Оценка карты ликвидаций ──────────────────────────────────────────────────

# Доли нового открытого интереса по плечам. ЭТО ДОПУЩЕНИЕ, А НЕ ЗАМЕР: биржа
# не публикует, с каким плечом открыты чужие позиции, и любая «карта
# ликвидаций» в интернете строится на таком же допущении. Веса взяты по
# общедоступной статистике розничных плеч на бессрочных контрактах; они
# влияют на веса кластеров, но не на их положение — а положение и есть то,
# что модели важно.
LEVERAGE_MIX = ((10, 0.45), (25, 0.30), (50, 0.15), (100, 0.10))

# Сколько часовых свечей назад смотрим: трое суток. Позиции старше либо уже
# закрыты, либо переставили стопы, и их ликвидационные уровни — шум.
LIQ_BARS = 72

# Кластер — уровни ближе этой доли цены друг к другу.
LIQ_CLUSTER_PCT = 0.4


def liquidation_estimate(pair, df, index, upto=None, bars=None):
    """
    Где скопились ликвидации ОЦЕНОЧНО — по приросту ОИ и типичным плечам.

    Позиции, открытые на свече с ценой P при плече L, ликвидируются у
    P·(1 − 1/L) для лонгов и P·(1 + 1/L) для шортов. Прирост ОИ на свече —
    сколько таких позиций добавилось; куда именно, лонги или шорты, — по
    направлению свечи (см. oi_flow). Уровни, которые цена уже прошла,
    выброшены: те позиции ликвидированы.

    Это прокси, и в разметке он подписан как оценка. Настоящие ликвидации
    копит сборщик по веб-сокету (liquidations.py) — когда его история
    станет длиннее суток, она уйдёт в снимок отдельным блоком.
    """
    series = _oi_by_bar(pair, df, index, upto, bars or LIQ_BARS)
    if not series or len(series) < 3:
        return None
    price = float(df['close'].iloc[index])
    high = np.asarray(df['high'].values, dtype=float)
    low = np.asarray(df['low'].values, dtype=float)

    longs, shorts = [], []
    for (_, oi_prev, px_prev), (i, oi, px) in zip(series, series[1:]):
        if oi is None or oi_prev is None or oi <= oi_prev or px_prev <= 0:
            continue
        opened = oi - oi_prev
        bucket = longs if px >= px_prev else shorts
        for lev, share in LEVERAGE_MIX:
            level = px * (1 - 1 / lev) if bucket is longs else px * (1 + 1 / lev)
            # Уже ликвидированы, если цена после той свечи туда доходила.
            later_low = float(np.min(low[i + 1:index + 1])) if i + 1 <= index else price
            later_high = float(np.max(high[i + 1:index + 1])) if i + 1 <= index else price
            if bucket is longs and later_low <= level:
                continue
            if bucket is shorts and later_high >= level:
                continue
            bucket.append((level, opened * share))

    def clusters(points, side):
        points = sorted(points)
        out, cur = [], None
        for level, weight in points:
            if cur is not None and abs(level - cur['to']) / price * 100 <= LIQ_CLUSTER_PCT:
                cur['to'] = level
                cur['weight'] += weight
            else:
                cur = {'from': level, 'to': level, 'weight': weight}
                out.append(cur)
        total = sum(c['weight'] for c in out) or 1.0
        out.sort(key=lambda c: -c['weight'])
        keep = out[:3]
        return [{'from': _number(c['from']), 'to': _number(c['to']),
                 'share_pct': c['weight'] / total * 100,
                 'dist_pct': ((c['from'] + c['to']) / 2 / price - 1) * 100,
                 'side': side}
                for c in sorted(keep, key=lambda c: abs((c['from'] + c['to']) / 2 - price))]

    below = clusters([p for p in longs if p[0] < price], 'лонги')
    above = clusters([p for p in shorts if p[0] > price], 'шорты')
    if not below and not above:
        return None
    return {'below': below, 'above': above, 'bars': len(series) - 1}


# ── Ликвидации по факту: поток биржи ────────────────────────────────────────

# Кластер — события ближе этой доли цены друг к другу.
LIQ_FACT_CLUSTER_PCT = 0.5


def liquidation_facts(pair, price, upto=None, hours=24):
    """
    Что ликвидировали на самом деле за последние сутки — из потока Bybit.

    Это ФАКТ, в отличие от оценки по ОИ: цена, сторона и объём каждой
    принудительной ликвидации. Но история начинается с запуска сборщика
    (liquidations.py), и первые часы честно называются «сбор идёт N ч»:
    источник есть, ряда ещё нет.
    """
    import liquidations

    now = int(upto) if upto else int(time.time() * 1000)
    started = liquidations.first_ts()
    if started is None:
        return None
    age_h = (now - started) / 3_600_000
    rows = liquidations.rows(pair, since=now - hours * 3_600_000, upto=now)
    if age_h < 1:
        return {'young_min': int(age_h * 60), 'events': len(rows)}

    def totals(window_h):
        edge = now - window_h * 3_600_000
        part = [r for r in rows if int(r['ts']) >= edge]
        return {
            'long_n': sum(1 for r in part if r['side'] == 'long'),
            'long_size': sum(float(r['size']) for r in part if r['side'] == 'long'),
            'short_n': sum(1 for r in part if r['side'] == 'short'),
            'short_size': sum(float(r['size']) for r in part if r['side'] == 'short'),
        }

    clusters = []
    for row in sorted(rows, key=lambda r: float(r['price'])):
        p, size, side = float(row['price']), float(row['size']), row['side']
        if clusters and abs(p - clusters[-1]['to']) / price * 100 <= LIQ_FACT_CLUSTER_PCT:
            c = clusters[-1]
            c['to'] = p
            c['size'] += size
            c[side] += size
        else:
            clusters.append({'from': p, 'to': p, 'size': size,
                             'long': size if side == 'long' else 0.0,
                             'short': size if side == 'short' else 0.0})
    total = sum(c['size'] for c in clusters) or 1.0
    top = sorted(clusters, key=lambda c: -c['size'])[:3]
    top = [{'from': c['from'], 'to': c['to'],
            'share_pct': c['size'] / total * 100,
            'dist_pct': ((c['from'] + c['to']) / 2 / price - 1) * 100,
            'mostly': 'лонги' if c['long'] >= c['short'] else 'шорты'}
           for c in sorted(top, key=lambda c: abs((c['from'] + c['to']) / 2 - price))]

    return {'hours': min(hours, age_h), 'h24': totals(24), 'h4': totals(4),
            'clusters': top, 'events': len(rows)}


# ── BTC как ориентир для альткоинов ─────────────────────────────────────────

def benchmark_facts(df_pair, df_btc, index):
    """
    Ход BTC за 4ч/24ч и сила пары относительно него.

    Правило из исходного задания аналитика: альткоин слабее BTC при падающем
    BTC — лонг с повышенным риском. Без этой строки модель судит альт так,
    будто он живёт сам по себе.
    """
    if df_btc is None or 'close' not in getattr(df_btc, 'columns', ()):
        return None
    n = len(df_btc)
    if n < 25:
        return None

    def change(df, at, bars):
        earlier = float(df['close'].iloc[at - bars])
        return (float(df['close'].iloc[at]) / earlier - 1) * 100 if earlier > 0 else None

    at_btc = n - 1
    if index < 24:
        return None
    out = {'btc_4h': change(df_btc, at_btc, 4), 'btc_24h': change(df_btc, at_btc, 24)}
    pair_24h = change(df_pair, index, 24)
    if pair_24h is not None and out['btc_24h'] is not None:
        out['relative_24h'] = pair_24h - out['btc_24h']
    return out


# ── Снимок целиком ───────────────────────────────────────────────────────────

def snapshot(pair, df, at=None, client=None, benchmark=None):
    """
    Всё сразу. Зовётся В ЦИКЛЕ: внутри есть запросы к бирже.

    Ни один упавший кусок не отменяет остальных — см. _safe. В разметке на
    месте пропажи будет прочерк, и модель скажет об этом вслух: правила в
    промте требуют назвать, чего не хватило.

    Отсечка по времени и ход цены за четыре часа считаются здесь же, по тем
    же свечам: вызывающему незачем знать, что дельте нужна отсечка, а
    расхождению — ход цены. Без отсечки дельта читалась бы до конца файла,
    и разбор прошлого видел бы будущее.

    benchmark — свечи BTC того же ТФ для альткоина, или None для самого BTC.
    """
    if df is None or 'close' not in getattr(df, 'columns', ()):
        return None
    index = len(df) - 1 if at is None else int(at)
    if index < 0 or index >= len(df):
        return None
    price = float(df['close'].iloc[index])

    upto = None
    try:
        upto = int(df['timestamp'].iloc[index].timestamp() * 1000)
    except Exception:                                  # noqa: BLE001
        pass                                           # без отсечки — только для живого бара

    change_4h = None
    if index >= 4:
        earlier = float(df['close'].iloc[index - 4])
        if earlier > 0:
            change_4h = (price / earlier - 1) * 100

    context = None
    try:
        import strategy_smc
        context = strategy_smc.get_context(pair, client=client)
    except Exception as exc:                           # noqa: BLE001
        log(f'   разметка: контекст SMC не собран — {exc}')

    return {
        'profile': _safe('профиль объёма', volume_profile, df, index),
        'absorption': _safe('поглощение', absorption, df, index),
        'delta': _safe('дельта', delta_facts, pair, upto, change_4h),
        'book': _safe('стакан', book_facts, client, pair),
        'smc': (_safe('структура', _smc_from_context, context, price)
                if context is not None else None),
        'pois': (_safe('зоны интереса', poi_facts, context, price,
                       len(context.frames['poi']) - 1)
                 if context is not None else None),
        'htf': (_safe('старшие ТФ', htf_facts, context,
                      context.frames['poi']['timestamp'].iloc[-1])
                if context is not None else None),
        'oi_flow': _safe('ОИ по свечам', oi_flow, pair, df, index, upto),
        'liquidations': _safe('оценка ликвидаций', liquidation_estimate,
                              pair, df, index, upto),
        'liq_fact': _safe('ликвидации по факту', liquidation_facts,
                          pair, price, upto),
        'benchmark': (_safe('BTC', benchmark_facts, df, benchmark, index)
                      if benchmark is not None else None),
    }
