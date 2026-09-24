# -*- coding: utf-8 -*-
"""
Каким должен быть буфер стопа за уровнем инвалидации.

ВОПРОС. Стоп ИИ стоит за уровнем слома структуры (последний подтверждённый
HL в бычьей, LH в медвежьей), отступив на буфер `stop_hunt_pct` — сейчас
0.33% при дневном размахе 0.5%. DOGE 23.09.2026 показал, чем это кончается:
ни одна свеча не закрылась ниже уровня (идея жива по определению самой
модели), а фитиль ушёл на 0.55% и снял стоп. Минус полный R на верном
сетапе.

ЧТО СЧИТАЕМ. Идём по часовым свечам. На каждой берём уровень инвалидации
той стороны, куда смотрит структура, и смотрим следующие HORIZON часов:

    ИДЕЯ ЖИВА  — ни одного ЗАКРЫТИЯ за уровнем (структура ломается по
                 закрытию, BREAK_ON_CLOSE — так считает наш же код);
    ПРОКОЛ     — при этом фитиль за уровень заходил, на столько-то процентов.

Распределение проколов ПРИ ЖИВОЙ ИДЕЕ и есть ответ: буфер обязан быть шире
прокола, иначе нас выбивает из сделки, которая не отменялась.

ОГОВОРКА. Это замер рынка, а не наших сделок: он не знает, где был бы вход
и прошёл бы план ворота. Он отвечает ровно на один вопрос — какой отступ
переживает шум вокруг уровня инвалидации.
"""
import sys, os, collections, statistics

sys.path.insert(0, '/opt/kraken/code/Live_Bot')
os.chdir('/opt/kraken/code/Live_Bot')

import numpy as np
import exchange
from smc import structure as S
import llm_decide as D

HORIZON = 12
STEP = 3                # берём каждую третью свечу: соседние почти одинаковы
PAIRS = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'XRPUSDT', 'ADAUSDT', 'DOGEUSDT',
         'LTCUSDT', 'LINKUSDT', 'DOTUSDT', 'AVAXUSDT', 'UNIUSDT', 'NEARUSDT',
         'ARBUSDT', 'SUIUSDT', 'XLMUSDT', 'AAVEUSDT', 'BNBUSDT', 'ZECUSDT']


def main():
    cl = exchange.get_exchange()
    pokes = []            # проколы при живой идее, %
    alive = broken = 0

    for pair in PAIRS:
        try:
            df = exchange.fetch_ohlcv('1h', limit=500, symbol=pair, client=cl)
            st = S.build_structure(df, tier='swing')
        except Exception:
            continue
        close = df['close'].values.astype(float)
        high = df['high'].values.astype(float)
        low = df['low'].values.astype(float)

        for i in range(40, len(df) - HORIZON, STEP):
            state = S.state_at(st, i)
            trend = state.get('trend')
            if trend not in (S.BULLISH, S.BEARISH):
                continue
            found = S.invalidation_level(st, i, trend)
            if not found or not found.get('price'):
                continue
            L = float(found['price'])
            w = slice(i + 1, i + 1 + HORIZON)

            if trend == S.BULLISH:
                closed_through = bool(np.any(close[w] < L))
                poke = (L - float(np.min(low[w]))) / L * 100
            else:
                closed_through = bool(np.any(close[w] > L))
                poke = (float(np.max(high[w])) - L) / L * 100

            if closed_through:
                broken += 1
                continue
            alive += 1
            if poke > 0:
                pokes.append(poke)

    print()
    print('наблюдений: идея жива %d, структура сломана закрытием %d' % (alive, broken))
    if not pokes:
        print('проколов не найдено')
        return
    pokes.sort()
    q = lambda p: pokes[min(int(len(pokes) * p), len(pokes) - 1)]
    print('проколов при живой идее: %d (%.0f%% живых случаев)'
          % (len(pokes), len(pokes) / alive * 100))
    print()
    print('  ГЛУБИНА ПРОКОЛА ПРИ ЖИВОЙ ИДЕЕ')
    for name, p in (('медиана', 0.50), ('70-й проц.', 0.70),
                    ('80-й проц.', 0.80), ('90-й проц.', 0.90),
                    ('95-й проц.', 0.95)):
        print('    %-12s %.2f%%' % (name, q(p)))
    print()
    print('  СКОЛЬКО ЖИВЫХ ИДЕЙ ВЫБИВАЕТ БУФЕР')
    for b in (0.33, 0.5, 0.75, 1.0, 1.5, 2.0):
        hit = sum(1 for x in pokes if x > b)
        print('    буфер %.2f%%  выбивает %4d из %d живых идей (%.0f%%)'
              % (b, hit, alive, hit / alive * 100))
    print()
    print('  наш нынешний буфер: %.2f%% при дневном размахе 0.5%%'
          % D.stop_hunt_pct(0.5))


main()
