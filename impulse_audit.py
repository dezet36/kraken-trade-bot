"""
impulse_audit.py — диагностика качества якорей A/B и натяжки сетки Фибоначчи.

Сканирует 12-мес 1H-данные скользящим окном 48 свечей (как живой бот каждый час),
детектит импульс ТЕКУЩИМ алгоритмом (bt.find_impulse_new — синхронен с live) и меряет:

1. broken_A: в ноге [A-3 .. B] есть low НИЖЕ якоря A (LONG) — сетка натянута не от
   истинного начала движения (все уровни 38.2/61.8/88.6/TP смещены).
2. broken_B: в [A .. B+3] есть high ВЫШЕ якоря B — сетка короче реального движения.
3. fallback_share: доля сетапов из fallback-ветки (глобальный max/min, БЕЗ проверки
   свежести) и их «протухлость» (возраст B).
4. deep_internal_retrace: внутри ноги A->B был откат >60% размера (нога — не импульс).
5. equal_bars_missed: у окна есть равные max-high свечи (strict-фрактал слепнет).

Смещения якорей меряются в % размера импульса — прямая мера искажения сетки.
"""
import numpy as np
import pandas as pd

import backtest as bt
import bt12

LOOKBACK = 48
SNAP = 3


def audit_pair(pair, df1):
    h = df1['high'].values.astype(float)
    l = df1['low'].values.astype(float)
    n = len(df1)
    stats = {'windows': 0, 'setups': 0, 'swing': 0, 'fallback': 0,
             'broken_A': 0, 'broken_B': 0, 'shift_A_pct': [], 'shift_B_pct': [],
             'deep_retrace': 0, 'fallback_stale': 0, 'equal_top_windows': 0}

    for end in range(LOOKBACK, n + 1):
        seg_df = df1.iloc[end - LOOKBACK:end]
        stats['windows'] += 1

        # равные вершины в окне (слепое пятно strict-фракталов)
        seg_h = h[end - LOOKBACK:end]
        mx = seg_h.max()
        if (seg_h == mx).sum() >= 2:
            stats['equal_top_windows'] += 1

        setup = bt.find_impulse_new(seg_df, lookback=LOOKBACK)
        if not setup:
            continue
        if setup['size'] / setup['end_price'] * 100 < bt.MIN_IMPULSE_PCT:
            continue
        stats['setups'] += 1

        # восстановим индексы якорей внутри окна (по ценам)
        seg = seg_df.reset_index(drop=True)
        sh = seg['high'].values.astype(float)
        sl = seg['low'].values.astype(float)
        is_long = setup['type'] == 'LONG'
        a_p, b_p = setup['start_price'], setup['end_price']
        size = setup['size']
        if is_long:
            a_cand = np.where(np.isclose(sl, a_p))[0]
            b_cand = np.where(np.isclose(sh, b_p))[0]
        else:
            a_cand = np.where(np.isclose(sh, a_p))[0]
            b_cand = np.where(np.isclose(sl, b_p))[0]
        if not len(a_cand) or not len(b_cand):
            continue
        a_i = int(a_cand[0])
        b_i = int(b_cand[-1])
        if a_i >= b_i:
            continue

        # свинг или fallback? (свинг-фрактал не может быть в последних 2 барах окна;
        # + fallback-B может быть старым)
        highs, lows = bt.find_extremes(seg, n=2)
        swing_like = any(x['index'] == b_i for x in (highs if is_long else lows))
        if swing_like:
            stats['swing'] += 1
        else:
            stats['fallback'] += 1
            if b_i < LOOKBACK - 24:
                stats['fallback_stale'] += 1

        # broken A: истинный экстремум чуть раньше/внутри ноги ниже якоря
        lo_win = sl[max(0, a_i - SNAP):b_i + 1] if is_long else sh[max(0, a_i - SNAP):b_i + 1]
        if is_long:
            true_a = lo_win.min()
            if true_a < a_p - 1e-12:
                stats['broken_A'] += 1
                stats['shift_A_pct'].append((a_p - true_a) / size * 100)
        else:
            true_a = lo_win.max()
            if true_a > a_p + 1e-12:
                stats['broken_A'] += 1
                stats['shift_A_pct'].append((true_a - a_p) / size * 100)

        # broken B: истинный экстремум в ноге/сразу после выше якоря
        hi_win = sh[a_i:min(len(seg), b_i + 1 + SNAP)] if is_long else sl[a_i:min(len(seg), b_i + 1 + SNAP)]
        if is_long:
            true_b = hi_win.max()
            if true_b > b_p + 1e-12:
                stats['broken_B'] += 1
                stats['shift_B_pct'].append((true_b - b_p) / size * 100)
        else:
            true_b = hi_win.min()
            if true_b < b_p - 1e-12:
                stats['broken_B'] += 1
                stats['shift_B_pct'].append((b_p - true_b) / size * 100)

        # глубокий внутренний ретрейс ноги (нога — не импульс)
        leg_h, leg_l = sh[a_i:b_i + 1], sl[a_i:b_i + 1]
        if is_long:
            run_max = np.maximum.accumulate(leg_h)
            max_dd = (run_max - leg_l).max()
        else:
            run_min = np.minimum.accumulate(leg_l)
            max_dd = (leg_h - run_min).max()
        if max_dd > 0.6 * size:
            stats['deep_retrace'] += 1

    return stats


