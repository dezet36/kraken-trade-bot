"""
Погода: помогает ли отбор городов по разбросу ошибки прогноза.

ОТКУДА ВОПРОС. Разброс ошибки различается между станциями втрое: Лондон 0.56,
Париж 0.54, Сеул 1.96, Амстердам 1.29. Смысл этой величины прямой. Корзина
шириной один градус; при разбросе в полградуса распределение почти целиком
ложится в одну-две корзины, и назвать вероятность можно уверенно. При разбросе
около двух градусов оно размазывается на пять, и «вероятность» перестаёт что-то
значить.

Отсюда гипотеза: ставки в городах с малым разбросом должны быть лучше.

ПОЧЕМУ ЭТОТ ОТБОР ЧЕСТЕН, А НЕ ПОДСМАТРИВАЕТ. Разброс обучается на днях СТРОГО
ДО окна с ценами: те же 330 дней, на которых считается поправка. К исходам
ставок он отношения не имеет и в момент решения известен. Это принципиально
отличается от отбора «оставим города, где получилось хорошо» — такой отбор
всегда даёт красивый результат и никогда не повторяется.

ПОРОГ НЕ ПОДБИРАЕТСЯ. Берётся медиана обученных разбросов — число, которое
нельзя выбрать в свою пользу. Перебор порогов превратил бы замер в рыбалку:
пятнадцать станций дают четырнадцать возможных делений, и лучшее из них
окажется хорошим по чистой случайности.

ПРИЁМКА, ЗАПИСАННАЯ ДО ПРОГОНА:

    отбор считается полезным, если половина с МАЛЫМ разбросом положительна
    после издержек, превосходит половину с БОЛЬШИМ разбросом, интервал разницы
    по событиям не накрывает ноль И в каждой половине не меньше 30 событий.

Требование про интервал РАЗНИЦЫ, а не про интервал каждой половины: вопрос
здесь не «зарабатывает ли малый разброс», а «даёт ли отбор прибавку». Половина
может быть в плюсе просто потому, что в плюсе вся стратегия.

ЧЕГО ЗАМЕР НЕ ОТВЕЧАЕТ. Он идёт по тому же месячному окну, что и основной, и
наследует его беду: результат там хвостозависим — лучшие пять ставок из 250
дали 39% суммы. Если отбор поможет, это будет признаком, а не доказательством.

ИТОГ 2026-08-08, И ЧИТАТЬ ЕГО НАДО В ДВА ПРИЁМА.

ПЕРВОЕ ЧТЕНИЕ — ПО ЗАПИСАННОМУ УСЛОВИЮ, ОТБОР НЕ ПОДТВЕРДИЛСЯ:

    малый разброс     148 ставок, 119 событий, +0.612  [+0.320; +1.063]
    большой разброс   102 ставки,  92 события, +0.173  [-0.309; +0.607]
    разница                                    +0.439  [-0.055; +1.139]

Половина с малым разбросом значимо положительна, с большим — нет. Но интервал
РАЗНИЦЫ накрывает ноль нижней границей -0.055, то есть промахивается на волосок.
Приёмка не смягчается.

ВТОРОЕ ЧТЕНИЕ — ПОСЛЕ ИСПРАВЛЕНИЯ ЕДИНИЦ, И ОНО ВСКРЫЛО ОШИБКУ ЗАМЕРА, А НЕ
МОДЕЛИ. В таблице по городам бросились в глаза три станции с ОДНОЙ ставкой
каждая, и все три проиграли полностью: Лос-Анджелес, Чикаго, Нью-Йорк. Это
города, торгующиеся в Фаренгейтах.

Причина оказалась не в переводе (лестница в °F даёт ровно 1.000000, проверено),
а в том, что разброс в ГРАДУСАХ несопоставим между городами. Корзина в
Фаренгейтах шириной 1°F = 0.56°C, значит при одном и том же разбросе в Цельсиях
распределение размазано вдвое шире ПО ЧИСЛУ КОРЗИН:

    Цельсий,    разброс 0.8:  три значимые корзины, у центральной 0.468
    Фаренгейт,  разброс 0.8:  семь корзин,          у центральной 0.269

Нью-Йорк с разбросом 0.83°C — это 1.50 корзины, то есть хуже Амстердама (1.49),
худшего в списке, а не середина, как показывало первое чтение. Сравнение по
градусам просто складывало разные единицы.

С разбросом, выраженным В ШИРИНАХ КОРЗИНЫ, деление по медиане даёт:

    малый     126 ставок,  97 событий, +0.764  [+0.487; +1.324]
    большой   124 ставки, 114 событий, +0.096  [-0.339; +0.486]
    разница                            +0.668  [+0.263; +1.408]

Формально это проходит приёмку. НО ЭТО ВТОРОЙ ВЗГЛЯД НА ТЕ ЖЕ ДАННЫЕ, и выдавать
его за пройденную проверку нельзя: разница между «исправил единицы» и «подобрал
переменную, глядя на результат» видна только изнутри, а со стороны они выглядят
одинаково. Правильный статус — гипотеза, ожидающая свежих данных.

ЧТО ИЗ ЭТОГО ВСЁ-ТАКИ ПРИМЕНЕНО В БОЮ. Исправление единиц — потому что оно верно
независимо от исхода замера: сравнивать разброс в °C между рынками с разной
шириной корзины неправильно само по себе. Отбор городов по порогу в бой НЕ
вынесен: он остаётся непроверенным.

Запуск:
    python research/weather_by_city.py
"""

