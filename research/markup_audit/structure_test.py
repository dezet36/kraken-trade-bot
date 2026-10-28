"""
Классическая структура в живой SMC (PROTOCOL_STRUCTURE.md, записан до расчёта).

    python research/markup_audit/structure_test.py gen fractal|protected [pool|other|oos|x12] [--reverse] [--workers=N]
    python research/markup_audit/structure_test.py eval [other]

gen — строки стенда «глазами бота» (research/smc_lab_live._job) сегодняшним
кодом при выбранной структуре → research/results/structure_audit/rows_<mode>_<sample>_<period>.pkl.
eval — портфель живой SMC (smc_lab_eval, фильтр толпы 0) по обоим вариантам.
"""
import os
import sys
import time
from multiprocessing import Pool

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
RESEARCH = os.path.dirname(HERE)
sys.path.insert(0, RESEARCH)
sys.path.insert(0, os.path.join(os.path.dirname(RESEARCH), 'Live_Bot'))

OUT = os.path.join(RESEARCH, 'results', 'structure_audit')
PERIODS = ['bear', 'mid1', 'mid2', '12m', 'fresh']
SELECT, ACCEPT = ('bear', 'mid1'), ('mid2', '12m', 'fresh')
WORKERS = 3


def _job(args):
    """Задание стенда с нужной структурой: настройка — до первого импорта
    общего слоя в процессе (и ещё раз явно, на случай уже загруженного).
    Готовая пара (её файл уже есть — сделал другой процесс) пропускается."""
    mode, sample, period, cache, pair, since = args
    if os.path.exists(part(mode, sample, period, pair)):
        return period, pair, None, 0.0
    os.environ['SMC_STRUCTURE_MODE'] = mode
    os.environ['SMC_LIVE_SINCE_MS'] = str(since)
    from analysis.smc import params as P
    P.STRUCTURE_MODE = mode
    import smc_lab_live
    return smc_lab_live._job((period, cache, pair))


def _save(f, target):
    f.to_pickle(target + '.tmp')
    os.replace(target + '.tmp', target)


def gen(mode, sample, reverse=False):
    """Строки по паре сохраняются сразу (parts/): прогон можно прервать и
    продолжить, а второй процесс (reverse — с конца списка) не повторит готовое."""
    import ai_doctrine as D
    import backtest_smc as bt
    import smc_lab_eval as E
    D.PERIODS['fresh'] = 'backtest_cache_fresh20'
    pairs = {p: sample_pairs(sample, p) for p in PERIODS}
    os.makedirs(os.path.join(OUT, 'parts'), exist_ok=True)
    # fresh считается только после конца 12m (раньше — уже в 12m), с запасом 2 суток
    since = {p: 0 for p in PERIODS}
    since['fresh'] = E.END_12M_MS - 2 * 86_400_000
    jobs = [(mode, sample, p, D.PERIODS[p], pair, since[p]) for p in PERIODS for pair in pairs[p]
            if not os.path.exists(path(mode, sample, p)) and not os.path.exists(part(mode, sample, p, pair))]
    if reverse:
        jobs.reverse()
    t0 = time.time()
    with Pool(WORKERS) as pool:
        for period, pair, rows, sec in pool.imap_unordered(_job, jobs):
            if rows is None:
                continue
            _save(pd.DataFrame(rows), part(mode, sample, period, pair))
            print(f'  {mode} {sample} {period:6s} {pair:13s} строк {len(rows):6d} {sec:6.0f} с '
                  f'(всего {time.time() - t0:.0f} с)', flush=True)
    for p in PERIODS:
        files = [part(mode, sample, p, pair) for pair in pairs[p]]
        if files and not os.path.exists(path(mode, sample, p)) and all(os.path.exists(x) for x in files):
            frames = [x for x in (pd.read_pickle(x) for x in files) if len(x)]
            _save(pd.concat(frames, ignore_index=True), path(mode, sample, p))


