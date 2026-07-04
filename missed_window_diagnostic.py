"""
missed_window_diagnostic.py — офлайн read-only диагностика "потерянного окна"
входа из-за дискретного (раз в 5 минут) опроса цены.

Гипотеза: analyze_market() генерирует сигнал, только если ТЕКУЩАЯ цена в момент
опроса лежит в окне [zone_a.top, end_price]. Между двумя опросами бота (раз в
5 минут) цена может "перепрыгнуть" всё окно на волатильном альте — сетап молча
теряется, без единой попытки поставить лимит. Этот скрипт измеряет, как часто
это реально происходит на исторических данных.

Переиспользует ЖИВЫЕ find_recent_impulse/get_zones/get_htf_trend/
calculate_trade_params из Live_Bot/strategy.py (не копии из backtest.py) —
гарантирует 100% совпадение с реальным поведением бота. Детект импульса
throttled раз в 1H (методологически согласовано с backtest_campaign.py —
структура локальных экстремумов физически не может измениться чаще, чем раз
в час, т.к. фрактальный экстремум подтверждается только после закрытия
соседних 1H свечей). Окно проверяется на КАЖДОЙ 5-минутной свече (close) —
это и есть прокси того, что видел бы бот при непрерывном 5-минутном опросе.

НЕ трогает Live_Bot/. Данные — из backtest_cache/, сети не будет.
Запуск: python missed_window_diagnostic.py   (из d:\\Bot trade\\)
"""
import sys
import time

sys.path.insert(0, r'D:\Bot trade')
sys.path.insert(0, r'D:\Bot trade\Live_Bot')

import numpy as np
import pandas as pd

import backtest as bt
import config as live_cfg
from strategy import find_recent_impulse, get_zones, get_htf_trend, calculate_trade_params

LOOKBACK    = live_cfg.LOOKBACK_CANDLES
STALE_HOURS = 72
COOLDOWN_H  = live_cfg.COOLDOWN_HOURS
APPLY_HTF   = True
APPLY_RR    = True
NOMINAL_BAL = 10_000.0


def detect_windows(pair, df1h, df5m, df4h):
    ts5  = df5m['timestamp'].values
    o5   = df5m['open'].values.astype(float)
    h5   = df5m['high'].values.astype(float)
    l5   = df5m['low'].values.astype(float)
    c5   = df5m['close'].values.astype(float)
    n5   = len(df5m)

    ts5i = df5m['timestamp'].values.astype('datetime64[ns]').astype('int64')
    ts1i = df1h['timestamp'].values.astype('datetime64[ns]').astype('int64')
    ts4i = (df4h['timestamp'].values.astype('datetime64[ns]').astype('int64')
            if df4h is not None else None)

    def try_build_window(i):
        """Пытается обнаружить свежий импульс+зону на момент 5m-свечи i (без
        учёта текущей цены относительно окна — это отдельно проверит основной
        цикл на этой же свече, чтобы не терять случаи мгновенной инвалидации)."""
        cur_i = ts5i[i]
        pos1 = int(np.searchsorted(ts1i, cur_i, side='right'))
        if pos1 < LOOKBACK:
            return None
        df1w = df1h.iloc[max(0, pos1 - LOOKBACK - 1):pos1]
        if len(df1w) < LOOKBACK:
            return None

        setup = find_recent_impulse(df1w, lookback_candles=LOOKBACK)
        if not setup:
            return None
        size_pct = setup['size'] / setup['end_price'] * 100
        if size_pct < live_cfg.MIN_IMPULSE_PCT:
            return None

        if APPLY_HTF and ts4i is not None:
            pos4 = int(np.searchsorted(ts4i, cur_i, side='right'))
            df4w = df4h.iloc[max(0, pos4 - live_cfg.HTF_EMA_SLOW - 20):pos4]
            htf = get_htf_trend(df4w)
            if (htf == 'BULLISH' and setup['type'] == 'SHORT') or \
               (htf == 'BEARISH' and setup['type'] == 'LONG'):
                return None

        zone_a, _ = get_zones(setup)
        ep, sz = setup['end_price'], setup['size']
        if setup['type'] == 'LONG':
            inv = ep - sz * live_cfg.ZONE_B_TOP
            entry_level = zone_a['top']
        else:
            inv = ep + sz * live_cfg.ZONE_B_TOP
            entry_level = zone_a['bottom']

        if APPLY_RR:
            params = calculate_trade_params(setup, entry_level, NOMINAL_BAL, log_reject=False)
            if not params:
                return None

        return {
            'pair': pair, 'dir': setup['type'], 'armed_i': i, 'armed_time': ts5[i],
            'entry_level': entry_level, 'far': ep, 'inv': inv,
            'win_top': max(entry_level, ep), 'win_bot': min(entry_level, ep),
            'impulse_pct': round(size_pct, 2), 'visited': False, 'seen': False,
        }

    results = []
    active = None
    last_det_hour = None
    cooldown_until = 0
    i = LOOKBACK * 12 + 12   # прогрев (48ч в 5м-барах + запас под HTF/searchsorted)

    while i < n5:
        if active is None:
            cur_hour = pd.Timestamp(ts5[i]).floor('h')
            if cur_hour != last_det_hour and i >= cooldown_until:
                last_det_hour = cur_hour
                active = try_build_window(i)
            if active is None:
                i += 1
                continue
            # НЕ увеличиваем i — сразу проверяем эту же 5м-свечу против только
            # что созданного окна (иначе мгновенная инвалидация на баре
            # обнаружения молча потеряется).

        aw = active
        top, bot = aw['win_top'], aw['win_bot']

        if l5[i] <= top and h5[i] >= bot:
            aw['visited'] = True
        if bot <= c5[i] <= top:
            aw['seen'] = True

        is_long = aw['dir'] == 'LONG'
        invalidated = (c5[i] < aw['inv']) if is_long else (c5[i] > aw['inv'])
        broke_past_far = (c5[i] > aw['far']) if is_long else (c5[i] < aw['far'])
        stale = (i - aw['armed_i']) > STALE_HOURS * 12

        if aw['seen'] or invalidated or broke_past_far or stale:
            aw['status'] = 'SEEN' if aw['seen'] else ('MISSED' if aw['visited'] else 'NEVER_APPROACHED')
            aw['term_reason'] = ('seen' if aw['seen'] else
                                  'invalidated' if invalidated else
                                  'broke_past_far' if broke_past_far else 'stale')
            results.append(aw)
            cooldown_until = i + COOLDOWN_H * 12
            active = None

        i += 1

    return results