import io
import json
import os
import sys
import time
from collections import defaultdict

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'Live_Bot'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import weather_vs_market as wm  # noqa: E402
from weather_skill import STATIONS, forecast_max, observed_max, station_meta  # noqa: E402

MIN_EVENTS = 30
BOOTSTRAP = 10_000
RNG = np.random.default_rng(20260808)


def fitted_stations(train_start, train_end):
    """Смещение и разброс по каждой станции на обучающем окне."""
    out = {}
    for city, icao in STATIONS.items():
        meta = station_meta(icao)
        if not meta:
            continue
        zone = meta.get('tz') or 'Etc/UTC'
        obs = observed_max(icao, train_start, train_end, zone)
        fcs = forecast_max(meta['lat'], meta['lon'], train_start, train_end, zone)
        if not obs or not fcs:
            continue
        days = sorted(set(obs) & set(fcs))
        if len(days) < 100:
            continue
        errs = np.array([obs[d] - fcs[d] for d in days], dtype=float)
        out[city] = {'icao': icao, 'meta': meta, 'bias': float(errs.mean()),
                     'sigma': max(float(errs.std(ddof=1)), 0.4),
                     'days': len(days)}
    return out


def collect_bets(fitted, price_start, today):
    """Ставки по правилу основного замера, с пометкой города и разброса."""
    now = time.strftime('%Y-%m-%d', time.gmtime(today))
    bets = []
    for city, fit in fitted.items():
        slug_city = wm.SLUG.get(city)
        if not slug_city:
            continue
        zone = fit['meta'].get('tz') or 'Etc/UTC'
        fcs = forecast_max(fit['meta']['lat'], fit['meta']['lon'],
                           price_start, now, zone) or {}
        for back in range(2, wm.PRICE_DAYS):
            stamp = time.gmtime(today - back * 86400)
            day = time.strftime('%Y-%m-%d', stamp)
            if day not in fcs:
                continue
            slug = (f'highest-temperature-in-{slug_city}-on-'
                    f'{wm.MONTHS[stamp.tm_mon - 1]}-{stamp.tm_mday}-2026')
            path = os.path.join(wm.ROOT, 'research', 'polymarket_cache',
                                'weather', f'ev_{slug}.json')
            if not os.path.exists(path):
                continue
            with open(path, encoding='utf-8') as fh:
                rows = json.load(fh)
            for row in rows:
                parsed = wm.bucket_of(row['question'])
                if not parsed:
                    continue
                value, kind, unit = parsed
                centre, spread = fcs[day] + fit['bias'], fit['sigma']
                if unit == 'F':
                    centre, spread = wm.to_f(centre), spread * 9 / 5
                ours = wm.normal_prob(centre, spread, value, kind)
                price = row['price']
                if price < wm.MIN_PRICE or ours - price < wm.MIN_EDGE:
                    continue
                fee = wm.FEE_RATE * price * (1 - price)
                net = (row['won'] - price - fee - wm.CROSS) / price
                bets.append({'city': city, 'sigma': fit['sigma'],
                             'event': slug, 'net': net})
    return bets


def event_ci(bets):
    """Интервал по СОБЫТИЯМ: корзины одного дня — один исход погоды."""
    by_event = defaultdict(list)
    for b in bets:
        by_event[b['event']].append(b['net'])
    events = list(by_event)
    if len(events) < 2:
        return float('nan'), float('nan'), len(events)
    per = np.array([np.mean(by_event[e]) for e in events], dtype=float)
    boots = RNG.choice(per, size=(BOOTSTRAP, len(per)), replace=True).mean(axis=1)
    return (float(np.percentile(boots, 2.5)), float(np.percentile(boots, 97.5)),
            len(events))


def diff_ci(low, high):
    """Интервал РАЗНИЦЫ двух групп, обе пересэмплируются по событиям."""
    def per_event(bets):
        by = defaultdict(list)
        for b in bets:
            by[b['event']].append(b['net'])
        return np.array([np.mean(v) for v in by.values()], dtype=float)

    a, b = per_event(low), per_event(high)
    if len(a) < 2 or len(b) < 2:
        return float('nan'), float('nan')
    boots = (RNG.choice(a, size=(BOOTSTRAP, len(a)), replace=True).mean(axis=1)
             - RNG.choice(b, size=(BOOTSTRAP, len(b)), replace=True).mean(axis=1))
    return float(np.percentile(boots, 2.5)), float(np.percentile(boots, 97.5))


