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

ЧЕГО ЗДЕСЬ НЕТ. Зон интереса SMC (ордерные блоки, брейкеры, FVG): им нужен
объект структуры, который строит свой сканер, и тащить его сюда значило бы
связать сборщик с одной стратегией. Добавляются вторым заходом, когда основной
путь заработает.
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


def build(pair, df, at=None, news=None):
    """
    Всё, что нужно модели для одного решения.

    Возвращает {'levels', 'text', 'facts'}. `text` уходит в промпт, `levels`
    нужен грамматике и разбору ответа: по идентификатору восстанавливается цена.

    news — готовый дайджест или None. None означает «фона нет», и в тексте это
    прямо сказано: иначе модель начнёт рассуждать о новостях, которых не видела.
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

    lines = [
        f'Пара: {pair}   Цена: {price_now:.6g}   '
        f'Размах (ATR): {_fmt(atr_pct, 2, "%")}',
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

    if news:
        lines += ['НОВОСТНОЙ ФОН', f"  {news}", '']
    else:
        # Прямо сказать, что фона нет. Молчание модель заполнит сама.
        lines += ['НОВОСТНОЙ ФОН', '  Не получен. Не рассуждай о новостях.', '']

    return {'levels': found, 'text': '\n'.join(lines), 'facts': facts}


def price_of(found, level_id):
    """Цена по идентификатору. None, если такого уровня в списке не было."""
    for level in found or []:
        if level.get('id') == level_id:
            return level['price']
    return None
