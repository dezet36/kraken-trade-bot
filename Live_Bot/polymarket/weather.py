"""
Погода: распределение дневного максимума на станции и вероятности корзин.

ЧТО ЗДЕСЬ ИЗМЕРЕНО, А ЧТО ПРЕДПОЛОЖЕНО. Измерено: прогноз с поправкой на
станцию бьёт климатическую подсказку — Брайер 0.6697 против 0.9555, попадание
в корзину 43.6% против 7.5% на пятнадцати станциях за 398 дней каждая.
Предположено: что этого хватит, чтобы обыграть ЦЕНУ. Замер на месячном окне дал
+0.433 на вложенный доллар, но лучшие пять ставок из двухсот пятидесяти дали
39% всей суммы — результат хвостозависимый и на одном месяце. Поэтому модуль
считает и показывает, а решение о деньгах остаётся за человеком.

ПОЧЕМУ РАСПРЕДЕЛЕНИЕ СТРОИТСЯ ИЗ ОШИБОК, А НЕ ИЗ АНСАМБЛЯ. Ансамбль из 31
участника даёт распределение напрямую, но ЗА ПРОШЛОЕ он не отдаётся вовсе:
поля приходят, значения пустые. Проверить его качество не на чем, а верить
непроверенному нельзя. Распределение собственных ошибок прогноза измеримо на
годах и включает всё сразу: ошибку модели, несовпадение узла сетки со станцией
и разницу между дневным максимумом и максимумом почасовых сводок.

ПОПРАВКА НЕ СЧИТАЕТСЯ НА ЛЕТУ. Она обучается отдельно и лежит файлом:
пересчёт при каждом обращении означал бы, что вчерашнее решение сегодня уже не
воспроизвести. Ровно по этой причине разметка сделок в боте тоже сохраняется, а
не пересчитывается.
"""

import json
import math
import os
import re
import time
import urllib.parse
import urllib.request

import config

from . import params

CALIBRATION = os.path.join(config.DATA_DIR, 'polymarket_data',
                           'weather_bias.json')
_UA = {'User-Agent': 'Mozilla/5.0 (research bot)'}


def _get(url, as_json=True):
    for attempt in range(params.RETRIES):
        try:
            req = urllib.request.Request(url, headers=_UA)
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = resp.read().decode('utf-8', 'replace')
            return json.loads(body) if as_json else body
        except Exception:                                   # noqa: BLE001
            if attempt == params.RETRIES - 1:
                return None
            time.sleep(2.0 + 2 * attempt)
    return None


def station_meta(icao):
    """Координаты и часовой пояс станции."""
    data = (_get(f'https://mesonet.agron.iastate.edu/api/1/station/{icao}.json')
            or {}).get('data')
    if isinstance(data, list):
        data = data[0] if data else None
    if not isinstance(data, dict):
        return None
    lat, lon = data.get('latitude'), data.get('longitude')
    if lat is None or lon is None:
        return None
    return {'lat': float(lat), 'lon': float(lon),
            'tz': data.get('tzname') or 'Etc/UTC', 'name': data.get('name')}


def observed_daily_max(icao, start, end, tz):
    """
    Максимум почасовых сводок по дням — то, ЧЕМ РАЗРЕШАЕТСЯ рынок.

    Сутки МЕСТНЫЕ. Рынок спрашивает про календарный день на станции, а граница
    UTC-суток для Нью-Йорка приходится на восемь вечера по местному времени —
    в «день» попал бы вечер предыдущего и утро следующего, тогда как максимум
    бывает после полудня.
    """
    y1, m1, d1 = start.split('-')
    y2, m2, d2 = end.split('-')
    txt = _get('https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py?'
               f'station={icao}&data=tmpc&year1={y1}&month1={m1}&day1={d1}'
               f'&year2={y2}&month2={m2}&day2={d2}'
               f'&tz={urllib.parse.quote(tz, safe="")}&format=onlycomma'
               '&latlon=no&missing=empty&trace=empty&direct=no&report_type=3',
               as_json=False)
    if not txt:
        return {}
    by_day = {}
    for line in txt.strip().split('\n')[1:]:
        parts = line.split(',')
        if len(parts) < 3 or not parts[2]:
            continue
        try:
            value = float(parts[2])
        except ValueError:
            continue
        day = parts[1][:10]
        if day not in by_day or value > by_day[day]:
            by_day[day] = value
    return by_day


def archived_forecast(lat, lon, start, end, tz):
    """Прогноз дневного максимума, каким он был тогда."""
    data = _get('https://historical-forecast-api.open-meteo.com/v1/forecast?'
                f'latitude={lat}&longitude={lon}&start_date={start}'
                f'&end_date={end}&daily=temperature_2m_max'
                f'&timezone={urllib.parse.quote(tz, safe="")}')
    daily = (data or {}).get('daily') or {}
    return {t: v for t, v in zip(daily.get('time') or [],
                                 daily.get('temperature_2m_max') or [])
            if v is not None}


def live_forecast(lat, lon, tz, days=3):
    """Прогноз на ближайшие дни — то, по чему принимается решение сейчас."""
    data = _get('https://api.open-meteo.com/v1/forecast?'
                f'latitude={lat}&longitude={lon}&daily=temperature_2m_max'
                f'&forecast_days={days}&timezone={urllib.parse.quote(tz, safe="")}')
    daily = (data or {}).get('daily') or {}
    return {t: v for t, v in zip(daily.get('time') or [],
                                 daily.get('temperature_2m_max') or [])
            if v is not None}


