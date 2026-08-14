"""
Доля покупок, находящих выход. Единственное, чего не хватало для приговора.

ЗАЧЕМ. Замер недобора уже сделан: накопление в полосе 0.02-0.15 безобидно
(+0.0191 на контракт, интервал [-0.0034, +0.0446]). Осталась доля выходов `f`,
и от неё зависит всё:

    0.01·f - 0.0034·(1-f) > 0   при   f > 25%   (худший конец интервала)

ПОЧЕМУ НЕ ЖДЁМ ПРОГОНА. Бумага даст ответ за сутки, лента — за минуту, и на
куда большей выборке. Прогон всё равно нужен как проверка, но начинать с него
значило бы ждать сутки ради числа, которое уже лежит в ленте.

КАК СЧИТАЕМ. Для каждого рынка берём НАСТОЯЩИЙ стакан, ставим себя на тик
внутрь и считаем по ленте за сутки:

    вход  — продажи по цене не выше нашего бида
    выход — покупки по цене не ниже нашего аска (вход плюс тик)

Отношение и есть доля: если выходов меньше входов, часть покупок остаётся
висеть до разрешения.

ЧЕГО ЭТОТ ЗАМЕР НЕ УМЕЕТ. Он прикладывает СЕГОДНЯШНИЙ стакан к ВЧЕРАШНИМ
сделкам. Стакан двигается, поэтому число приблизительное. Оно не заменяет
прогон, а говорит, стоит ли его вообще ждать.
"""

import io
import os
import statistics as S
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), 'Live_Bot'))

from polymarket import book as B, oneside as O   # noqa: E402

DAY = 24 * 3600


def measure(market, trades, top):
    """Входы и выходы за сутки для одного рынка."""
    tick = float(market['tick'])
    entry = round(top['bid'] + tick, 10)
    exit_ = round(entry + tick, 10)
    now = time.time()
    mine = [t for t in trades
            if t.get('asset') == market['token_id'] and now - t['ts'] < DAY]
    if not mine:
        return None
    span = max((max(t['ts'] for t in mine) - min(t['ts'] for t in mine)) / 3600,
               0.5)
    ins = [t for t in mine if t['side'] == 'SELL' and t['price'] <= entry + 1e-9]
    outs = [t for t in mine if t['side'] == 'BUY' and t['price'] >= exit_ - 1e-9]
    return {
        'question': market['question'], 'price': top['mid'],
        'entry': entry, 'exit': exit_, 'hours': span,
        'in_per_h': len(ins) / span, 'out_per_h': len(outs) / span,
        'in_size': sum(t['size'] for t in ins),
        'out_size': sum(t['size'] for t in outs),
    }


def main():
    rows = O.scan(budget=100, limit=120)
    markets = O.plan(rows, budget=100)['markets']
    print(f'рынков в плане: {len(markets)}\n')
    got = []
    for market in markets[:60]:
        trades = B.tape(market['condition_id'], limit=500)
        if not trades:
            continue
        top = B.top(B.fetch_many([market['token_id']]).get(
            str(market['token_id'])) or {})
        if not top or top.get('bid') is None:
            continue
        row = measure(market, trades, top)
        if row:
            got.append(row)

    if not got:
        print('ленты не хватило')
        return

    print(f'{"вход/ч":>8} {"выход/ч":>9} {"доля":>7}  рынок')
    print('-' * 72)
    shares = []
    for row in sorted(got, key=lambda r: -r['in_per_h'])[:18]:
        share = min(1.0, row['out_per_h'] / row['in_per_h']) \
            if row['in_per_h'] > 0 else None
        mark = f'{share:>6.0%}' if share is not None else '   нет'
        print(f'{row["in_per_h"]:>8.2f} {row["out_per_h"]:>9.2f} {mark:>7}  '
              f'{row["question"][:42]}')
    for row in got:
        if row['in_per_h'] > 0:
            shares.append(min(1.0, row['out_per_h'] / row['in_per_h']))

    total_in = sum(r['in_per_h'] for r in got)
    total_out = sum(r['out_per_h'] for r in got)
    print(f'\nрынков с лентой: {len(got)}')
    print(f'входов всего: {total_in:.1f}/час = {total_in * 24:.0f} в сутки')
    print(f'выходов всего: {total_out:.1f}/час = {total_out * 24:.0f} в сутки')
    if shares:
        print(f'\nДОЛЯ ВЫХОДОВ: медиана {S.median(shares):.0%}, '
              f'среднее {S.mean(shares):.0%}')
        print(f'  рынков, где выходов не меньше четверти входов: '
              f'{sum(1 for s in shares if s >= 0.25)}/{len(shares)} '
              f'= {sum(1 for s in shares if s >= 0.25) / len(shares):.0%}')
        med = S.median(shares)
        edge = 0.01 * med - 0.0034 * (1 - med)
        print(f'\nПРИГОВОР при медианной доле {med:.0%}:')
        print(f'  доход на контракт = 0.01·{med:.2f} - 0.0034·{1 - med:.2f} '
              f'= {edge:+.5f}')
        print(f'  {"ПОЛОЖИТЕЛЕН" if edge > 0 else "ОТРИЦАТЕЛЕН"} '
              f'на худшем конце интервала недобора')
        best = 0.01 * med + 0.0191 * (1 - med)
        print(f'  при ТОЧЕЧНОЙ оценке недобора (+0.0191): {best:+.5f}')


if __name__ == '__main__':
    main()
