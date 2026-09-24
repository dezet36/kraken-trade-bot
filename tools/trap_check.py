# -*- coding: utf-8 -*-
"""
Слом 1ч против целой 4ч — ловушка ликвидности или начало разворота?

ВОПРОС. Цена ломает структуру на часе вниз, а на 4ч тренд ещё бычий.
Народное правило говорит: это снятие ликвидности, цена вернётся. Если
правило верно, стоп надо ставить за слом 4ч, а не 1ч. Если нет — расширять
стопы значит платить за красивую историю.

КАК СЧИТАЕМ. Берём структуру теми же функциями, что и бот (`smc.structure`),
идём по часовым свечам и ловим КАЖДОЕ событие слома. В момент события
смотрим направление структуры на 4ч:

    СОГЛАСНА    — 4ч смотрит туда же, куда сломался час;
    ПРОТИВ      — 4ч смотрит в другую сторону (та самая ловушка).

Дальше меряем, что цена сделала за следующие HORIZON часов:

    ВОЗВРАТ     — закрытие часа вернулось за уровень слома обратно
                  (для слома вниз: закрытие выше уровня);
    ПРОДОЛЖЕНИЕ — ушла на CONT_ATR дневных размахов дальше, не вернувшись.

Если у «ПРОТИВ» доля возвратов заметно выше, чем у «СОГЛАСНА», — правило
подтверждено и стоп для плана в сторону 4ч должен стоять за слом 4ч.
"""
import sys, os, collections

sys.path.insert(0, '/opt/kraken/code/Live_Bot')
os.chdir('/opt/kraken/code/Live_Bot')

import numpy as np
import exchange
from smc import structure as S
import smc.params as P

HORIZON = 12          # часов наблюдения после слома
CONT_PCT = 1.0        # «ушла дальше» — на столько процентов за уровень
PAIRS = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'XRPUSDT', 'ADAUSDT', 'DOGEUSDT',
         'LTCUSDT', 'LINKUSDT', 'DOTUSDT', 'AVAXUSDT', 'UNIUSDT', 'NEARUSDT',
         'ARBUSDT', 'SUIUSDT', 'XLMUSDT', 'AAVEUSDT', 'BNBUSDT', 'ZECUSDT']


def build(df):
    return S.build_structure(df, tier='swing')


def main():
    cl = exchange.get_exchange()
    tally = collections.defaultdict(lambda: collections.Counter())
    examples = collections.defaultdict(list)

    for pair in PAIRS:
        try:
            h1 = exchange.fetch_ohlcv('1h', limit=500, symbol=pair, client=cl)
            h4 = exchange.fetch_ohlcv('4h', limit=500, symbol=pair, client=cl)
        except Exception as exc:
            print('  %s — свечи не получены (%s)' % (pair, str(exc)[:40]))
            continue
        try:
            st1 = build(h1)
            st4 = build(h4)
        except Exception as exc:
            print('  %s — структура не построена (%s)' % (pair, str(exc)[:60]))
            continue
        if not st1 or not st4:
            print('  %s — структура пуста' % pair)
            continue

        t1 = np.asarray(h1['timestamp'].values).astype('datetime64[ms]').astype('int64')
        t4 = np.asarray(h4['timestamp'].values).astype('datetime64[ms]').astype('int64')
        close = h1['close'].values.astype(float)
        high = h1['high'].values.astype(float)
        low = h1['low'].values.astype(float)

        for ev in st1['events']:
            i = int(ev['index'])
            if i < 30 or i + HORIZON >= len(h1):
                continue
            lvl = ev.get('price') or ev.get('level')
            if not lvl:
                continue
            lvl = float(lvl)
            direction = ev['direction']                  # куда сломался час

            j = int(np.searchsorted(t4, t1[i], side='right')) - 1
            if j < 5:
                continue
            s4 = S.state_at(st4, j)
            d4 = s4.get('trend')
            if d4 not in (S.BULLISH, S.BEARISH):
                continue

            agree = 'СОГЛАСНА' if d4 == direction else 'ПРОТИВ'

            w = slice(i + 1, i + 1 + HORIZON)
            if direction == S.BEARISH:
                back = bool(np.any(close[w] > lvl))
                far = bool(np.any(low[w] < lvl * (1 - CONT_PCT / 100)))
            else:
                back = bool(np.any(close[w] < lvl))
                far = bool(np.any(high[w] > lvl * (1 + CONT_PCT / 100)))

            key = agree
            tally[key]['всего'] += 1
            if back:
                tally[key]['возврат'] += 1
            if far and not back:
                tally[key]['продолжение'] += 1
            if len(examples[key]) < 2:
                examples[key].append('%s %s %s уровень %.6g' % (
                    pair, str(h1['timestamp'].iloc[i])[:16], ev.get('type', ''), lvl))

    print()
    print('%-10s %7s %9s %13s' % ('4ч', 'сломов', 'возврат', 'продолжение'))
    for key in ('СОГЛАСНА', 'ПРОТИВ'):
        t = tally[key]
        n = t['всего']
        if not n:
            print('%-10s %7s' % (key, '—'))
            continue
        print('%-10s %7d %8.0f%% %12.0f%%' % (
            key, n, t['возврат'] / n * 100, t['продолжение'] / n * 100))
    print()
    print('окно наблюдения %d ч; «продолжение» = ушла на %.1f%% за уровень и не вернулась'
          % (HORIZON, CONT_PCT))
    n_a, n_p = tally['СОГЛАСНА']['всего'], tally['ПРОТИВ']['всего']
    if n_a and n_p:
        ra = tally['СОГЛАСНА']['возврат'] / n_a * 100
        rp = tally['ПРОТИВ']['возврат'] / n_p * 100
        print()
        print('РАЗНИЦА В ДОЛЕ ВОЗВРАТОВ: %+.0f п.п. (ПРОТИВ %.0f%% против СОГЛАСНА %.0f%%)'
              % (rp - ra, rp, ra))
        if abs(rp - ra) < 10:
            print('Меньше десяти пунктов — правило «слом против 4ч это ловушка» НЕ подтверждается.')
        else:
            print('Разница заметна — правило работает в ту сторону, которую показывает знак.')


main()
