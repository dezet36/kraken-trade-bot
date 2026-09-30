"""
Стенд SMC «глазами бота»: срабатывания ядра на ОКНЕ свечей живого бота.

ЗАЧЕМ (аудит SMC, 30.09.2026). research/smc_lab.py строит структуру один раз
по всей истории периода (до 1.5 года часовых свечей) и оценивает каждый час на
ней. Живой бот строит её каждый час заново по последним свечам
(market_structure.load_frames: 1d — LOOKBACK_BIAS+4, 4h — LOOKBACK_HTF+4,
1h — LOOKBACK_POI+4 закрытых). Утечки будущего нет ни там, ни там, но пулы
ликвидности, которые ядро ставит первой целью, зависят от глубины истории.
Сверка пяти живых сетапов 22–25.09.2026 со стендом: вход и стоп совпали во
всех пяти, цели — в одном (у ETH первая цель бота 2788.07, стенда 2713.35).
Пересчёт на окне бота повторил живой сетап ETH до цента.

Здесь каждый час каждой пары пула оценивается так, как это сделал бы бот:
окно закрытых свечей на момент закрытия часа → build_context → evaluate по
последней свече с мягкими порогами smc_lab (дальше пороги накладывает
smc_lab_eval, как и для обычного стенда). Индексы зоны и ноги переводятся из
окна в сквозные — иначе ключ зоны менялся бы каждый час.

Запуск:
    python research/smc_lab_live.py gen             # 10 пар пула × 5 периодов, 4 процесса
    python research/smc_lab_live.py gen fresh       # выбранные периоды
Строки: research/results/smc_lab_rows_<период>_live.pkl (в .gitignore);
читаются как раскладка 'live' (smc_lab_eval.rows(period, 'live')).
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

OUT = os.path.join(HERE, 'results')
FRAMES = (('bias', '1d', 'LOOKBACK_BIAS', 24), ('htf', '4h', 'LOOKBACK_HTF', 4), ('poi', '1h', 'LOOKBACK_POI', 1))
H_MS = 3_600_000


def rows_path(period):
    return os.path.join(OUT, f'smc_lab_rows_{period}_live.pkl')


def _stamps_ms(df):
    ts = pd.to_datetime(df['timestamp'], utc=True)
    return ((ts - pd.Timestamp('1970-01-01', tz='UTC')) // pd.Timedelta(milliseconds=1)).to_numpy(dtype='int64')


def _job(args):
    period, cache, pair = args
    import logger
    logger.log = lambda *a, **k: None
    import backtest_smc as bt
    import smc_lab
    from smc import params as P
    from smc import signal as smc_signal
    bt.CACHE_DIR = os.path.join(HERE, cache)
    data = bt.load_pair(pair)
    if data is None:
        return period, pair, [], 0.0
    started = time.time()
    decision = smc_lab.permissive_decision()
    frames = {}
    for key, tf, lookback_name, hours in FRAMES:
        df = data[tf].reset_index(drop=True)
        frames[key] = (df, _stamps_ms(df) + hours * H_MS, getattr(P, lookback_name) + 4)
    poi_df, poi_close, _ = frames['poi']
    rows = []
    since = int(os.getenv('SMC_LIVE_SINCE_MS', '0'))      # для проверки на коротком куске
    for i in range(60, len(poi_df)):
        t_close = int(poi_close[i])
        if t_close < since:
            continue
        window, offset = {}, 0
        for key, (df, closes, depth) in frames.items():
            end = int(np.searchsorted(closes, t_close, side='right'))      # закрытые к t_close
            start = max(0, end - depth)
            window[key] = df.iloc[start:end].reset_index(drop=True)
            if key == 'poi':
                offset = start
        if len(window['poi']) < 60:
            continue
        ctx = smc_signal.build_context(window, pair=pair)
        at = len(window['poi']) - 1
        setup, _why = ctx.evaluate(at, balance=10_000.0, decision=decision)
        if setup is None:
            continue
        poi, leg, trade = setup['poi'], setup['leg'], setup['params']
        swept, fvg, brk = setup.get('sweep'), setup.get('fvg'), setup.get('structure') or {}
        row = {
            'layout': 'live',
            'period': period, 'pair': pair, 'i': i, 't': t_close,
            'dir': 1 if setup['direction'] == 'BULLISH' else -1,
            'poi_type': poi['type'], 'poi_index': int(poi['index']) + offset,
            'top': float(poi['top']), 'bottom': float(poi['bottom']),
            'entry_near': float(poi['entry_near']), 'entry_mid': float(poi['entry_mid']),
            'invalidation': float(poi['invalidation']), 'touches': int(poi.get('touches', 0)),
            'leg_start': float(leg['start']['price']), 'leg_end': float(leg['end']['price']),
            'leg_start_i': int(leg['start']['index']) + offset, 'leg_end_i': int(leg['end']['index']) + offset,
            'swept': swept is not None,
            'sweep_extreme': float(swept['extreme']) if swept is not None else np.nan,
            'fvg': fvg is not None,
            'brk': brk.get('type') if brk else None,
            'poi_score': float(setup['poi_score']), 'confluence': float(setup['confluence']),
            'entry': float(trade['entry']), 'stop': float(trade['stop_loss']),
            'targets': [float(x) for x in trade['targets']],
            'fractions': [float(x) for x in trade['fractions']],
            'rr': float(trade['rr']),
            'close': float(window['poi']['close'].iloc[at]),
        }
        for name, ok in (setup.get('factors') or {}).items():
            row[f'f_{name}'] = bool(ok)
        rows.append(row)
    return period, pair, rows, time.time() - started


def generate(periods):
    import ai_doctrine as D
    import backtest_smc as bt
    jobs = [(p, D.PERIODS[p], pair) for p in periods for pair in bt.DEFAULT_PAIRS]
    by_period = {p: [] for p in periods}
    with Pool(4) as pool:
        for period, pair, rows, sec in pool.imap_unordered(_job, jobs):
            by_period[period] += rows
            print(f'  live {period:6s} {pair:13s} срабатываний {len(rows):6d}  {sec:7.1f} с', flush=True)
    for period, rows in by_period.items():
        frame = pd.DataFrame(rows)
        path = rows_path(period)
        frame.to_pickle(path + '.tmp')
        os.replace(path + '.tmp', path)
        print(f'live {period}: {len(frame)} срабатываний → {path}', flush=True)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    if sys.argv[1:2] == ['gen']:
        import ai_doctrine as D
        generate(sys.argv[2:] or list(D.PERIODS))