def sample_pairs(sample, period):
    """Пары выборки в периоде.
    pool  — 10 пар SMC;  other — раскладка live20 (16 пар, с пулом);
    oos   — пары списка бота вне пула, какие есть в кэше периода (28.10.2026);
    x12   — пары кэша 12m вне списка бота, только период 12m (как live12x)."""
    import ai_doctrine as D
    import backtest_smc as bt
    import smc_lab_eval as E
    D.PERIODS['fresh'] = 'backtest_cache_fresh20'
    if sample == 'pool':
        return list(bt.DEFAULT_PAIRS)
    if sample == 'other':
        return sorted(set(E.rows('bear', 'live20').pair))
    cache = os.path.join(RESEARCH, D.PERIODS[period])
    have = {f.rsplit('_', 1)[0] for f in os.listdir(cache) if f.endswith('_5m.pkl')}
    if sample == 'oos':
        return sorted(x for x in D.PAIRS if x in have and x not in bt.DEFAULT_PAIRS)
    if sample == 'x12':
        return sorted(have - set(D.PAIRS)) if period == '12m' else []
    raise ValueError(sample)


def path(mode, sample, period):
    return os.path.join(OUT, f'rows_{mode}_{sample}_{period}.pkl')


def part(mode, sample, period, pair):
    return os.path.join(OUT, 'parts', f'rows_{mode}_{sample}_{period}_{pair}.pkl')


def against(f, limit=0.0):
    return not (f.get('funding', np.nan) > limit)


MODES = ('fractal', 'protected')
# Варианты портфеля: как в боте (толпа 0), налив насквозь 0.05%, без фильтра толпы
VARIANTS = {'толпа': (True, 0.0), 'насквозь': (True, 0.0005), 'без толпы': (False, 0.0)}


def _trade_rows(mode, variant, period, out, E):
    rows = []
    for t in out['trades']:
        if period == 'fresh' and E.overlaps_12m(t):
            continue
        rows.append({'mode': mode, 'variant': variant, 'period': period, 'pair': t['pair'],
                     'side': 'лонг' if t['direction'] == 'BULLISH' else 'шорт',
                     'entry_time': t['entry_time'], 'exit_time': t['exit_time'],
                     'R': t['pnl'] / t['risk'], 'exit': t['exit_reason'], 'tps': t['tps_hit'],
                     'stop_pct': t['meta']['stop_pct'], 'rr': t['meta']['rr']})
    return rows


def stats(f, months):
    """Сделки одного варианта: число, в месяц, доля плюсовых, R, PF, просадка (R, по времени выхода)."""
    if not len(f):
        return {'сделок': 0, 'в месяц': 0.0}
    r = f.sort_values('exit_time')['R'].to_numpy()
    eq = np.cumsum(r)
    dd = np.max(np.maximum.accumulate(np.concatenate([[0.0], eq]))[1:] - eq)
    loss = -r[r <= 0].sum()
    hold = (f['exit_time'] - f['entry_time']).dt.total_seconds().mean() / 3600
    return {'сделок': len(r), 'в месяц': len(r) / months, 'плюсовых': (r > 0).mean(),
            'дошли до цели 1': (f['tps'] >= 1).mean(), 'R на сделку': r.mean(),
            '±95%': 1.96 * r.std(ddof=1) / np.sqrt(len(r)) if len(r) > 1 else np.nan,
            'итог R': r.sum(), 'PF': r[r > 0].sum() / loss if loss else np.inf,
            'макс. просадка R': dd, 'средний плюс R': r[r > 0].mean() if (r > 0).any() else 0.0,
            'средний минус R': r[r <= 0].mean() if (r <= 0).any() else 0.0,
            'держали, ч': hold, 'стоп, %': f['stop_pct'].mean()}


