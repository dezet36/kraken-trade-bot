"""
Стратегия 1: сигнальный бот на ценовых рынках. MVP из спецификации.

ИДЕЯ. Рынок спрашивает про будущую цену актива, а на бирже эта же
неопределённость уже оценена — волатильностью. Значит вероятность можно
посчитать, а не угадать, и сравнить с ценой корзины. Это ровно тот случай, ради
которого спецификация рекомендует крипту первой: минимум человеческой
интерпретации.

ДВА ВИДА ВОПРОСОВ, И СЧИТАТЬ ИХ ОДНОЙ ФОРМУЛОЙ НЕЛЬЗЯ:

    «X выше $P на дату T»            состояние в КОНЦЕ  → цифровой опцион
    «X опустится до $P за неделю»     КАСАНИЕ в любой момент → барьерный

Вероятность касания заметно выше вероятности оказаться там в конце: цена может
дойти до барьера и вернуться. Для типичных сроков разница двукратная. Посчитав
касание цифровой формулой, мы систематически занижали бы вероятность и покупали
бы «недооценённое» там, где недооценки нет. Здесь используется принцип
отражения, дающий точный ответ для геометрического броуновского движения.

ЧЕСТНАЯ ОГОВОРКА ПРО МЕРУ. Формулы дают вероятность в риск-нейтральной мере, а
рынок предсказаний торгует реальную. Различие в сносе: на горизонте часов и дней
оно мало по сравнению с волатильностью крипты, но оно ЕСТЬ, и на длинных сроках
им пренебрегать нельзя. Поэтому уверенность падает с ростом срока.

ВАЛИДАЦИЯ НЕ ПРОВОДИЛАСЬ, И ЭТО ЗАПИСАНО В МОДУЛЕ. Спецификация требует бэктест
до реальных денег. Историю подразумеваемой волатильности Deribit проверял:
истёкших контрактов отдаётся 44, сделок по ним ноль — бэктест на IV построить не
на чем. Версия на реализованной волатильности проверяема, но замер ещё не
сделан. До него модуль на реальные деньги не выходит.
"""

import json
import math
import re
import time
import urllib.request

from . import client, params
from .base import UNTESTED, SignalModule, SignalResult, Validation

_UA = {'User-Agent': 'Mozilla/5.0 (research bot)'}

# Активы, которые модуль берётся считать. Всё остальное — акции, оценки
# компаний, индексы аренды видеокарт — пропускается: там нет ни спота, ни
# волатильности, доступных этим же способом.
ASSETS = {
    'BITCOIN': 'BTC', 'BTC': 'BTC',
    'ETHEREUM': 'ETH', 'ETH': 'ETH',
    'SOLANA': 'SOL', 'SOL': 'SOL',
    'XRP': 'XRP', 'RIPPLE': 'XRP',
    'DOGECOIN': 'DOGE', 'DOGE': 'DOGE',
}


def _get(url):
    for attempt in range(params.RETRIES):
        try:
            req = urllib.request.Request(url, headers=_UA)
            with urllib.request.urlopen(req, timeout=params.TIMEOUT) as resp:
                return json.loads(resp.read().decode('utf-8', 'replace'))
        except Exception:                                   # noqa: BLE001
            if attempt == params.RETRIES - 1:
                return None
            time.sleep(1.0 + attempt)
    return None


