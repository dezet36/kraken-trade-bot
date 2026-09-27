"""
H9 (research/smc_lab_study.py): вход в SMC с подтверждением на 15 минутах.

Сейчас SMC ставит лимит на ближний край ордер-блока и наливается при
касании — в том числе когда цена прошивает блок насквозь и уходит в стоп.
Классический вход smart money — дождаться реакции: цена дошла до блока, и
15-минутная свеча закрылась обратно за его ближним краем. Вход по рынку на
закрытии этой свечи (тейкер), стоп прежний — за дальним краем блока, цели
прежние (Фибо-расширения ноги).

Правила (записаны до замера):
    1. После постановки (срок 48 ч) ждём касания ближнего края.
    2. После касания смотрим 15-минутные свечи: если минимум дошёл до стопа
       (для лонга) раньше подтверждения — сетап сломан, сделки нет; если свеча
       закрылась за ближним краем — подтверждение (та же свеча касания тоже
       годится: прокол фитилём и закрытие обратно).
    3. Подтверждение ждём не дольше 12 часов после касания.
    4. Вход по рынку — по открытию 5-минутной свечи после подтверждения,
       комиссия тейкера; предел издержек брокера считается от этой цены.
Сравнение — те же зоны с обычным лимитом (без смещения и с живым 0.1%):
каждая заявка отдельно и в портфеле с живыми правилами.

Запуск (после research/smc_lab.py gen):
    python research/smc_lab_confirm.py
"""
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import smc_lab_eval as E                              # noqa: E402
from common import ci                                 # noqa: E402
from smc_engine import Order, _prepare               # noqa: E402

CONFIRM_HOURS = 12.0
BAR15 = np.timedelta64(15, 'm')


def confirmed(order, arr, cost_limit):
    """Заявка со входом по подтверждению или None."""
    long_ = order.direction == 'BULLISH'
    near = order.meta['row'].entry_near
    stop = order.stop
    ts, low, high, close = arr['ts'], arr['low'], arr['high'], arr['close']
    start = int(np.searchsorted(ts, np.datetime64(order.created), side='right'))
    end = int(np.searchsorted(ts, np.datetime64(order.expires), side='right'))
    touch = None
    for k in range(start, min(end, len(ts))):
        if (low[k] <= near) if long_ else (high[k] >= near):
            touch = k
            break
    if touch is None:
        return None
    # 15-минутные окна от окна, в котором случилось касание.
    t0 = ts[touch].astype('datetime64[m]')
    window_start = t0 - (t0.astype('int64') % 15).astype('timedelta64[m]')
    deadline = ts[touch] + np.timedelta64(int(CONFIRM_HOURS * 60), 'm')
    k = touch
    while k < len(ts) and ts[k] <= deadline:
        w_end = window_start + BAR15
        lo_w, hi_w, last = np.inf, -np.inf, None
        j = k
        while j < len(ts) and ts[j] < w_end:
            lo_w, hi_w, last = min(lo_w, low[j]), max(hi_w, high[j]), j
            j += 1
        if last is None:
            window_start, k = w_end, j
            continue
        if (lo_w <= stop) if long_ else (hi_w >= stop):
            return None                                # до подтверждения цена дошла до стопа
        c = close[last]
        if (c > near) if long_ else (c < near):
            # Вход ПО РЫНКУ — по открытию следующей 5-минутной свечи. Стоп-заявка
            # на цене закрытия была бы нечестной: она не наливается, если цена
            # сразу пошла против сделки, а рыночный вход налился бы и проиграл.
            nxt = last + 1
            if nxt >= len(ts):
                return None
            entry = float(arr['open'][nxt])
            if abs(entry - stop) <= 0 or ((entry <= stop) if long_ else (entry >= stop)):
                return None
            share = entry / abs(entry - stop) * E.ROUND_TRIP * 100
            if cost_limit and share > cost_limit:
                return None
            tg = [t for t in order.targets if (t > entry if long_ else t < entry)]
            if not tg:
                return None
            fr = list(order.fractions[-len(tg):])
            fr[-1] += 1.0 - sum(fr)
            # Движок начинает со свечи, открытой ПОЗЖЕ created, — ставим created на
            # микросекунду раньше открытия свечи nxt, и она становится свечой налива.
            # Стоп-заявка на цене открытия этой свечи наливается на ней же по этой
            # цене с комиссией тейкера — это и есть рыночный вход.
            created = ts[nxt] - np.timedelta64(1, 'us')
            return Order(pair=order.pair, direction=order.direction, entry=entry, stop=stop, targets=tg,
                         fractions=fr, created=created, expires=created + np.timedelta64(1, 'h'),
                         key=order.key, meta=dict(order.meta, confirm_share=share), entry_type='stop')
        window_start, k = w_end, j
    return None


def main():
    spec_zone = dict(E.LIVE, offset=0.0, cost_limit=0.0)          # зоны — по живым порогам сигнала
    spec_live = dict(E.LIVE)
    totals = {'лимит, как сейчас (смещение 0.1%)': [], 'подтверждение 15м': []}
    per_setup = {'лимит без смещения': [], 'лимит, как сейчас (смещение 0.1%)': [], 'подтверждение 15м': []}
    print('Вход с подтверждением на 15 минутах против лимита на краю блока (живые пороги, 10 пар)')
    for period in E.PERIODS:
        zones = E.orders_for(period, spec_zone)
        data = E.exec_data(period, spec_zone['pairs'])
        prepared = {p: _prepare(df) for p, df in data.items()}
        conf = [c for o in zones if (c := confirmed(o, prepared[o.pair], spec_live['cost_limit'])) is not None]
        # каждая заявка отдельно
        for name, orders in (('лимит без смещения', zones),
                             ('лимит, как сейчас (смещение 0.1%)', E.orders_for(period, spec_live)),
                             ('подтверждение 15м', conf)):
            rs = [r for _, r in E.per_setup(period, spec_live, orders) if r is not None]
            per_setup[name].append((period, len(orders), rs))
        # портфель
        line = []
        for name, orders in (('лимит, как сейчас (смещение 0.1%)', None), ('подтверждение 15м', conf)):
            res, _ = E.portfolio(period, spec_live, orders)
            rs = [t['pnl'] / t['risk'] for t in res['trades']]
            totals[name].append((period, rs))
            line.append(f'{name}: {len(rs)} сд {np.sum(rs):+.1f}R')
        print(f'  {period:6s} зон {len(zones):4d}, подтвердилось {len(conf):4d} | ' + ' | '.join(line), flush=True)
    print('\nКАЖДАЯ ЗАЯВКА ОТДЕЛЬНО (R на сделку, по периодам)')
    for name, items in per_setup.items():
        allr = np.concatenate([np.array(rs) for _, _, rs in items if rs])
        lo, hi = ci(allr)
        print(f'  {name:36s} {len(allr):5d} сд {allr.mean():+.3f} [{lo:+.3f}; {hi:+.3f}]  '
              + ' '.join(f'{p} {np.mean(rs) if rs else 0:+.3f}({len(rs)})' for p, _, rs in items))
    print('\nПОРТФЕЛЬ С ЖИВЫМИ ПРАВИЛАМИ')
    for name, items in totals.items():
        allr = np.concatenate([np.array(rs) for _, rs in items if rs])
        lo, hi = ci(allr)
        print(f'  {name:36s} {len(allr):5d} сд {allr.sum():+8.1f}R {allr.mean():+.3f} [{lo:+.3f}; {hi:+.3f}]  '
              + ' '.join(f'{p} {np.sum(rs):+.1f}' for p, rs in items))


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
