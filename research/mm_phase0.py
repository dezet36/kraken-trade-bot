"""
Фаза 0: что показало наблюдение за стаканом.

ЧТО ЗДЕСЬ СЧИТАЕТСЯ И ПОЧЕМУ ИМЕННО ЭТО. Маркет-мейкинг нельзя проверить на
исторических ценах: он живёт вероятностью исполнения и неблагоприятным отбором,
а в истории цен нет ни очереди заявок, ни того, кто кого снял. Наблюдатель
(Live_Bot/polymarket/observer.py) добывает обе величины единственным возможным
способом — котирует понарошку поверх живого стакана и записывает, что было
дальше. Здесь записанное превращается в числа.

ТРИ ВОПРОСА, И ТОЛЬКО ОНИ

    1. Как часто нас исполняло бы. Без этого доходность не посчитать вовсе:
       захваченный спред умножается на число исполнений.
    2. Сколько стоит неблагоприятный отбор. Нас снимают тогда, когда встречной
       стороне это выгодно. Разница между ценой исполнения и серединой рынка
       ПОСЛЕ него — прямая мера этой платы.
    3. Остаётся ли что-то после. Спред минус отбор минус доля в награде — это и
       есть ответ, работает ли схема.

ПРИЁМКА, ЗАПИСАННАЯ ДО НАКОПЛЕНИЯ ДАННЫХ:

    схема считается пригодной для Фазы 2, если захваченный спред превышает
    измеренный неблагоприятный отбор, интервал разницы по РЫНКАМ не накрывает
    ноль И исполнений не меньше 200.

Интервал по рынкам, а не по заявкам: заявки одного рынка исполняются одними и
теми же событиями, и считать их независимыми — тот же самый обман, что считать
одиннадцать температурных корзин одного дня одиннадцатью наблюдениями.

ЧЕГО ЭТОТ ЗАМЕР НЕ ПОКАЖЕТ. Он не скажет, сколько мы заработаем: доля в награде
зависит от того, сколько ещё маркет-мейкеров придёт в тот же стакан, а этого не
предскажешь. И он не заменяет проверку живыми деньгами — модель очереди
пессимистична, но всё же модель.

Запуск:
    python research/mm_phase0.py
"""

import io
import json
import os
import sys
from collections import defaultdict

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'Live_Bot'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

MIN_FILLS = 200
BOOTSTRAP = 10_000
RNG = np.random.default_rng(20260813)


def load(path):
    if not os.path.exists(path):
        return []
    rows = []
    with open(path, encoding='utf-8') as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def market_ci(values_by_market):
    """Интервал, где пересэмплируются РЫНКИ целиком."""
    keys = list(values_by_market)
    if len(keys) < 2:
        return float('nan'), float('nan')
    per = np.array([np.mean(values_by_market[k]) for k in keys], dtype=float)
    boots = RNG.choice(per, size=(BOOTSTRAP, len(per)), replace=True).mean(axis=1)
    return float(np.percentile(boots, 2.5)), float(np.percentile(boots, 97.5))


