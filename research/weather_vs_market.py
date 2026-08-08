"""
Погода: обыгрывает ли наше распределение ЦЕНУ рынка. Решающий замер.

ЧТО УЖЕ ИЗВЕСТНО. Прогноз с поправкой на станцию бьёт климатическую подсказку
на 30% по оценке Брайера и попадает в корзину в 5.8 раза чаще
(`weather_skill.py`). Но климат — соперник слабый. Участники рынка пользуются
теми же бесплатными прогнозами, и вполне может оказаться, что цена уже содержит
всё, что мы намерили. Тогда навык есть, а заработка нет.

Здесь соперник настоящий: цена корзины за сутки до разрешения.

ЧТО СЧИТАЕТСЯ. Для каждого разрешённого погодного события берутся цены всех
корзин за 24 часа до конца, наше распределение на тот же момент и фактический
исход. Два вопроса, и они разные:

    1. ЧЬЁ РАСПРЕДЕЛЕНИЕ ТОЧНЕЕ — сравнение оценок Брайера. Отвечает, знаем ли
       мы что-то сверх рынка.
    2. ЗАРАБОТАЛИ БЫ МЫ — ставка на корзины, где наша вероятность выше цены на
       величину, превышающую издержки. Отвечает, можно ли это превратить в
       деньги.

Второе не следует из первого. Можно быть точнее в среднем и всё равно терять
на издержках, если расхождения мелкие; и наоборот, можно быть точнее лишь
изредка, но крупно, и зарабатывать.

ИЗДЕРЖКИ ПО ФОРМУЛЕ БИРЖИ: комиссия = 0.05 × p × (1 − p) на контракт, только с
тейкера и только на входе. Плюс пересечение спреда, заданное явно. У дешёвых
корзин издержки в процентах ставки чудовищны — на калибровочном замере они
достигали 22% у корзин по 5 центов, — поэтому ставки дешевле MIN_PRICE не
рассматриваются вовсе.

ПОПРАВКА НА СТАНЦИЮ БЕРЁТСЯ ИЗ ПРОШЛОГО, А НЕ ИЗ ТОГО ЖЕ ОКНА. Смещение и
разброс считаются по дням СТРОГО ДО начала окна с ценами. Обучить их на тех же
днях, на которых потом торгуем, значило бы знать ответ заранее — и получить
красивый, ничего не значащий результат.

ГРАДУСЫ РАЗНЫЕ. Часть городов торгуется в Фаренгейтах (Нью-Йорк, Чикаго,
Лос-Анджелес, Майами), остальные в Цельсиях. Наблюдения и прогноз идут в
Цельсиях, поэтому для таких городов и прогноз, и разброс переводятся, а не
сравниваются как есть. Ошибка здесь не упала бы, а тихо сдвинула бы всё
распределение на два десятка корзин.

ПРИЁМКА, ЗАПИСАННАЯ ДО ПРОГОНА:

    край считается пригодным, если ставки по правилу «наша вероятность выше
    цены больше чем на издержки» дают положительный результат ПОСЛЕ издержек,
    интервал по СОБЫТИЯМ не накрывает ноль И событий не меньше 50.

Интервал по событиям, а не по ставкам: одиннадцать корзин одного города и дня —
это один исход погоды, а не одиннадцать наблюдений. Считая их независимыми, мы
сузили бы интервал втрое на пустом месте. Ровно этой ошибкой, судя по всему,
получены обещания «края 15%» в статьях про Polymarket.

ИТОГ 2026-08-08. РЕЗУЛЬТАТ ПРОТИВОРЕЧИВЫЙ, И ПРЕЖДЕ ВСЕГО НАДО ПРИЗНАТЬ ОШИБКУ
В САМОЙ ПРИЁМКЕ. В этом файле записаны ДВА условия, и они не совпадают: в
заголовке — только про доходность ставок, в коде вердикта добавлено требование
побить рынок по Брайеру. Оба написаны до прогона, но это не оправдание:
приёмка должна быть одна. Ниже приводятся оба чтения, и ни одно не выдаётся за
пройденное.

    чьё распределение точнее   Брайер наш 0.0558 против рыночного 0.0503
                               РЫНОК ЛУЧШЕ на 11%
    ставки по расхождению      +0.433 на вложенный доллар
                               интервал по событиям [+0.152; +0.741]
                               250 ставок в 211 событиях

По условию из заголовка — проходит. По условию из кода — нет.

ПОЧЕМУ ЭТО НЕ ПРОТИВОРЕЧИЕ ПО СУЩЕСТВУ. Рынок точнее НА ВСЕЙ СОВОКУПНОСТИ
корзин, а ставим мы на 250 из нескольких тысяч — там, где расхождение с ценой
превышает издержки. Быть хуже в среднем и лучше на отобранном подмножестве
можно; именно на это и рассчитан отбор.

ЗАГЛЯДЫВАНИЯ ВПЕРЁД, СКОРЕЕ ВСЕГО, НЕТ, И ПРОВЕРЯЕТСЯ ЭТО ИМЕННО ПЕРВЫМ
ВОПРОСОМ. Опасение было в том, что архив отдаёт прогноз, выпущенный позже
момента, когда бралась цена. Будь так, наш Брайер БИЛ БЫ рыночный. Он ему
проигрывает — значит более свежей информации у нас нет.

НО ВЕРИТЬ ЦИФРЕ +0.433 НЕЛЬЗЯ, И ВОТ ПОЧЕМУ:

    выигрышных ставок          37%
    медиана ставки             -1.068  (то есть чаще всего теряем всё вложенное)
    вклад лучших ПЯТИ ставок   39% всей суммы

Пять ставок из двухсот пятидесяти — два процента выборки — дают почти сорок
процентов результата. Среднее при таком распределении неустойчиво: убери
удачную неделю, и оно рассыплется. Деление по времени даёт +0.402 и +0.460,
знак держится на обеих половинах, но ранняя половина интервалом накрывает ноль.

И ГЛАВНОЕ: ВСЕГО ОДИН МЕСЯЦ. Глубже история цен не живёт. Месяц с
хвостозависимым результатом — это признак, а не доказательство.

ВЫВОД. Направление перспективное и единственное из проверенных на этой
площадке, где вообще что-то нашлось. Но масштабировать нечего: правильный
следующий шаг — копить цены живьём и вернуться к вопросу через три месяца, имея
свой ряд. Ставить деньги можно только размером, потеря которого ничего не
решает, и исключительно ради проверки исполнения, а не ради дохода.

Запуск:
    python research/weather_vs_market.py
"""