def run():
    t0 = time.time()
    data_1h, data_5m = bt.load_all_data()

    all_windows = []
    for pair in bt.BACKTEST_PAIRS:
        df1h = data_1h[pair]
        df5m = data_5m[pair]
        df4h = data_1h.get(pair + '_4h')
        wins = detect_windows(pair, df1h, df5m, df4h)
        all_windows.extend(wins)
        by_status = pd.Series([w['status'] for w in wins]).value_counts().to_dict() if wins else {}
        print(f'  {pair}: {len(wins)} окон | {by_status}')

    if not all_windows:
        print('Окон не обнаружено (странно — проверьте данные/фильтры).')
        return

    df = pd.DataFrame(all_windows).drop(columns=['armed_i'])
    df.to_csv(r'D:\Bot trade\missed_window_report.csv', index=False)

    total  = len(df)
    seen   = int((df['status'] == 'SEEN').sum())
    missed = int((df['status'] == 'MISSED').sum())
    never  = int((df['status'] == 'NEVER_APPROACHED').sum())
    reachable = seen + missed

    print('\n' + '=' * 60)
    print(f'  MISSED-WINDOW | {len(bt.BACKTEST_PAIRS)} пар | лукбэк {LOOKBACK} | {bt.MONTHS_BACK} мес')
    print('=' * 60)
    print(f'  Всего окон: {total} | SEEN: {seen} | MISSED: {missed} | NEVER_APPROACHED: {never}')
    if reachable:
        print(f'  %% потерь среди достижимых (missed/(seen+missed)): {missed/reachable*100:.1f}%')
    else:
        print('  Достижимых окон (seen+missed) нет — недостаточно данных для оценки %.')
    if total:
        print(f'  %% потерь от всех окон (missed/total): {missed/total*100:.1f}%')
    if seen:
        print(f'  Потенциальный прирост выборки сделок (missed/seen): {missed/seen*100:.1f}%')
    print('=' * 60)
    print('\nПо парам и статусу:')
    print(df.groupby(['pair', 'status']).size().unstack(fill_value=0))
    print(f'\nОтчёт сохранён: missed_window_report.csv ({len(df)} строк)')
    print(f'Время: {(time.time()-t0)/60:.1f} мин')

    print('\nОриентировочная интерпретация (не откалибровано эмпирически):')
    if reachable < 25:
        print('  Выборка достижимых окон маленькая (<25) — доверяйте % слабо, это шум.')
    elif reachable:
        pct = missed / reachable * 100
        if pct < 10:
            print(f'  {pct:.1f}% < 10% — низкий приоритет, частоту опроса менять не нужно.')
        elif pct < 25:
            print(f'  {pct:.1f}% — умеренно, можно рассмотреть снижение интервала (5м -> 2-3м), не срочно.')
        else:
            print(f'  {pct:.1f}% > 25% — существенно, стоит приоритизировать снижение интервала опроса.')


if __name__ == '__main__':
    run()