def evaluate(sample):
    import smcr_live as SL                                        # fresh → кэш 20 пар, BAR_H
    E = SL.E
    pairs = tuple(sample_pairs('pool' if sample == 'pool' else 'other', 'bear'))
    trades, funnel, months = [], [], {}
    for period in PERIODS:
        for mode in MODES:
            layout = f'st_{mode}_{sample}'
            E.BAR_H[layout] = 1.0
            f = pd.read_pickle(path(mode, sample, period)).sort_values(['t', 'pair']).reset_index(drop=True)
            f['key'] = list(zip(f['pair'], f['poi_type'], f['poi_index'], f['dir'], [layout] * len(f)))
            E._rows[(period, layout)] = f
            base = dict(E.LIVE, pairs=pairs, layouts=(layout,))
            keep = (lambda o: not E.overlaps_12m(o)) if period == 'fresh' else (lambda o: True)
            setups = [o for o in E.orders_for(period, dict(base, filt=None)) if keep(o)]
            crowd = [o for o in E.orders_for(period, dict(base, filt=lambda r, x: against(x))) if keep(o)]
            seen = f[f['t'] > E.END_12M_MS] if period == 'fresh' else f
            longs = sum(o.direction == 'BULLISH' for o in crowd)
            funnel.append({'mode': mode, 'period': period, 'часов с сетапом': len(seen),
                           'зон': seen['key'].nunique(), 'заявок без толпы': len(setups),
                           'заявок с толпой': len(crowd), 'лонг/шорт': f'{longs}/{len(crowd) - longs}'})
            for variant, (use_crowd, ft) in VARIANTS.items():
                spec = dict(base, filt=(lambda r, x: against(x)) if use_crowd else None, fill_through=ft)
                out, _ = E.portfolio(period, spec)
                trades += _trade_rows(mode, variant, period, out, E)
        data = E._exec.get(period, {})
        # длина периода — по свечам исполнения (мс от эпохи: свечи бывают с поясом и без)
        lo = min(pd.Timestamp(df['timestamp'].iloc[0]).value for df in data.values()) // 10 ** 6
        hi = max(pd.Timestamp(df['timestamp'].iloc[-1]).value for df in data.values()) // 10 ** 6
        if period == 'fresh':
            lo = E.END_12M_MS
        months[period] = (hi - lo) / 86_400_000 / 30.44
        E._exec.pop(period, None)
    t = pd.DataFrame(trades)
    t.to_pickle(os.path.join(OUT, f'trades_{sample}.pkl'))
    lines = report(t, pd.DataFrame(funnel), months, sample)
    text = '\n'.join(lines)
    with open(os.path.join(OUT, f'eval_{sample}.txt'), 'w', encoding='utf-8') as fh:
        fh.write(text + '\n')
    print(text)


