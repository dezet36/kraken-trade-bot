"""
Односторонний мейкер: сжигает ли накопление больше, чем приносит тик?

КРИТЕРИИ ЗАПИСАНЫ ДО ЗАПУСКА, в ONESIDE_CRITERIA.md, и смягчению не подлежат.

Стратегия котирует только бид на дешёвых рынках: заявка на пять контрактов по
0.10 стоит $0.50, а не $5, поэтому сотня долларов покрывает двести рынков, а не
двадцать. Выход бесплатен — продаём то, чем владеем. Риск один: второе
исполнение не случается, контракт доживает до разрешения и становится нулём.

Меряем недобор `π - b`: реальную частоту исхода «да» минус цену покупки. Если
он отрицателен сильнее тика, накопление не окупится никаким числом кругов.

БУТСТРАП ПЕРЕСЭМПЛИРУЕТ СОБЫТИЯ, А НЕ РЫНКИ. Рынки одного события разрешаются
вместе; считая их независимыми, мы завысили бы точность в разы.
"""

import io
import json
import os
import random
import sys
from collections import defaultdict

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     'polymarket_cache')
BANDS = [(0.02, 0.05), (0.05, 0.10), (0.10, 0.15), (0.02, 0.15)]
TICK = 0.01
MIN_EVENTS = 30


def load_markets():
    with open(os.path.join(CACHE, 'markets.json'), encoding='utf-8') as fh:
        return json.load(fh)


def history(market_id):
    path = os.path.join(CACHE, f'h_{market_id}.json')
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding='utf-8') as fh:
            return json.load(fh)
    except Exception:                                          # noqa: BLE001
        return None


def observations(markets):
    """
    Каждое наблюдение цены — возможная покупка. Возвращает (событие, цена, исход).

    БЕРЁМ ВСЕ НАБЛЮДЕНИЯ, А НЕ ОДНО НА РЫНОК. Мейкер стоит в стакане постоянно
    и покупает тогда, когда его исполнят, — то есть в случайный по отношению к
    нему момент. Одно наблюдение на рынок отвечало бы на другой вопрос: «как
    разрешился рынок, стоивший столько-то в один выбранный день».
    """
    rows = []
    missing = 0
    for market in markets:
        won = market.get('yes_won')
        if won is None:
            continue
        series = history(market.get('id'))
        if not series:
            missing += 1
            continue
        points = series if isinstance(series, list) else series.get('history')
        if not points:
            missing += 1
            continue
        event = market.get('event') or market.get('id')
        for point in points:
            price = point.get('p') if isinstance(point, dict) else None
            if price is None:
                continue
            rows.append((event, float(price), 1.0 if won else 0.0))
    return rows, missing


def shortfall(rows):
    """Недобор: реальная частота «да» минус уплаченная цена, на контракт."""
    if not rows:
        return None
    return sum(won - price for _, price, won in rows) / len(rows)


def bootstrap(rows, draws=2000, seed=20260814):
    """
    Доверительный интервал по СОБЫТИЯМ.

    Пересэмплируем события целиком: внутри события рынки разрешаются вместе, и
    их независимость — иллюзия, завышающая точность.
    """
    by_event = defaultdict(list)
    for event, price, won in rows:
        by_event[event].append((event, price, won))
    events = list(by_event)
    if len(events) < 2:
        return None, None
    rng = random.Random(seed)
    out = []
    for _ in range(draws):
        pick = []
        for _ in range(len(events)):
            pick.extend(by_event[events[rng.randrange(len(events))]])
        value = shortfall(pick)
        if value is not None:
            out.append(value)
    if not out:
        return None, None
    out.sort()
    return out[int(len(out) * 0.025)], out[int(len(out) * 0.975)]


def main():
    markets = load_markets()
    rows, missing = observations(markets)
    print(f'разрешённых рынков в кэше: {len(markets)}')
    print(f'без пригодной истории цен: {missing}')
    print(f'наблюдений цены всего: {len(rows):,}\n')

    print(f'{"полоса":>12} {"событий":>8} {"наблюд":>9} {"частота":>9} '
          f'{"цена":>7} {"недобор":>9} {"интервал 95%":>22}')
    print('-' * 82)
    verdicts = {}
    for low, high in BANDS:
        band = [r for r in rows if low <= r[1] < high]
        events = len({r[0] for r in band})
        if not band:
            print(f'{low:.2f}-{high:.2f}   пусто')
            continue
        freq = sum(r[2] for r in band) / len(band)
        price = sum(r[1] for r in band) / len(band)
        gap = shortfall(band)
        lo, hi = bootstrap(band)
        span = f'[{lo:+.4f}, {hi:+.4f}]' if lo is not None else 'нет'
        print(f'{low:.2f}-{high:.2f} {events:>10} {len(band):>9,} '
              f'{freq:>8.2%} {price:>7.4f} {gap:>+9.4f} {span:>22}')
        verdicts[(low, high)] = (events, gap, lo, hi)

    print('\nПРИГОВОР ПО ЗАРАНЕЕ ЗАПИСАННЫМ КРИТЕРИЯМ\n')
    target = verdicts.get((0.02, 0.15))
    if not target:
        print('  целевая полоса пуста — решить нельзя')
        return
    events, gap, lo, hi = target
    print(f'  целевая полоса 0.02-0.15: событий {events}, '
          f'недобор {gap:+.4f}, интервал [{lo:+.4f}, {hi:+.4f}]')
    if events < MIN_EVENTS:
        print(f'\n  РЕШИТЬ НЕЛЬЗЯ: событий {events} < {MIN_EVENTS}.')
        print('  Малую выборку не спасает никакой интервал.')
        return
    if hi is not None and hi < -TICK:
        print(f'\n  ОТКЛОНЕНО: верхняя граница {hi:+.4f} ниже -{TICK}.')
        print('  Накопление сжигает больше тика — не окупится никаким числом кругов.')
    elif lo is not None and lo >= 0:
        print(f'\n  ГОДНА: нижняя граница {lo:+.4f} не ниже нуля.')
        print('  Накопление безобидно; всё решает частота исполнений.')
    else:
        need = abs(gap) / TICK if gap < 0 else 0.0
        print(f'\n  ПРОМЕЖУТОЧНЫЙ СЛУЧАЙ: интервал накрывает ноль.')
        print(f'  Решение переносится на бумажный замер.')
        if gap < 0:
            print(f'  Нужно, чтобы доля исполненных выходов превышала {need:.1%}')
            print(f'  (тик {TICK} должен покрывать недобор {gap:+.4f}).')
        else:
            print(f'  Точечная оценка недобора положительна ({gap:+.4f}):')
            print(f'  накопление скорее помогает, но интервал этого не доказывает.')


if __name__ == '__main__':
    main()
