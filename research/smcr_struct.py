"""
Разбор SMC 01.10.2026 (docs/SMC_разбор_аналитика_2026-10-01.md), структура:
стенд по всей истории (как research/smc_lab.py) с ПОДМЕНЁННОЙ функцией общего
слоя. Файлы бота не меняются — подмена живёт только в процессе замера.

Варианты:
    base  ядро как есть
    S1    экстремум снятия — по всем свечам от прокола до возврата
    S2    стоп за снятием — только если снятие не раньше начала ноги (−3 свечи)
    S3    ордер-блок у начала импульса (экстремум между пробитым свингом и сломом)
    S4    цели — только по пулам, которые цена не прошла; снятие известно с возврата

20 пар списка бота (research/ai_doctrine.PAIRS), пять периодов; свежий — из
backtest_cache_fresh20 (там все 20 пар). Строки — results/smcr/rows_<вар>_<период>.pkl.

    python research/smcr_struct.py base S1 S2 S3 S4
"""
import os
import sys
import time
from multiprocessing import Pool

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, 'Live_Bot'))

OUT = os.path.join(HERE, 'results', 'smcr')
CACHES = {'bear': 'backtest_cache_bear', 'mid1': 'backtest_cache_mid1', 'mid2': 'backtest_cache_mid2',
          '12m': 'backtest_cache_12m', 'fresh': 'backtest_cache_fresh20'}

_patched = None
_arrays = {}


def apply_patch(variant):
    """Подмена функций общего слоя в ЭТОМ процессе (бот не затрагивается)."""
    global _patched
    if _patched == variant:
        return
    from smc import liquidity, poi, signal
    import importlib
    importlib.reload(liquidity)
    importlib.reload(poi)
    importlib.reload(signal)
    if variant in ('S1', 'ALL'):
        orig = liquidity.find_sweeps

        def find_sweeps_full(df, pools, min_penetration=None, reclaim_bars=None):
            out = orig(df, pools, min_penetration, reclaim_bars)
            high = df['high'].to_numpy(dtype=float)
            low = df['low'].to_numpy(dtype=float)
            for s in out:
                a, b = s['index'], s['reclaimed_at']
                ext = high[a:b + 1].max() if s['side'] == liquidity.BSL else low[a:b + 1].min()
                s['extreme'] = float(ext)
                s['penetration_pct'] = abs(ext - s['level']) / s['level']
            return out
        liquidity.find_sweeps = find_sweeps_full
    if variant in ('S2', 'ALL'):
        orig_bt = signal.MarketContext._build_trade

        def build_trade_attached(self, candidate, leg, direction, swept, at_index, balance, d=None):
            if swept is not None and swept['index'] < leg['start']['index'] - 3:
                swept = None
            return orig_bt(self, candidate, leg, direction, swept, at_index, balance, d=d)
        signal.MarketContext._build_trade = build_trade_attached
    if variant in ('S3', 'ALL'):
        P = poi.params

        def find_order_blocks_origin(df, structure_obj, min_impulse_pct=None, max_lookback=20):
            min_impulse_pct = P.OB_MIN_IMPULSE_PCT if min_impulse_pct is None else min_impulse_pct
            open_ = df['open'].to_numpy(dtype=float)
            close = df['close'].to_numpy(dtype=float)
            high = df['high'].to_numpy(dtype=float)
            low = df['low'].to_numpy(dtype=float)
            times = df['timestamp'].to_numpy() if 'timestamp' in df.columns else np.arange(len(df))
            blocks = []
            for event in structure_obj['events']:
                brk = event['index']
                direction = event['direction']
                a = max(0, min(event.get('broken_index', brk - max_lookback), brk - 1), brk - max_lookback)
                seg = slice(a, brk + 1)
                origin = a + int(np.argmin(low[seg]) if direction == 'BULLISH' else np.argmax(high[seg]))
                ob_idx = None
                for i in range(origin, max(-1, origin - 6), -1):
                    is_bear = close[i] < open_[i]
                    if (direction == 'BULLISH' and is_bear) or (direction == 'BEARISH' and not is_bear):
                        ob_idx = i
                        break
                if ob_idx is None:
                    continue
                if direction == 'BULLISH':
                    impulse = (high[brk] - low[ob_idx]) / low[ob_idx] if low[ob_idx] else 0.0
                else:
                    impulse = (high[ob_idx] - low[brk]) / high[ob_idx] if high[ob_idx] else 0.0
                if impulse < min_impulse_pct:
                    continue
                top, bottom = float(high[ob_idx]), float(low[ob_idx])
                if top <= bottom:
                    continue
                blocks.append({'type': 'ORDER_BLOCK', 'direction': direction, 'top': top, 'bottom': bottom,
                               'index': ob_idx, 'confirmed_at': brk, 'time': times[ob_idx],
                               'impulse_pct': float(impulse), 'break_event': event['type'], 'break_index': brk,
                               **poi._zone_geometry(direction, top, bottom)})
            return blocks
        poi.find_order_blocks = find_order_blocks_origin
    if variant in ('S4', 'ALL'):
        def untapped_pools_strict(pools, sweeps, index, side=None):
            high, low = _arrays['high'], _arrays['low']
            swept = {(s['pool']['source'], round(s['level'], 10))
                     for s in sweeps if s['reclaimed_at'] <= index}
            out = []
            for pool in pools:
                c = pool['confirmed_at']
                if c > index:
                    continue
                if side and pool['side'] != side:
                    continue
                if (pool['source'], round(pool['price'], 10)) in swept:
                    continue
                if c + 1 <= index:
                    if pool['side'] == liquidity.BSL and high[c + 1:index + 1].max() > pool['price']:
                        continue
                    if pool['side'] == liquidity.SSL and low[c + 1:index + 1].min() < pool['price']:
                        continue
                out.append(pool)
            return out
        liquidity.untapped_pools = untapped_pools_strict
    _patched = variant


