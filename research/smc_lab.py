"""
Стенд SMC: все срабатывания ядра на 20 парах и пяти периодах — один раз,
дальше любой вариант правил собирается фильтром и прогоняется через портфель
с правилами живого брокера.

ЗАЧЕМ. Владелец 27.09.2026: «сделаем стратегию прибыльной». На истории SMC в
живых условиях около нуля (docs/ИИ_замечания_на_проверку.md, п. 64): плюс
старых бэктестов — стопы теснее 0.75%, которых брокер и настройки не берут.
Искать улучшение перебором по полному пересчёту ядра дорого (минуты на
вариант) и опасно (соблазн перебрать сотни вариантов). Здесь ядро считается
один раз с МЯГКИМИ порогами, а каждое срабатывание пишется целиком.

ПОЧЕМУ ФИЛЬТР ТОЧЕН. MarketContext.evaluate берёт лучшую по баллу зону и
только потом проверяет порог конфлюенса, минимальный стоп и R:R — другой
зоны взамен не ищет (smc/signal.py, шаги 6–8). Значит, «строже порог» =
«отбросить срабатывания, не прошедшие его». Повторы одной зоны (пара, тип,
индекс, сторона) отбрасываются ПОСЛЕ фильтра — первое прошедшее срабатывание
становится заявкой, ровно как у бота и бэктеста.

Мягкие пороги генерации: стоп от 0.2%, R:R от 1.5, конфлюенс от 0. Остальные
решения — как у SMC сейчас (smc/params: только ордер-блоки, премия/дисконт,
стоп консервативный +0.15%, цели Фибо 25/25/50).

Запуск:
    python research/smc_lab.py gen            # все периоды (≈15–30 мин на 4 ядрах)
    python research/smc_lab.py gen bear mid1  # выбранные
    python research/smc_lab.py gen 4h         # зоны на 4 ч (слом и направление — день)
    python research/smc_lab.py gen 15m --pool10   # зоны на 15 мин, только 10 пар пула
Строки: research/results/smc_lab_rows_<период>.pkl (в .gitignore).
"""
import os
import sys
import time
from multiprocessing import Pool
from types import SimpleNamespace

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, 'Live_Bot'))

OUT = os.path.join(HERE, 'results')


def permissive_decision():
    from strategies.smc import params as smc_params
    d = SimpleNamespace(**{name: getattr(smc_params, name) for name in smc_params.DECISION})
    d.MIN_SL_PCT = 0.002
    d.MIN_RR = 1.5
    d.MAX_RR = 0.0
    d.MIN_CONFLUENCE_SCORE = 0.0
    d.LONG_CONFLUENCE_PREMIUM = 0.0
    d.SKIP_TARGET_TAKEN = False
    d.KILLZONE_AS_GATE = False
    d.LEG_BARS_MIN = 0
    d.LEG_BARS_MAX = 0
    return d


# Раскладки кадров: зоны — рабочий ТФ; слом и направление — старше
# (как в research/ai_mtf_smc.py). '1h' — боевая SMC.
LAYOUTS = {
    '1h': ({'bias': '1d', 'htf': '4h', 'poi': '1h'}, 1.0),
    '4h': ({'bias': '1d', 'htf': '1d', 'poi': '4h'}, 4.0),
    '15m': ({'bias': '1d', 'htf': '4h', 'poi': '15m'}, 0.25),
}


def rows_path(period, layout='1h'):
    suffix = '' if layout == '1h' else f'_{layout}'
    return os.path.join(OUT, f'smc_lab_rows_{period}{suffix}.pkl')


def _job(args):
    period, cache, pair, layout = args
    from infra import logger
    logger.log = lambda *a, **k: None
    import backtest_smc as bt
    from strategies.smc import signal as smc_signal
    bt.CACHE_DIR = os.path.join(HERE, cache)
    data = bt.load_pair(pair)
    if data is None:
        return period, pair, [], 0.0
    started = time.time()
    decision = permissive_decision()
    frames_of, bar_h = LAYOUTS[layout]
    ctx = smc_signal.build_context({k: data[v] for k, v in frames_of.items()}, pair=pair)
    df = data[frames_of['poi']]
    stamps = df['timestamp']
    rows = []
    for i in range(60, len(df)):
        setup, _why = ctx.evaluate(i, decision=decision)
        if setup is None:
            continue
        poi, leg, trade = setup['poi'], setup['leg'], setup['params']
        swept, fvg, brk = setup.get('sweep'), setup.get('fvg'), setup.get('structure') or {}
        # заявка — на закрытии свечи рабочего ТФ
        t_ms = int(pd.Timestamp(stamps.iloc[i]).value // 10 ** 6) + int(bar_h * 3_600_000)
        row = {
            'layout': layout,
            'period': period, 'pair': pair, 'i': i, 't': t_ms,
            'dir': 1 if setup['direction'] == 'BULLISH' else -1,
            'poi_type': poi['type'], 'poi_index': int(poi['index']),
            'top': float(poi['top']), 'bottom': float(poi['bottom']),
            'entry_near': float(poi['entry_near']), 'entry_mid': float(poi['entry_mid']),
            'invalidation': float(poi['invalidation']), 'touches': int(poi.get('touches', 0)),
            'leg_start': float(leg['start']['price']), 'leg_end': float(leg['end']['price']),
            'leg_start_i': int(leg['start']['index']), 'leg_end_i': int(leg['end']['index']),
            'swept': swept is not None,
            'sweep_extreme': float(swept['extreme']) if swept is not None else np.nan,
            'fvg': fvg is not None,
            'brk': brk.get('type') if brk else None,
            'poi_score': float(setup['poi_score']), 'confluence': float(setup['confluence']),
            'entry': float(trade['entry']), 'stop': float(trade['stop_loss']),
            'targets': [float(x) for x in trade['targets']],
            'fractions': [float(x) for x in trade['fractions']],
            'rr': float(trade['rr']),
            'close': float(df['close'].iloc[i]),
        }
        for name, ok in (setup.get('factors') or {}).items():
            row[f'f_{name}'] = bool(ok)
        rows.append(row)
    return period, pair, rows, time.time() - started


def generate(periods, layout='1h', pairs=None):
    import ai_doctrine as D
    pairs = pairs or D.PAIRS
    jobs = [(p, D.PERIODS[p], pair, layout) for p in periods for pair in pairs]
    by_period = {p: [] for p in periods}
    with Pool(4) as pool:
        for period, pair, rows, sec in pool.imap_unordered(_job, jobs):
            by_period[period] += rows
            print(f'  {layout} {period:6s} {pair:13s} срабатываний {len(rows):6d}  {sec:6.1f} с', flush=True)
    for period, rows in by_period.items():
        frame = pd.DataFrame(rows)
        path = rows_path(period, layout)
        frame.to_pickle(path + '.tmp')
        os.replace(path + '.tmp', path)
        print(f'{layout} {period}: {len(frame)} срабатываний → {path}', flush=True)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    if sys.argv[1:2] == ['gen']:
        import ai_doctrine as D
        args = sys.argv[2:]
        layout = '1h'
        if args and args[0] in LAYOUTS:
            layout, args = args[0], args[1:]
        pool10 = None
        if '--pool10' in args:
            import backtest_smc as bt
            pool10, args = list(bt.DEFAULT_PAIRS), [a for a in args if a != '--pool10']
        generate(args or list(D.PERIODS), layout, pool10)