def spot_and_vol(symbol, client_ccxt=None):
    """
    Текущая цена и годовая волатильность актива.

    Волатильность берётся РЕАЛИЗОВАННАЯ по часовым свечам, а не подразумеваемая.
    Подразумеваемая точнее описывает ожидания рынка, но её история недоступна
    (у Deribit отдаётся 44 истёкших контракта и ноль сделок по ним), а значит
    проверить построенную на ней стратегию нечем. Реализованная проверяема, и
    это перевешивает.
    """
    import exchange

    ex = client_ccxt or exchange.make_market_client(exchange.active_exchange_name())
    pair = f'{symbol}USDT'
    market = exchange.market_symbol(pair, ex)
    if market is None:
        return None
    bars = ex.fetch_ohlcv(market, '1h', limit=500)
    if not bars or len(bars) < 50:
        return None
    closes = [b[4] for b in bars]
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))
            if closes[i - 1] > 0]
    if len(rets) < 30:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    hourly = math.sqrt(var)
    return {'spot': float(closes[-1]),
            'sigma_annual': hourly * math.sqrt(24 * 365),
            'bars': len(closes)}


def parse_market(question):
    """
    Разбор вопроса: актив, порог, направление и вид (конец либо касание).

    Возвращает None, если вопрос не про цену известного актива. Это не
    осторожность ради осторожности: «Will Glean's valuation hit $10B» выглядит
    похоже, но ни спота, ни волатильности для него этим способом не достать, и
    посчитанная «вероятность» была бы выдумкой.
    """
    if not question:
        return None
    text = question.strip()
    upper = text.upper()

    symbol = None
    for name, code in ASSETS.items():
        if re.search(rf'\b{name}\b', upper):
            symbol = code
            break
    if symbol is None:
        return None

    price = re.search(r'\$\s*([\d,]+(?:\.\d+)?)\s*([KM])?', text, re.I)
    if not price:
        return None
    value = float(price.group(1).replace(',', ''))
    suffix = (price.group(2) or '').upper()
    if suffix == 'K':
        value *= 1_000
    elif suffix == 'M':
        value *= 1_000_000

    # Касание: «dip to», «hit», «reach», «touch». Состояние на дату: «above»,
    # «below», «be». Порядок проверок важен — «hit $X by date» это касание,
    # хотя в нём есть и дата.
    touch = bool(re.search(r'\b(dip to|hit|reach|touch|drop to)\b', text, re.I))
    down = bool(re.search(r'\b(dip|below|under|drop|less than)\b', text, re.I))
    return {'symbol': symbol, 'strike': value,
            'kind': 'touch' if touch else 'terminal',
            'direction': 'down' if down else 'up'}


def digital_probability(spot, strike, sigma, years, direction):
    """
    Вероятность оказаться выше (ниже) порога В КОНЦЕ срока.

    Логнормальная модель без сноса: N(d2) для «выше», 1 - N(d2) для «ниже».
    """
    if spot <= 0 or strike <= 0 or sigma <= 0 or years <= 0:
        return None
    d2 = (math.log(spot / strike) - 0.5 * sigma ** 2 * years) / (sigma * math.sqrt(years))
    up = 0.5 * (1 + math.erf(d2 / math.sqrt(2)))
    return up if direction == 'up' else 1 - up


def touch_probability(spot, barrier, sigma, years, direction):
    """
    Вероятность КОСНУТЬСЯ барьера хотя бы раз за срок.

    Принцип отражения для броуновского движения с нулевым сносом в логарифме:
    P(касание) = N(-|h| / s) + N(-|h| / s), где h — логарифмическое расстояние
    до барьера, s — накопленная волатильность. Проще говоря — удвоенная
    вероятность оказаться за барьером в конце, с поправкой на снос.

    ПОЧЕМУ ЭТО НЕ ПРИДИРКА: для срока в неделю и барьера в одном стандартном
    отклонении вероятность касания вдвое выше вероятности закрытия за ним.
    Спутав их, мы объявили бы половину рынков недооценёнными.
    """
    if spot <= 0 or barrier <= 0 or sigma <= 0 or years <= 0:
        return None
    h = math.log(barrier / spot)
    if (direction == 'down' and h >= 0) or (direction == 'up' and h <= 0):
        return 1.0                       # барьер уже пройден

    def n(x):
        return 0.5 * (1 + math.erf(x / math.sqrt(2)))

    # В логарифме цена — броуновское движение со сносом mu на единицу времени.
    # При нулевой ожидаемой доходности актива снос логарифма отрицателен:
    # mu = -sigma^2 / 2. Это не описка и не пессимизм, а поправка Ито.
    mu = -0.5 * sigma ** 2
    s = sigma * math.sqrt(years)
    drift = mu * years
    # Формула отражения одна и та же для обеих сторон, если брать |h|:
    #     P = N((-|h| + drift)/s) + exp(2*mu*|h|/sigma^2) * N((-|h| - drift)/s)
    # Знак сноса относительно барьера учитывается тем, что для верхнего барьера
    # снос мешает, а для нижнего помогает; здесь это входит через drift.
    dist = abs(h)
    sign = 1.0 if direction == 'up' else -1.0
    first = n((-dist + sign * drift) / s)
    factor = math.exp(2 * mu * sign * dist / sigma ** 2)
    second = factor * n((-dist - sign * drift) / s)
    return min(max(first + second, 0.0), 1.0)