def main():
    from polymarket import observer

    obs = load(observer.OBSERVATIONS)
    fills = load(observer.QUOTES)
    print(f'снимков стакана: {len(obs)}')
    print(f'разобранных заявок: {len(fills)}')
    if not obs:
        print('\nданных нет — наблюдатель ещё не работал')
        return

    quoted = [r for r in obs if r.get('our_bid') is not None]
    markets = {r['market'] for r in quoted}
    print(f'рынков под наблюдением: {len(markets)}')
    span = (max(r['ts'] for r in quoted) - min(r['ts'] for r in quoted)) / 3600
    print(f'окно наблюдения: {span:.1f} часов\n')

    # ── Состояние стакана ───────────────────────────────────────────────────
    print('=' * 84)
    print('СТАКАН: где вообще можно стоять')
    print('=' * 84)
    spreads = np.array([r['spread'] for r in quoted], dtype=float)
    ticks = np.array([r['tick'] for r in quoted], dtype=float)
    in_ticks = spreads / ticks
    first = [r for r in quoted if (r.get('queue_bid') or 0) == 0]
    reward_ok = [r for r in quoted if r.get('reward_ok')]
    print(f'спред: медиана {np.median(spreads):.4f}, '
          f'в тиках медиана {np.median(in_ticks):.1f}')
    print(f'снимков, где мы ПЕРВЫЕ в очереди: {len(first)} из {len(quoted)} '
          f'({len(first) / len(quoted) * 100:.0f}%)')
    print(f'снимков, проходящих под награду:  {len(reward_ok)} из {len(quoted)} '
          f'({len(reward_ok) / len(quoted) * 100:.0f}%)')

    if not fills:
        print()
        print('=' * 84)
        print('ИСПОЛНЕНИЙ ПОКА НЕТ — замер не состоялся.')
        print('=' * 84)
        print('Это ожидаемо на первых часах: заявка разбирается через пять')
        print('минут после выставления, а на наблюдаемых рынках лента даёт')
        print('порядка двух сделок в час. Нужно копить.')
        return

    # ── Исполнения и отбор ──────────────────────────────────────────────────
    print()
    print('=' * 84)
    print('ИСПОЛНЕНИЯ И НЕБЛАГОПРИЯТНЫЙ ОТБОР')
    print('=' * 84)
    by_side = defaultdict(list)
    for f in fills:
        by_side[f['side']].append(f)
    for side, rows in sorted(by_side.items()):
        secs = [r['seconds_to_fill'] for r in rows if r.get('seconds_to_fill')]
        print(f'{side}: {len(rows)} исполнений, до исполнения медиана '
              f'{np.median(secs) / 60:.1f} мин' if secs else f'{side}: {len(rows)}')

    drifts = [f for f in fills if f.get('drift') is not None]
    if drifts:
        by_market = defaultdict(list)
        for f in drifts:
            by_market[f['market']].append(f['drift'])
        values = np.array([f['drift'] for f in drifts], dtype=float)
        lo, hi = market_ci(by_market)
        print(f'\nсдвиг середины ПОСЛЕ нашего исполнения, в сторону позиции:')
        print(f'   медиана {np.median(values):+.4f}, среднее {values.mean():+.4f}')
        print(f'   интервал по рынкам [{lo:+.4f}; {hi:+.4f}]')
        print('   отрицательное значение = нас снимали те, кто знал больше')

        # Спред, который мы захватываем, против платы за отбор.
        half = np.array([f['spread_at_quote'] / 2 for f in drifts
                         if f.get('spread_at_quote')], dtype=float)
        if len(half):
            print(f'\nполовина спреда при выставлении: медиана {np.median(half):+.4f}')
            net = values[:len(half)] + half if len(values) == len(half) else None
            if net is not None:
                by_market_net = defaultdict(list)
                for f, h in zip(drifts, half):
                    by_market_net[f['market']].append(f['drift'] + h)
                nlo, nhi = market_ci(by_market_net)
                print(f'ИТОГ на исполнение (спред минус отбор): '
                      f'{np.mean(net):+.4f}  интервал [{nlo:+.4f}; {nhi:+.4f}]')

    print()
    print('=' * 84)
    print('ПРИЁМКА: спред превышает отбор, интервал разницы по рынкам не')
    print(f'накрывает ноль И не меньше {MIN_FILLS} исполнений.')
    print('=' * 84)
    if len(fills) < MIN_FILLS:
        print(f'исполнений {len(fills)} — меньше {MIN_FILLS}. ЗАМЕР НЕ СОСТОЯЛСЯ,')
        print('нужно копить дальше. Любой вывод сейчас был бы гаданием.')
    else:
        print('данных достаточно, читать таблицы выше')


if __name__ == '__main__':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    main()