def run():
    import time
    t0 = time.time()
    data_1h, _ = bt12.load(bt12.PAIRS10)
    agg = None
    for pair in bt12.PAIRS10:
        s = audit_pair(pair, data_1h[pair])
        print(f"{pair:<10} окон={s['windows']:<6} сетапов={s['setups']:<5} "
              f"swing/fb={s['swing']}/{s['fallback']} "
              f"брак A={s['broken_A']} ({s['broken_A']/max(s['setups'],1)*100:.0f}%) "
              f"брак B={s['broken_B']} ({s['broken_B']/max(s['setups'],1)*100:.0f}%) "
              f"fb-протух={s['fallback_stale']} глуб.ретрейс={s['deep_retrace']}")
        if agg is None:
            agg = {k: (list(v) if isinstance(v, list) else v) for k, v in s.items()}
        else:
            for k, v in s.items():
                if isinstance(v, list):
                    agg[k].extend(v)
                else:
                    agg[k] += v

    st = agg['setups']
    print('\n===== ИТОГО (10 пар, 12 мес, скользящее окно 1H) =====')
    print(f"Окон: {agg['windows']} | сетапов (>=3%): {st} | swing {agg['swing']} / fallback {agg['fallback']}")
    print(f"Сломанный якорь A: {agg['broken_A']} ({agg['broken_A']/st*100:.1f}% сетапов), "
          f"медианный сдвиг {np.median(agg['shift_A_pct']):.1f}% размера" if agg['shift_A_pct'] else "A: 0")
    print(f"Сломанный якорь B: {agg['broken_B']} ({agg['broken_B']/st*100:.1f}% сетапов), "
          f"медианный сдвиг {np.median(agg['shift_B_pct']):.1f}% размера" if agg['shift_B_pct'] else "B: 0")
    print(f"Fallback протухший (B старше 24 свечей): {agg['fallback_stale']} "
          f"({agg['fallback_stale']/st*100:.1f}% сетапов)")
    print(f"Глубокий внутренний ретрейс ноги (>60% размера): {agg['deep_retrace']} "
          f"({agg['deep_retrace']/st*100:.1f}% сетапов)")
    print(f"Окон с равными max-вершинами (слепое пятно strict-фракталов): "
          f"{agg['equal_top_windows']} ({agg['equal_top_windows']/agg['windows']*100:.1f}% окон)")
    print(f"\nВремя: {(time.time()-t0)/60:.1f} мин")


if __name__ == '__main__':
    run()