def report(t, fn, months, sample):
    pd.set_option('display.width', 250)
    pd.set_option('display.max_columns', 30)
    L = [f'Структура в живой SMC — {sample}: нынешняя (fractal) против классической (protected)',
         'месяцев: ' + ', '.join(f'{p} {m:.1f}' for p, m in months.items())]

    def block(variant, periods):
        g = t[(t.variant == variant) & t.period.isin(periods)]
        m = sum(months[p] for p in periods)
        return pd.DataFrame({mode: stats(g[g['mode'] == mode], m) for mode in MODES})

    # 1. Приёмка по протоколу
    L.append('\n1. Протокол: отбор bear+mid1, приёмка mid2+12m+fresh (итог R и R на сделку выше нынешней)')
    verdict = {}
    for name, periods, variant in (('отбор', SELECT, 'толпа'), ('приёмка', ACCEPT, 'толпа'),
                                   ('приёмка, насквозь', ACCEPT, 'насквозь')):
        b = block(variant, periods)
        ok = all(b.loc[k, 'protected'] > b.loc[k, 'fractal'] for k in ('итог R', 'R на сделку'))
        verdict[name] = ok
        L.append(f'  {name:18s} fractal {b.loc["сделок", "fractal"]:4.0f} сд {b.loc["итог R", "fractal"]:+7.1f}R '
                 f'{b.loc["R на сделку", "fractal"]:+.3f} | protected {b.loc["сделок", "protected"]:4.0f} сд '
                 f'{b.loc["итог R", "protected"]:+7.1f}R {b.loc["R на сделку", "protected"]:+.3f} -> '
                 f'{"выше" if ok else "НЕ выше"}')
    passed = all(verdict.values())
    L.append(f'  ИТОГ: {"прошла на этой выборке" if passed else "не прошла"}')

    # 2. Сводка
    for variant in VARIANTS:
        for name, periods in (('все периоды', PERIODS), ('приёмка', ACCEPT)):
            L.append(f'\n2. {variant}, {name}:')
            L.append(block(variant, periods).round(3).to_string())

    # 3. Воронка
    L.append('\n3. Воронка (fresh — после конца 12m): часы с сетапом -> зоны -> заявки (пороги бота) -> с фильтром толпы')
    L.append(fn.set_index(['period', 'mode']).to_string())
    tot = fn.groupby('mode')[['часов с сетапом', 'зон', 'заявок без толпы', 'заявок с толпой']].sum()
    L.append('всего:\n' + tot.to_string())

    # 4. По периодам
    L.append('\n4. По периодам (толпа): сделок | в месяц | плюсовых | R на сделку | итог R | просадка R')
    for p in PERIODS:
        cells = []
        for mode in MODES:
            s = stats(t[(t.variant == 'толпа') & (t.period == p) & (t['mode'] == mode)], months[p])
            cells.append(f'{mode} {s["сделок"]:4d} {s["в месяц"]:5.1f} {s.get("плюсовых", 0):5.0%} '
                         f'{s.get("R на сделку", 0):+.3f} {s.get("итог R", 0):+7.1f} {s.get("макс. просадка R", 0):5.1f}')
        L.append(f'  {p:6s} ' + ' | '.join(cells))

    g = t[t.variant == 'толпа']
    # 5. Стороны, 6. Выходы, 7. Пары
    L.append('\n5. По сторонам (толпа, все периоды): сделок, R на сделку, итог R')
    L.append(g.groupby(['side', 'mode'])['R'].agg(['count', 'mean', 'sum']).round(3).unstack('mode').to_string())
    L.append('\n6. Как закрылись (толпа, доля сделок)')
    L.append(pd.crosstab(g['exit'], g['mode'], normalize='columns').round(3).to_string())
    L.append('\n7. По парам (толпа, все периоды): сделок / итог R')
    L.append(g.groupby(['pair', 'mode'])['R'].agg(['count', 'sum']).round(2).unstack('mode').to_string())

    # 8. Общие и новые сделки
    L.append('\n8. Общие сделки (пара, сторона, время входа совпадают) и свои у каждой структуры (толпа)')

    def keys(f):
        return list(zip(f['pair'], f['side'], f['entry_time']))

    a, b = g[g['mode'] == 'fractal'], g[g['mode'] == 'protected']
    common = set(keys(a)) & set(keys(b))
    for name, f in (('fractal', a), ('protected', b)):
        mask = np.array([x in common for x in keys(f)], bool)
        inn, own = f[mask], f[~mask]
        L.append(f'  {name:9s}: общих {len(inn):4d} ({inn.R.mean():+.3f} R, итог {inn.R.sum():+.1f}) | '
                 f'только свои {len(own):4d} ({own.R.mean() if len(own) else np.nan:+.3f} R, итог {own.R.sum():+.1f})')

    # 9. Разница средних
    for name, periods in (('все периоды', PERIODS), ('приёмка', ACCEPT)):
        x = g[(g['mode'] == 'fractal') & g.period.isin(periods)].R
        y = g[(g['mode'] == 'protected') & g.period.isin(periods)].R
        se = np.sqrt(x.var() / len(x) + y.var() / len(y))
        L.append(f'\n9. Разница R на сделку ({name}): {y.mean() - x.mean():+.3f} ± {1.96 * se:.3f} '
                 f'(t {(y.mean() - x.mean()) / se:+.2f}; сделки двух вариантов во многом общие — t грубый)')
    return L


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    cmd = sys.argv[1]
    if cmd == 'gen':
        if '--workers' in ' '.join(sys.argv):
            WORKERS = int(next(a for a in sys.argv if a.startswith('--workers=')).split('=')[1])
        args = [a for a in sys.argv[2:] if not a.startswith('--')]
        gen(args[0], args[1] if len(args) > 1 else 'pool', reverse='--reverse' in sys.argv)
    else:
        evaluate(sys.argv[2] if len(sys.argv) > 2 else 'pool')