import io
import json
import os
import re
import sys
import time
import urllib.request
from collections import defaultdict
from math import erf, sqrt

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from weather_skill import (STATIONS, cached, fetch, forecast_max,  # noqa: E402
                           observed_max, station_meta)

GAMMA = 'https://gamma-api.polymarket.com'
CLOB = 'https://clob.polymarket.com'

PRICE_DAYS = 32          # глубже история цен не живёт
TRAIN_DAYS = 330         # на чём учится поправка, СТРОГО до окна цен
HORIZON_H = 24           # за сколько часов до конца берём цену
FEE_RATE = 0.05          # категория weather_fees
CROSS = 0.01             # пересечение спреда
MIN_PRICE = 0.10         # дешевле — издержки съедают всё
MIN_EDGE = 0.03          # ниже этого расхождения не ставим
MIN_EVENTS = 50

SLUG = {
    'New York City': 'nyc', 'London': 'london', 'Paris': 'paris',
    'Tokyo': 'tokyo', 'Tel Aviv': 'tel-aviv', 'Moscow': 'moscow',
    'Chicago': 'chicago', 'Los Angeles': 'los-angeles', 'Miami': 'miami',
    'Hong Kong': 'hong-kong', 'Singapore': 'singapore',
    'Seoul (Incheon)': 'seoul-incheon', 'Amsterdam': 'amsterdam',
    'Madrid': 'madrid', 'Toronto': 'toronto',
}
MONTHS = ('january february march april may june july august september '
          'october november december').split()