def main():
    today = time.time()
    price_start = time.strftime('%Y-%m-%d',
                                time.gmtime(today - wm.PRICE_DAYS * 86400))
    train_start = time.strftime(
        '%Y-%m-%d', time.gmtime(today - (wm.PRICE_DAYS + wm.TRAIN_DAYS) * 86400))
    train_end = time.strftime('%Y-%m-%d',
                              time.gmtime(today - (wm.PRICE_DAYS + 1) * 86400))
    print(f'разброс обучается {train_start} … {train_end}')
    print(f'ставки берутся с {price_start}\n')

    fitted = fitted_stations(train_start, train_end)
    bets = collect_bets(fitted, price_start, today)
    print(f'станций {len(fitted)}, ставок {len(bets)}\n')
    if not bets:
        print('ставок нет — замер не состоялся')
        return

    print('=' * 88)
    print('ПО ГОРОДАМ. Порядок по разбросу: слева самые предсказуемые станции.')
    print('=' * 88)
    print(f'{"город":<18}{"разброс":>9}{"ставок":>8}{"событий":>9}'
          f'{"R/ставку":>10}{"интервал по событиям":>26}')
    print('-' * 88)
    by_city = defaultdict(list)
    for b in bets:
        by_city[b['city']].append(b)
    for city in sorted(by_city, key=lambda c: fitted[c]['sigma']):
        rows = by_city[city]
        lo, hi, n_ev = event_ci(rows)
        mean = float(np.mean([r['net'] for r in rows]))
        span = f'[{lo:+.3f}; {hi:+.3f}]' if n_ev >= 2 else '—'
        print(f'{city[:16]:<18}{fitted[city]["sigma"]:>9.2f}{len(rows):>8}'
              f'{n_ev:>9}{mean:>+10.3f}{span:>26}')

    sigmas = sorted(f['sigma'] for f in fitted.values())
    cut = float(np.median(sigmas))
    low = [b for b in bets if b['sigma'] <= cut]
    high = [b for b in bets if b['sigma'] > cut]

    print()
    print('=' * 88)
    print(f'ДЕЛЕНИЕ ПО МЕДИАНЕ РАЗБРОСА ({cut:.2f}). Порог не подбирался.')
    print('=' * 88)
    print(f'{"группа":<22}{"ставок":>8}{"событий":>9}{"R/ставку":>10}'
          f'{"интервал по событиям":>26}')
    print('-' * 88)
    stats = {}
    for name, rows in (('малый разброс', low), ('большой разброс', high)):
        if not rows:
            print(f'{name:<22}{"— ставок нет":>8}')
            continue
        lo, hi, n_ev = event_ci(rows)
        mean = float(np.mean([r['net'] for r in rows]))
        stats[name] = {'mean': mean, 'lo': lo, 'events': n_ev, 'n': len(rows)}
        print(f'{name:<22}{len(rows):>8}{n_ev:>9}{mean:>+10.3f}'
              f'{f"[{lo:+.3f}; {hi:+.3f}]":>26}')

    print()
    print('=' * 88)
    print('ПРИЁМКА, ЗАПИСАННАЯ ДО ПРОГОНА: малый разброс положителен после')
    print('издержек, превосходит большой, интервал РАЗНИЦЫ не накрывает ноль')
    print(f'И в каждой половине не меньше {MIN_EVENTS} событий.')
    print('=' * 88)
    a, b = stats.get('малый разброс'), stats.get('большой разброс')
    if not a or not b:
        print('одна из половин пуста — замер не состоялся')
        return
    dlo, dhi = diff_ci(low, high)
    gap = a['mean'] - b['mean']
    print(f'  разница {gap:+.3f}, интервал [{dlo:+.3f}; {dhi:+.3f}]')
    print(f'  событий: малый {a["events"]}, большой {b["events"]}')
    print()
    thin = a['events'] < MIN_EVENTS or b['events'] < MIN_EVENTS
    if thin:
        print('МАЛО СОБЫТИЙ — замер не состоялся.')
    elif a['mean'] > 0 and gap > 0 and dlo > 0:
        print('ОТБОР ПОЛЕЗЕН. Разброс станции предсказывает качество ставки,')
        print('и ограничение списка городов — не вкусовщина, а рабочее правило.')
    else:
        print('ОТБОР НЕ ПОДТВЕРДИЛСЯ по записанному условию.')
        print('Разброс объясняет ШИРИНУ нашего распределения, но, судя по')
        print('этому замеру, не объясняет, где мы обыгрываем цену. Ограничивать')
        print('города по нему — значит терять ставки без доказанной выгоды.')


if __name__ == '__main__':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    main()