def _job(args):
    variant, period, pair = args
    import logger
    logger.log = lambda *a, **k: None
    import backtest_smc as bt
    import smc_lab
    apply_patch(variant)
    from smc import signal as smc_signal
    bt.CACHE_DIR = os.path.join(HERE, CACHES[period])
    # Как bt.load_pair, но без 5-минутных свечей — стенду структуры они не нужны.
    df_1h = bt.load_cached(pair, '1h')
    if df_1h is None or not os.path.exists(os.path.join(bt.CACHE_DIR, f'{pair}_5m.pkl')):
        return variant, period, pair, [], 0.0
    df_4h = bt.load_cached(pair, '4h')
    if df_4h is None:
        df_4h = bt.resample(df_1h, '4h')
    data = {'1h': df_1h, '4h': df_4h, '1d': bt.resample(df_1h, '1D')}
    started = time.time()
    decision = smc_lab.permissive_decision()
    frames_of = {'bias': '1d', 'htf': '4h', 'poi': '1h'}
    df = data['1h']
    _arrays['high'] = df['high'].to_numpy(dtype=float)
    _arrays['low'] = df['low'].to_numpy(dtype=float)
    ctx = smc_signal.build_context({k: data[v] for k, v in frames_of.items()}, pair=pair)
    stamps = df['timestamp']
    rows = []
    for i in range(60, len(df)):
        setup, _why = ctx.evaluate(i, balance=10_000.0, decision=decision)
        if setup is None:
            continue
        p, leg, trade = setup['poi'], setup['leg'], setup['params']
        swept, fvg, brk = setup.get('sweep'), setup.get('fvg'), setup.get('structure') or {}
        t_ms = int(pd.Timestamp(stamps.iloc[i]).value // 10 ** 6) + 3_600_000
        row = {
            'layout': '1h', 'period': period, 'pair': pair, 'i': i, 't': t_ms,
            'dir': 1 if setup['direction'] == 'BULLISH' else -1,
            'poi_type': p['type'], 'poi_index': int(p['index']),
            'top': float(p['top']), 'bottom': float(p['bottom']),
            'entry_near': float(p['entry_near']), 'entry_mid': float(p['entry_mid']),
            'invalidation': float(p['invalidation']), 'touches': int(p.get('touches', 0)),
            'break_index': int(p.get('break_index', -1)), 'break_event': p.get('break_event'),
            'leg_start': float(leg['start']['price']), 'leg_end': float(leg['end']['price']),
            'leg_start_i': int(leg['start']['index']), 'leg_end_i': int(leg['end']['index']),
            'swept': swept is not None,
            'sweep_index': int(swept['index']) if swept is not None else -1,
            'sweep_extreme': float(swept['extreme']) if swept is not None else np.nan,
            'fvg': fvg is not None, 'brk': brk.get('type') if brk else None,
            'poi_score': float(setup['poi_score']), 'confluence': float(setup['confluence']),
            'entry': float(trade['entry']), 'stop': float(trade['stop_loss']),
            'targets': [float(x) for x in trade['targets']],
            'fractions': [float(x) for x in trade['fractions']],
            'rr': float(trade['rr']), 'close': float(df['close'].iloc[i]),
        }
        for name, ok in (setup.get('factors') or {}).items():
            row[f'f_{name}'] = bool(ok)
        rows.append(row)
    return variant, period, pair, rows, time.time() - started


def rows_path(variant, period):
    return os.path.join(OUT, f'rows_{variant}_{period}.pkl')


def generate(variants, periods=('bear', 'mid1', 'mid2', '12m', 'fresh')):
    import ai_doctrine as D
    os.makedirs(OUT, exist_ok=True)
    jobs = [(v, p, pair) for v in variants for p in periods for pair in D.PAIRS
            if not os.path.exists(rows_path(v, p))]
    acc = {}
    with Pool(4) as pool:
        for v, p, pair, rows, sec in pool.imap_unordered(_job, jobs):
            acc.setdefault((v, p), []).extend(rows)
            print(f'  {v:4s} {p:5s} {pair:13s} {len(rows):6d}  {sec:6.1f} с', flush=True)
    for (v, p), rows in acc.items():
        frame = pd.DataFrame(rows)
        path = rows_path(v, p)
        frame.to_pickle(path + '.tmp')
        os.replace(path + '.tmp', path)
        print(f'{v} {p}: {len(frame)} → {path}', flush=True)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    for v in sys.argv[1:] or ['base']:
        # База по bear…12m — готовые строки стенда (results/smc_lab_rows_<период>.pkl,
        # совпадение проверено); свежий период там только для 10 пар — досчитываем.
        generate([v], periods=('fresh',) if v == 'base' else ('bear', 'mid1', 'mid2', '12m', 'fresh'))