def to_f(c):
    return c * 9 / 5 + 32


def bucket_of(question):
    """
    Из вопроса корзины — её граница и вид.

    Возвращает (число, вид, единица), где вид: 'exact', 'below', 'above'.
    None — вопрос не разобран, и такую корзину нельзя молча считать обычной:
    ошибка разбора превратила бы «31 или ниже» в «ровно 31».
    """
    m = re.search(r'be\s+(-?\d+)\s*°?\s*([CF])\s*(or below|or higher)?',
                  question, re.I)
    if not m:
        return None
    value = int(m.group(1))
    unit = m.group(2).upper()
    tail = (m.group(3) or '').lower()
    kind = 'below' if 'below' in tail else ('above' if 'higher' in tail
                                            else 'exact')
    return value, kind, unit


def normal_prob(centre, sigma, value, kind):
    """Вероятность корзины при нормальной ошибке вокруг centre."""
    def cdf(x):
        return 0.5 * (1 + erf((x - centre) / (sigma * sqrt(2))))
    if kind == 'below':
        return cdf(value + 0.5)
    if kind == 'above':
        return 1 - cdf(value - 0.5)
    return cdf(value + 0.5) - cdf(value - 0.5)


def event_prices(slug):
    """Корзины события: цена за сутки до конца, исход, вопрос."""
    def build():
        ev = fetch(f'{GAMMA}/events?slug={slug}')
        if not ev:
            return None
        markets = (ev[0].get('markets') or [])
        out = []
        for m in markets:
            try:
                prices = json.loads(m.get('outcomePrices') or '[]')
                tokens = json.loads(m.get('clobTokenIds') or '[]')
            except Exception:                              # noqa: BLE001
                continue
            if sorted(prices) != ['0', '1'] or not tokens:
                continue
            hist = fetch(f'{CLOB}/prices-history?market={tokens[0]}'
                         f'&interval=max&fidelity=60')
            points = (hist or {}).get('history') or []
            if len(points) < 6:
                continue
            target = points[-1]['t'] - HORIZON_H * 3600
            fit = [p for p in points if p['t'] <= target]
            if not fit:
                continue
            out.append({'question': m.get('question') or '',
                        'price': float(fit[-1]['p']),
                        'won': 1 if prices[0] == '1' else 0})
            time.sleep(0.05)
        return out
    return cached(f'ev_{slug}.json', build)


