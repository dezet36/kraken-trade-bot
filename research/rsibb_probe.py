"""
RSI плюс Боллинджер: распределения до назначения порогов.

ПЕРВЫЙ ВОПРОС РЕШАЕТ СУДЬБУ ЗАТЕИ ДО ВСЯКОЙ СТРАТЕГИИ: насколько далеко цель.
Вход на полосе, цель на средней линии — расстояние равно полуширине канала. На
пятиминутках оно может оказаться в десятые доли процента, и тогда круг
мейкер-мейкер (0.040%) съедает заметную часть цели ещё до разговора о том,
доходит ли она. Ровно на этой арифметике умерла сетка в коридоре.

ОСТАЛЬНЫЕ ВОПРОСЫ
    2. как часто цена вообще выходит за полосу — есть ли из чего выбирать;
    3. каков RSI В МОМЕНТ выхода за полосу: если он и так почти всегда за
       порогом, то «подтверждение RSI» ничего не подтверждает, а лишь
       переписывает то же условие другими словами;
    4. каков ADX в этот момент и есть ли у него разброс — фильтр по значению,
       которое почти не меняется, отсеет либо всё, либо ничего;
    5. чем кончается выход за полосу: возвратом к средней линии или ходьбой
       вдоль полосы. Это и есть та самая болезнь, названная в источниках.

Ничего не торгуется. Запуск:
    python research/rsibb_probe.py
"""

import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'Live_Bot'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from smc_market_regime import BULL_CACHE, BULL_PAIRS  # noqa: E402

PAIRS_LIMIT = 6
TIMEFRAME = '5m'
FORWARD = 48          # сколько баров даём на возврат к средней линии


def q(values, *points):
    values = np.asarray(values, dtype=float)
    values = values[np.isfinite(values)]
    if len(values) == 0:
        return [float('nan')] * len(points)
    return [float(np.percentile(values, p)) for p in points]