def fit_calibration(stations=None, days=None):
    """
    Обучает смещение и разброс ошибки по каждой станции и сохраняет файлом.

    Возвращает словарь станций. Станции, где данных мало, пропускаются молча —
    но в файл не попадают, и ставить по ним нельзя.
    """
    stations = stations or params.STATIONS
    days = days or params.BIAS_DAYS
    end = time.strftime('%Y-%m-%d', time.gmtime(time.time() - 2 * 86400))
    start = time.strftime('%Y-%m-%d', time.gmtime(time.time() - days * 86400))

    out = {}
    for city, icao in stations.items():
        meta = station_meta(icao)
        if not meta:
            continue
        obs = observed_daily_max(icao, start, end, meta['tz'])
        fcs = archived_forecast(meta['lat'], meta['lon'], start, end, meta['tz'])
        common = sorted(set(obs) & set(fcs))
        if len(common) < 100:
            continue
        errs = [obs[d] - fcs[d] for d in common]
        mean = sum(errs) / len(errs)
        var = sum((e - mean) ** 2 for e in errs) / (len(errs) - 1)
        out[city] = {'icao': icao, 'lat': meta['lat'], 'lon': meta['lon'],
                     'tz': meta['tz'], 'bias': mean,
                     'sigma': max(math.sqrt(var), 0.4), 'days': len(common),
                     'fitted_at': time.strftime('%Y-%m-%dT%H:%M:%SZ',
                                                time.gmtime())}
    os.makedirs(os.path.dirname(CALIBRATION), exist_ok=True)
    with open(CALIBRATION, 'w', encoding='utf-8') as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)
    return out


def calibration():
    """Обученная поправка. Пустой словарь — не обучена, ставить нельзя."""
    if not os.path.exists(CALIBRATION):
        return {}
    try:
        with open(CALIBRATION, encoding='utf-8') as fh:
            return json.load(fh)
    except Exception:                                       # noqa: BLE001
        return {}


def parse_bucket(question):
    """
    Из вопроса корзины — граница, вид и единица.

    Вид: 'exact' (ровно N), 'below' (N или ниже), 'above' (N или выше). None —
    вопрос не разобран, и такую корзину нельзя молча считать обычной: ошибка
    разбора превратила бы «31 или ниже» в «ровно 31» и перевернула бы ставку.
    """
    m = re.search(r'be\s+(-?\d+)\s*°?\s*([CF])\s*(or below|or higher)?',
                  question or '', re.I)
    if not m:
        return None
    tail = (m.group(3) or '').lower()
    kind = 'below' if 'below' in tail else ('above' if 'higher' in tail
                                            else 'exact')
    return {'value': int(m.group(1)), 'kind': kind, 'unit': m.group(2).upper()}


def _cdf(x, centre, sigma):
    return 0.5 * (1 + math.erf((x - centre) / (sigma * math.sqrt(2))))


def bucket_probability(bucket, forecast_c, bias, sigma):
    """
    Вероятность корзины при нашей поправке.

    Для рынков в Фаренгейтах переводятся И центр, И разброс. Забыть про разброс
    — распространённая ошибка: он в градусах, и без перевода распределение
    оказалось бы вдвое уже, чем на самом деле.
    """
    centre = forecast_c + bias
    spread = sigma
    if bucket['unit'] == 'F':
        centre = centre * 9 / 5 + 32
        spread = sigma * 9 / 5
    value = bucket['value']
    if bucket['kind'] == 'below':
        return _cdf(value + 0.5, centre, spread)
    if bucket['kind'] == 'above':
        return 1 - _cdf(value - 0.5, centre, spread)
    return _cdf(value + 0.5, centre, spread) - _cdf(value - 0.5, centre, spread)


def city_of(question):
    """Город из вопроса рынка. None — не разобрано."""
    if not question or ' in ' not in question:
        return None
    return question.split(' in ', 1)[1].split(' be ')[0].strip()


def signals(markets, forecasts=None):
    """
    Расхождения между нашей вероятностью и ценой по списку рынков.

    Возвращает список словарей с полями model, market, edge, cost, city.
    Рынки без обученной станции, с разбросом выше предела или с неразобранной
    корзиной пропускаются — молча ставить по ним нельзя.
    """
    from . import client

    fitted = calibration()
    cache = dict(forecasts or {})
    out = []
    for m in markets:
        city = city_of(m.get('question'))
        fit = fitted.get(city)
        if not fit or fit['sigma'] > params.MAX_SIGMA:
            continue
        bucket = parse_bucket(m.get('question'))
        if not bucket:
            continue
        end = str(m.get('endDate') or '')[:10]
        if not end:
            continue
        key = (city, end)
        if key not in cache:
            cache[key] = live_forecast(fit['lat'], fit['lon'], fit['tz']).get(end)
        forecast = cache[key]
        if forecast is None:
            continue
        try:
            price = float(json.loads(m.get('outcomePrices') or '[]')[0])
        except Exception:                                  # noqa: BLE001
            continue
        model = bucket_probability(bucket, forecast, fit['bias'], fit['sigma'])
        rate = client.fee_rate(m)
        out.append({'market': m, 'city': city, 'bucket': bucket,
                    'forecast_c': forecast, 'model': model, 'price': price,
                    'edge': model - price,
                    'cost': client.entry_cost(price, rate),
                    'liquidity': float(m.get('liquidity') or 0)})
    return out