def main():
    today = time.time()
    price_start = time.strftime('%Y-%m-%d',
                                time.gmtime(today - PRICE_DAYS * 86400))
    train_start = time.strftime(
        '%Y-%m-%d', time.gmtime(today - (PRICE_DAYS + TRAIN_DAYS) * 86400))
    train_end = time.strftime('%Y-%m-%d',
                              time.gmtime(today - (PRICE_DAYS + 1) * 86400))
    print(f'поправка учится {train_start} … {train_end}')
    print(f'цены берутся с {price_start}\n')

    fitted = {}
    for city, icao in STATIONS.items():
        meta = station_meta(icao)
        if not meta:
            continue
        zone = meta.get('tz') or 'Etc/UTC'
        obs = observed_max(icao, train_start, train_end, zone)
        fcs = forecast_max(meta['lat'], meta['lon'], train_start, train_end,
                           zone)
        if not obs or not fcs:
            continue
        days = sorted(set(obs) & set(fcs))
        if len(days) < 100:
            continue
        errs = np.array([obs[d] - fcs[d] for d in days], dtype=float)
        fitted[city] = {'icao': icao, 'meta': meta,
                        'bias': float(errs.mean()),
                        'sigma': max(float(errs.std(ddof=1)), 0.4)}
    print(f'станций с поправкой: {len(fitted)}')

    bets_by_event, brier_us, brier_mkt = defaultdict(list), [], []
    events_seen = 0
    for city, fit in fitted.items():
        slug_city = SLUG.get(city)
        if not slug_city:
            continue
        zone = fit['meta'].get('tz') or 'Etc/UTC'
        obs = observed_max(fit['icao'], price_start,
                           time.strftime('%Y-%m-%d', time.gmtime(today)), zone)
        fcs = forecast_max(fit['meta']['lat'], fit['meta']['lon'], price_start,
                           time.strftime('%Y-%m-%d', time.gmtime(today)), zone)
        if not obs or not fcs:
            continue
        for back in range(2, PRICE_DAYS):
            stamp = time.gmtime(today - back * 86400)
            day = time.strftime('%Y-%m-%d', stamp)
            if day not in fcs:
                continue
            slug = (f'highest-temperature-in-{slug_city}-on-'
                    f'{MONTHS[stamp.tm_mon - 1]}-{stamp.tm_mday}-2026')
            rows = event_prices(slug)
            if not rows:
                continue
            events_seen += 1
            for row in rows:
                parsed = bucket_of(row['question'])
                if not parsed:
                    continue
                value, kind, unit = parsed
                centre = fcs[day] + fit['bias']
                sigma = fit['sigma']
                if unit == 'F':
                    centre, sigma = to_f(centre), sigma * 9 / 5
                ours = normal_prob(centre, sigma, value, kind)
                price = row['price']
                brier_us.append((ours - row['won']) ** 2)
                brier_mkt.append((price - row['won']) ** 2)
                if price < MIN_PRICE or ours - price < MIN_EDGE:
                    continue
                fee = FEE_RATE * price * (1 - price)
                net = (row['won'] - price - fee - CROSS) / price
                bets_by_event[slug].append(net)

    print(f'событий с ценами: {events_seen}, корзин оценено: {len(brier_us)}')
    if not brier_us:
        print('данных нет — замер не состоялся')
        return

    print()
    print('=' * 84)
    print('ВОПРОС 1: ЧЬЁ РАСПРЕДЕЛЕНИЕ ТОЧНЕЕ')
    print('=' * 84)
    bu, bm = float(np.mean(brier_us)), float(np.mean(brier_mkt))
    print(f'  Брайер наш {bu:.4f}   Брайер рынка {bm:.4f}   '
          f'{"мы лучше" if bu < bm else "РЫНОК ЛУЧШЕ"} на {abs(bm - bu) / bm * 100:.1f}%')

    print()
    print('=' * 84)
    print('ВОПРОС 2: ЗАРАБОТАЛИ БЫ МЫ')
    print(f'ставим, когда наша вероятность выше цены на {MIN_EDGE:+.2f} и цена'
          f' не ниже {MIN_PRICE}')
    print('=' * 84)
    events = [e for e, v in bets_by_event.items() if v]
    if len(events) < MIN_EVENTS:
        print(f'  событий со ставками {len(events)} — меньше {MIN_EVENTS}, '
              'замер не состоялся')
        return
    per_event = np.array([np.mean(bets_by_event[e]) for e in events])
    flat = np.concatenate([bets_by_event[e] for e in events])
    rng = np.random.default_rng(20260808)
    boots = rng.choice(per_event, size=(10_000, len(per_event)),
                       replace=True).mean(axis=1)
    lo, hi = np.percentile(boots, 2.5), np.percentile(boots, 97.5)
    print(f'  ставок {len(flat)} в {len(events)} событиях')
    print(f'  чистый результат на вложенный доллар: {flat.mean():+.3f}')
    print(f'  интервал по событиям: [{lo:+.3f}; {hi:+.3f}]')

    print()
    print('=' * 84)
    if bu < bm and lo > 0:
        print('ПРИГОДЕН: мы точнее рынка И ставки в плюсе с интервалом от нуля.')
        print('Дальше — сбор живых данных и малый размер на реальных деньгах.')
    elif bu < bm:
        print('ЧАСТИЧНО: распределение точнее рынка, но ставки края не дают.')
        print('Расхождения слишком мелкие для издержек — это не повод торговать.')
    else:
        print('НЕ ПРИГОДЕН: рынок точнее нас.')
        print('Бесплатный прогноз с поправкой уже учтён в цене. Чтобы обыграть,')
        print('нужен источник, которого у рынка нет, а не тот же самый.')


if __name__ == '__main__':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    main()