def _years_until(end_iso):
    if not end_iso:
        return None
    try:
        stamp = time.strptime(str(end_iso)[:19], '%Y-%m-%dT%H:%M:%S')
    except ValueError:
        return None
    seconds = time.mktime(stamp) - time.mktime(time.gmtime())
    return seconds / (365 * 24 * 3600) if seconds > 0 else None


class CryptoSignal(SignalModule):
    """Стратегия 1 из спецификации, MVP на ценовых рынках крипты."""

    name = 'CRYPTO'
    validation = Validation(
        UNTESTED,
        'бэктест не проводился. Историю подразумеваемой волатильности Deribit '
        'не отдаёт (44 истёкших контракта, ноль сделок), поэтому вариант из '
        'спецификации непроверяем в принципе; вариант на реализованной '
        'волатильности проверяем, но замер ещё не сделан')

    def __init__(self, vol_source=None):
        # Источник волатильности подменяем в тестах: ходить на биржу из теста
        # значит поставить его в зависимость от сети и от рынка.
        self._vol = vol_source or spot_and_vol
        self._cache = {}

    def _market_data(self, symbol):
        if symbol not in self._cache:
            self._cache[symbol] = self._vol(symbol)
        return self._cache[symbol]

    def scan(self, markets):
        out = []
        for m in markets:
            parsed = parse_market(m.get('question'))
            if not parsed:
                continue
            years = _years_until(m.get('endDate'))
            if not years:
                continue
            data = self._market_data(parsed['symbol'])
            if not data:
                continue
            if parsed['kind'] == 'touch':
                model = touch_probability(data['spot'], parsed['strike'],
                                          data['sigma_annual'], years,
                                          parsed['direction'])
            else:
                model = digital_probability(data['spot'], parsed['strike'],
                                            data['sigma_annual'], years,
                                            parsed['direction'])
            if model is None:
                continue
            try:
                price = float(json.loads(m.get('outcomePrices') or '[]')[0])
            except Exception:                              # noqa: BLE001
                continue

            # Уверенность падает с ростом срока: расхождение риск-нейтральной и
            # реальной меры растёт со временем, и на горизонте месяцев наша
            # оценка перестаёт значить то, что спрашивает рынок.
            days = years * 365
            confidence = 1.0 if days <= 7 else max(0.3, 7.0 / days)
            out.append(SignalResult(
                model_probability=model,
                market_probability=price,
                confidence=confidence,
                data_sources=['exchange:ohlcv-1h', 'polymarket:gamma'],
                market=m,
                cost=client.entry_cost(price, client.fee_rate(m)),
                liquidity=float(m.get('liquidity') or 0),
                note=f'{parsed["symbol"]} {parsed["kind"]} {parsed["direction"]} '
                     f'{parsed["strike"]:.6g}, срок {days:.1f}д, '
                     f'вола {data["sigma_annual"] * 100:.0f}%'))
        return out
