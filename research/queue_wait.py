"""
Сколько ждать в очереди — вот что решает, а не её длина.

СОБСТВЕННАЯ ОШИБКА, КОТОРУЮ ЭТОТ ЗАМЕР ПРОВЕРЯЕТ. Рынки со спредом в один тик
были отброшены на том основании, что впереди нас стоит 152 контракта по медиане
и заявка не исполнится. Длина очереди мерилась на МЕДЛЕННЫХ рынках и была
перенесена на все.

Но очередь важна не длиной, а временем: сто контрактов впереди при потоке в
тысячу контрактов в час — это шесть минут, а не «никогда». На быстрых рынках
спред узок именно потому, что там торгуют, и там же очередь рассасывается.

    ожидание = очередь / поток

ПОЧЕМУ ЭТО МОЖЕТ РЕШИТЬ ВСЮ ЗАДАЧУ. Доход мейкера равен спреду, умноженному на
число кругов. Круг на однотиковом рынке даёт всего тик, зато кругов там может
быть на порядок больше. Разобранный кошелёк делает 1 495 сделок в сутки, и
таких рынков в наших отборах не было ни одного — потому что мы их отбрасывали.

ЧТО СЧИТАЕМ. Для каждого рынка: глубина на лучшей цене (наша очередь, если
встать рядом), поток встречных сделок по этой цене за сутки, и частное — время
ожидания. Плюс доход за круг: спред на размер.
"""

import io
import os
import statistics as S
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), 'Live_Bot'))

from polymarket import book as B, client, params, selector   # noqa: E402

DAY = 24 * 3600


def depth_at(book, side, price):
    """Контрактов на этой самой цене — наша очередь, если встать рядом."""
    levels = book['bids'] if side == 'bid' else book['asks']
    return sum(size for lvl, size in levels if abs(lvl - price) < 1e-9)


def main():
    rows = selector._candidates(20, 2000, 0.05, 0.95)
    print(f'кандидатов: {len(rows)}')
    books = B.fetch_many([r['token_id'] for r in rows])
    now = time.time()
    got = []
    for row in rows:
        live = books.get(str(row['token_id']))
        if not live or not live['bids'] or not live['asks']:
            continue
        top = B.top(live)
        if not top or top['bid'] is None:
            continue
        got.append((row, top, live))
    # Ленту берём только у тех, где книга есть: запрос на каждый рынок дорог.
    got.sort(key=lambda x: -float(x[0]['volume']))
    out = []
    for row, top, live in got[:70]:
        trades = B.tape(row['condition_id'], limit=500) or []
        mine = [t for t in trades
                if t.get('asset') == row['token_id'] and now - t['ts'] < DAY]
        if len(mine) < 5:
            continue
        span = max((max(t['ts'] for t in mine)
                    - min(t['ts'] for t in mine)) / 3600, 0.5)
        # Встаём РЯДОМ с лучшей ценой, в конец её очереди.
        sells = [t for t in mine
                 if t['side'] == 'SELL' and t['price'] <= top['bid'] + 1e-9]
        flow = sum(t['size'] for t in sells) / span
        queue = depth_at(live, 'bid', top['bid'])
        wait = queue / flow if flow > 0 else float('inf')
        ticks = round(top['spread'] / row['tick'])
        out.append({
            'q': row['question'], 'price': top['mid'], 'ticks': ticks,
            'tick': row['tick'], 'queue': queue, 'flow': flow, 'wait': wait,
            'trades_h': len(mine) / span, 'spread': top['spread'],
        })

    if not out:
        print('данных не хватило')
        return
    out.sort(key=lambda r: r['wait'])
    print(f'\nрынков с лентой: {len(out)}\n')
    print(f'{"ждать":>8} {"очередь":>9} {"поток/ч":>9} {"тик":>4} '
          f'{"сд/ч":>6} {"цена":>6}  рынок')
    print('-' * 84)
    for r in out[:22]:
        wait = f'{r["wait"] * 60:.0f} мин' if r['wait'] < 24 else 'сутки+'
        print(f'{wait:>8} {r["queue"]:>9,.0f} {r["flow"]:>9,.0f} '
              f'{r["ticks"]:>4} {r["trades_h"]:>6.1f} {r["price"]:>6.3f}  '
              f'{r["q"][:36]}')

    quick = [r for r in out if r['wait'] < 1]
    print(f'\nрынков с ожиданием меньше часа: {len(quick)}/{len(out)}')
    if quick:
        print(f'  медианное ожидание: {S.median([r["wait"] for r in quick]) * 60:.0f} мин')
        print(f'  медианный спред: {S.median([r["ticks"] for r in quick]):.0f} тик(а)')
        # Круг = вход и выход. Ждать приходится дважды.
        circles = sum(1 / (2 * r['wait']) for r in quick if r['wait'] > 0)
        print(f'\n  КРУГОВ В ЧАС (вход плюс выход = два ожидания): {circles:.1f}')
        print(f'  = {circles * 24:.0f} в сутки')
        # Размер ограничим долей потока, как в односторонней схеме.
        gain = sum((1 / (2 * r['wait'])) * min(r['flow'] * 0.5, 100) * r['spread']
                   for r in quick if r['wait'] > 0)
        cost = sum(min(r['flow'] * 0.5, 100) * 1.0 for r in quick)
        print(f'\n  ПОТОЛОК ДОХОДА: ${gain:.2f}/час при вложенных ${cost:,.0f}')
        if cost > 0:
            print(f'  на $100 это ${gain / cost * 100 * 24:.2f} в сутки')


if __name__ == '__main__':
    main()
