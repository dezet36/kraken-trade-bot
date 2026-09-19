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


# ── Снимок целиком ───────────────────────────────────────────────────────────

def snapshot(pair, df, at=None, client=None):
    """
    Всё сразу. Зовётся В ЦИКЛЕ: внутри есть запросы к бирже.

    Ни один упавший кусок не отменяет остальных — см. _safe. В разметке на
    месте пропажи будет прочерк, и модель скажет об этом вслух: правила в
    промте требуют назвать, чего не хватило.

    Отсечка по времени и ход цены за четыре часа считаются здесь же, по тем
    же свечам: вызывающему незачем знать, что дельте нужна отсечка, а
    расхождению — ход цены. Без отсечки дельта читалась бы до конца файла,
    и разбор прошлого видел бы будущее.
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

    return {
        'profile': _safe('профиль объёма', volume_profile, df, index),
        'absorption': _safe('поглощение', absorption, df, index),
        'delta': _safe('дельта', delta_facts, pair, upto, change_4h),
        'book': _safe('стакан', book_facts, client, pair),
        'smc': _safe('структура', smc_facts, pair, price, client),
    }