def main():
    os.environ['SMC_CACHE_DIR'] = BULL_CACHE
    import backtest_smc as bt
    from rsibb import core

    frames = []
    for pair in BULL_PAIRS[:PAIRS_LIMIT]:
        loaded = bt.load_pair(pair)
        if loaded and TIMEFRAME in loaded:
            frames.append((pair, loaded[TIMEFRAME]))
    bars = sum(len(df) for _, df in frames)
    print(f'пар: {len(frames)}   баров {TIMEFRAME}: {bars}')
    print()

    half_pct, rsi_at, adx_at, touches = [], [], [], 0
    reverted, walked, neither = 0, 0, 0
    walk_len = []
    # Разрез, ради которого замер и стоит делать: возвращается ли цена к
    # средней линии ЧАЩЕ, если наложить фильтры. Источники утверждают, что
    # именно фильтры превращают 45% в 58-65%. Здесь это проверяется на
    # исходе, а не на итоговом винрейте, — исход не зависит от геометрии
    # стопа и потому не может быть подогнан.
    def extreme(r, low_thr, high_thr):
        return r <= low_thr or r >= high_thr

    cuts = {
        'без фильтров':            lambda r, a, lg: True,
        # ── Канон: RSI подтверждает крайность, ADX разрешает боковик ────────
        'RSI 30/70':               lambda r, a, lg: extreme(r, 30, 70),
        'RSI 25/75':               lambda r, a, lg: extreme(r, 25, 75),
        'ADX < 25':                lambda r, a, lg: np.isfinite(a) and a < 25,
        'ADX < 20':                lambda r, a, lg: np.isfinite(a) and a < 20,
        'RSI 30/70 и ADX < 25':    lambda r, a, lg: extreme(r, 30, 70)
                                                    and np.isfinite(a) and a < 25,
        'RSI 30/70 и ADX < 20':    lambda r, a, lg: extreme(r, 30, 70)
                                                    and np.isfinite(a) and a < 20,
        # ── Обратное прочтение, на которое указывают сами числа ─────────────
        # Если крайний RSI на полосе ухудшает возврат, значит он помечает не
        # истощение, а импульс — то есть ровно ходьбу по полосе. Тогда
        # работать должно НЕкрайнее значение.
        'RSI нейтральный 40-60':   lambda r, a, lg: 40 <= r <= 60,
        'RSI некрайний 35-65':     lambda r, a, lg: 35 <= r <= 65,
        # Расхождение: цена на нижней полосе, а импульс НЕ слаб (и наоборот).
        'расхождение (лонг RSI>45)': lambda r, a, lg: (r > 45) if lg else (r < 55),
        'расхождение сильнее (>50)': lambda r, a, lg: (r > 50) if lg else (r < 50),
    }
    tally = {name: [0, 0] for name in cuts}      # [вернулась, всего]

    for pair, df in frames:
        ind = core.indicators(df['open'].to_numpy(float),
                              df['high'].to_numpy(float),
                              df['low'].to_numpy(float),
                              df['close'].to_numpy(float))
        low, high, close = ind['low'], ind['high'], ind['close']
        lower, upper, mid = ind['lower'], ind['upper'], ind['mid']
        n = len(close)

        below = low <= lower
        above = high >= upper
        hit = np.flatnonzero((below | above) & np.isfinite(mid))
        touches += len(hit)

        for i in hit:
            if ind['width'][i] <= 0:
                continue
            half_pct.append(ind['width'][i] / 2 / close[i] * 100)
            rsi_at.append(ind['rsi'][i])
            adx_at.append(ind['adx'][i])

        # Чем кончается выход за полосу. Берём только те бары, где выход
        # НАЧАЛСЯ (предыдущий бар полосу не задевал), иначе одна затяжная
        # ходьба вдоль полосы посчиталась бы двадцать раз.
        starts = [i for i in hit
                  if i > 0 and not (below[i - 1] or above[i - 1])]
        for i in starts:
            if i + FORWARD >= n:
                continue
            long_side = below[i]
            window = slice(i + 1, i + 1 + FORWARD)
            if long_side:
                back = np.flatnonzero(high[window] >= mid[i + 1:i + 1 + FORWARD])
                further = np.flatnonzero(low[window] <= lower[i + 1:i + 1 + FORWARD])
            else:
                back = np.flatnonzero(low[window] <= mid[i + 1:i + 1 + FORWARD])
                further = np.flatnonzero(high[window] >= upper[i + 1:i + 1 + FORWARD])
            first_back = back[0] if len(back) else None
            # «Ходьба» — цена ещё раз выходит за ту же полосу ДО того, как
            # вернулась к средней линии.
            first_walk = None
            for k in further:
                if first_back is None or k < first_back:
                    first_walk = k
                    break
            came_back = first_back is not None and first_walk is None
            if came_back:
                reverted += 1
            elif first_walk is not None:
                walked += 1
                walk_len.append(int(np.sum(further < (first_back if first_back
                                                      is not None else FORWARD))))
            else:
                neither += 1

            value, strength = ind['rsi'][i], ind['adx'][i]
            if not np.isfinite(value):
                continue
            for name, keep in cuts.items():
                if keep(value, strength, bool(long_side)):
                    tally[name][1] += 1
                    tally[name][0] += int(came_back)

    print('1. ЦЕЛЬ В ПРОЦЕНТАХ ЦЕНЫ (полуширина канала)')
    p10, p25, p50, p75 = q(half_pct, 10, 25, 50, 75)
    print(f'   10% / 25% / медиана / 75%:  {p10:.3f} / {p25:.3f} / '
          f'{p50:.3f} / {p75:.3f}')
    print(f'   круг мейкер-мейкер 0.040% съедает от медианной цели: '
          f'{0.040 / p50 * 100:.0f}%')
    print(f'   круг тейкер-тейкер 0.210% съедает от медианной цели: '
          f'{0.210 / p50 * 100:.0f}%')
    print('   Если издержки съедают заметную долю ЦЕЛИ, дальше можно не идти:')
    print('   именно на этом умерла сетка в коридоре.')
    print()

    print('2. КАК ЧАСТО ЦЕНА ВЫХОДИТ ЗА ПОЛОСУ')
    print(f'   касаний: {touches} — это {touches / bars * 100:.1f}% баров')
    print()

    print('3. RSI В МОМЕНТ ВЫХОДА ЗА ПОЛОСУ')
    r10, r25, r50, r75, r90 = q(rsi_at, 10, 25, 50, 75, 90)
    print(f'   10/25/50/75/90:  {r10:.0f} / {r25:.0f} / {r50:.0f} / '
          f'{r75:.0f} / {r90:.0f}')
    values = np.asarray(rsi_at, dtype=float)
    values = values[np.isfinite(values)]
    for low_thr, high_thr in ((30, 70), (25, 75), (20, 80)):
        share = np.mean((values <= low_thr) | (values >= high_thr))
        print(f'   порог {low_thr}/{high_thr}: подтверждает {share:.0%} касаний')
    print('   Если подтверждает почти всё — RSI не фильтр, а переписанное')
    print('   другими словами условие «цена ушла за полосу».')
    print()

    print('4. ADX В МОМЕНТ ВЫХОДА ЗА ПОЛОСУ')
    a10, a25, a50, a75, a90 = q(adx_at, 10, 25, 50, 75, 90)
    print(f'   10/25/50/75/90:  {a10:.0f} / {a25:.0f} / {a50:.0f} / '
          f'{a75:.0f} / {a90:.0f}')
    strength = np.asarray(adx_at, dtype=float)
    strength = strength[np.isfinite(strength)]
    for cap in (20, 25, 30, 40):
        print(f'   ADX < {cap}: остаётся {np.mean(strength < cap):.0%} касаний')
    print('   Пороги 20-25 названы в источниках. Проверяем, сколько после них')
    print('   вообще останется сделок — фильтр, режущий 90%, не проверяем.')
    print()

    print('5. ЧЕМ КОНЧАЕТСЯ ВЫХОД ЗА ПОЛОСУ (48 баров вперёд)')
    total = reverted + walked + neither
    if total:
        print(f'   вернулась к средней линии:   {reverted:>6} '
              f'({reverted / total:.0%})')
        print(f'   пошла вдоль полосы дальше:   {walked:>6} '
              f'({walked / total:.0%})')
        print(f'   ни то ни другое:             {neither:>6} '
              f'({neither / total:.0%})')
    print('   «Пошла вдоль полосы» — это та самая болезнь: цена выходит за')
    print('   полосу ЕЩЁ РАЗ, не успев вернуться к средней. Если таких')
    print('   большинство, фильтр состояния обязан их отделять — иначе')
    print('   разговаривать не о чем.')
    print()

    print('6. ГЛАВНАЯ ПРОВЕРКА: ПОДНИМАЮТ ЛИ ФИЛЬТРЫ ДОЛЮ ВОЗВРАТОВ')
    print(f'   {"фильтр":<24}{"случаев":>9}{"вернулась к средней":>22}')
    print('   ' + '-' * 55)
    base_rate = None
    for name in cuts:
        back, total_cut = tally[name]
        if total_cut == 0:
            continue
        rate = back / total_cut
        if base_rate is None:
            base_rate = rate
        delta = f'{(rate - base_rate) * 100:+.1f} п.п.' if name != 'без фильтров' else ''
        print(f'   {name:<24}{total_cut:>9}{rate:>18.0%}   {delta}')
    print()
    print('   Источники утверждают, что фильтры превращают ~45% попаданий в')
    print('   58-65%, то есть обязаны дать примерно 15 процентных пунктов.')
    print('   Здесь это проверяется на ИСХОДЕ движения, а не на итоговом')
    print('   винрейте: исход не зависит от того, где мы поставим стоп, и')
    print('   потому его нельзя подогнать геометрией.')
    print('   Если прибавка около нуля — фильтры не отличают боковик от')
    print('   тренда, и вся конструкция держится на утверждении, которого')
    print('   в данных нет.')


if __name__ == '__main__':
    main()
