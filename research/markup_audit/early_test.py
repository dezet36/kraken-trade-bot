"""
SMC только от раннего разворота (PROTOCOL_EARLY.md, записан до расчёта).

Метка сетапа: слом, создавший ордер-блок (poi.find_order_blocks → break_index),
есть у классической структуры на том же окне бота — «классический», нет —
«ранний разворот». Правило E — только ранний разворот; база — живая SMC.

    python research/markup_audit/early_test.py [oos x12 pool]

Строки — research/results/structure_audit/rows_fractal_<выборка>_<период>.pkl
(structure_test.py gen fractal <выборка>); метки кэшируются рядом (labels_*).
"""
import os
import sys
import warnings

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import structure_test as T                                   # noqa: E402  (пути research и Live_Bot)
from analysis.smc import params as P, poi as poi_mod, structure  # noqa: E402

MIN_CONF, MIN_STOP = 4.5, 0.8                                # пороги бота: ниже — заявкой не станет
SAMPLES = {'oos': T.PERIODS, 'x12': ['12m'], 'pool': T.PERIODS}
NAME = {'oos': 'A — пары списка бота вне пула', 'x12': 'B — 23 пары 12m вне списка бота',
        'pool': 'пул 10 пар SMC (справка, не критерий)'}


def _structures(window):
    try:
        P.STRUCTURE_MODE = 'fractal'
        frac = structure.build_structure(window, tier='swing')
        P.STRUCTURE_MODE = 'protected'
        prot = structure.build_structure(window, tier='swing')
    finally:
        P.STRUCTURE_MODE = 'fractal'
    return frac, prot


def label_rows(f, period):
    """early (True/False/None) для строк, которые могут стать заявкой; окно 1ч — как в smc_lab_live."""
    import ai_doctrine as D
    import backtest_smc as bt
    from smc_lab_live import _stamps_ms
    stop_pct = (f['entry_near'] - f['stop']).abs() / f['entry_near'] * 100
    need = (f['confluence'] >= MIN_CONF - 1e-9) & (stop_pct >= MIN_STOP - 1e-9)
    out = pd.Series([None] * len(f), index=f.index, dtype=object)
    info = {'меток': 0, 'блок не найден': 0, 'зона не та': 0, 'два слома, метки разные': 0}
    D.PERIODS['fresh'] = 'backtest_cache_fresh20'          # как стенд: свежие свечи всех 20 пар
    bt.CACHE_DIR = os.path.join(T.RESEARCH, D.PERIODS[period])
    depth = P.LOOKBACK_POI + 4
    for pair, g in f[need].groupby('pair'):
        df = bt.load_cached(pair, '1h').reset_index(drop=True)
        closes = _stamps_ms(df) + 3_600_000
        for idx, r in g.iterrows():
            end = int(np.searchsorted(closes, int(r['t']), side='right'))
            start = max(0, end - depth)
            window = df.iloc[start:end].reset_index(drop=True)
            frac, prot = _structures(window)
            side = 'BULLISH' if r['dir'] > 0 else 'BEARISH'
            cands = [b for b in poi_mod.find_order_blocks(window, frac)
                     if b['index'] == r['poi_index'] - start and b['direction'] == side]
            if not cands:
                info['блок не найден'] += 1
                continue
            b = max(cands, key=lambda x: x['break_index'])
            if abs(b['top'] - r['top']) > 1e-9 or abs(b['bottom'] - r['bottom']) > 1e-9:
                info['зона не та'] += 1
                continue
            classic = {(e['index'], e['direction']) for e in prot['events']}
            labels = {(c['break_index'], side) not in classic for c in cands}
            info['два слома, метки разные'] += len(labels) > 1
            out[idx] = (b['break_index'], side) not in classic
            info['меток'] += 1
    return out, info


def labelled(sample, period):
    path = os.path.join(T.OUT, f'labels_{sample}_{period}.pkl')
    f = pd.read_pickle(T.path('fractal', sample, period)).sort_values(['t', 'pair']).reset_index(drop=True)
    if os.path.exists(path):
        lab = pd.read_pickle(path)
        f['early'] = lab['early'].to_numpy()
        return f, lab.attrs.get('info', {})
    lab, info = label_rows(f, period)
    keep = pd.DataFrame({'pair': f['pair'], 't': f['t'], 'early': lab})
    keep.attrs['info'] = info
    keep.to_pickle(path + '.tmp')
    os.replace(path + '.tmp', path)
    f['early'] = lab.to_numpy()
    return f, info


def run(sample):
    import smcr_live as SL                                    # fresh → кэш 20 пар
    E = SL.E
    trades, months, infos, counts = [], {}, {}, []
    for period in SAMPLES[sample]:
        f, info = labelled(sample, period)
        infos[period] = info
        pairs = tuple(T.sample_pairs(sample, period))
        sub = {'база': f, 'E': f[f['early'] == True].reset_index(drop=True)}       # noqa: E712
        for name, rows in sub.items():
            layout = f'early_{sample}_{name}'
            rows = rows.copy()
            rows['key'] = list(zip(rows['pair'], rows['poi_type'], rows['poi_index'], rows['dir'], [layout] * len(rows)))
            E._rows[(period, layout)] = rows
            E.BAR_H[layout] = 1.0
            base = dict(E.LIVE, pairs=pairs, layouts=(layout,))
            keep = (lambda o: not E.overlaps_12m(o)) if period == 'fresh' else (lambda o: True)
            n_orders = sum(map(keep, E.orders_for(period, dict(base, filt=lambda r, x: T.against(x)))))
            counts.append({'period': period, 'вариант': name, 'заявок с толпой': n_orders})
            for variant, (use_crowd, ft) in T.VARIANTS.items():
                spec = dict(base, filt=(lambda r, x: T.against(x)) if use_crowd else None, fill_through=ft)
                out, _ = E.portfolio(period, spec)
                trades += T._trade_rows(name, variant, period, out, E)
        data = E._exec.get(period, {})
        lo = min(pd.Timestamp(df['timestamp'].iloc[0]).value for df in data.values()) // 10 ** 6
        hi = max(pd.Timestamp(df['timestamp'].iloc[-1]).value for df in data.values()) // 10 ** 6
        months[period] = (hi - (E.END_12M_MS if period == 'fresh' else lo)) / 86_400_000 / 30.44
        E._exec.pop(period, None)
    t = pd.DataFrame(trades)
    t.to_pickle(os.path.join(T.OUT, f'early_trades_{sample}.pkl'))
    return t, months, infos, pd.DataFrame(counts)


def report(sample, t, months, infos, counts):
    periods = SAMPLES[sample]
    L = [f'\n=== {NAME[sample]} ===',
         'месяцев: ' + ', '.join(f'{p} {months[p]:.1f}' for p in periods),
         'метки: ' + '; '.join(f'{p} ' + ', '.join(f'{k} {v}' for k, v in infos[p].items()) for p in periods)]
    L.append('заявок с толпой: ' + ', '.join(f'{r.period} {r.вариант} {r["заявок с толпой"]}'
                                             for _, r in counts.iterrows()))

    def st(name, variant, ps):
        g = t[(t['mode'] == name) & (t.variant == variant) & t.period.isin(ps)]
        return T.stats(g, sum(months[p] for p in ps))

    for variant in T.VARIANTS:
        L.append(f'\n{variant}, все периоды выборки:')
        L.append(pd.DataFrame({n: st(n, variant, periods) for n in ('база', 'E')}).round(3).to_string())
    if len(periods) > 1:
        L.append('\nпо периодам (толпа): сделок | R на сделку | итог R  — база / E')
        for p in periods:
            a, b = st('база', 'толпа', [p]), st('E', 'толпа', [p])
            L.append(f'  {p:6s} {a["сделок"]:4d} {a.get("R на сделку", 0):+.3f} {a.get("итог R", 0):+7.1f} | '
                     f'{b["сделок"]:4d} {b.get("R на сделку", 0):+.3f} {b.get("итог R", 0):+7.1f}')
    g = t[t.variant == 'толпа']
    L.append('\nпо сторонам (толпа): сделок, R на сделку, итог R')
    L.append(g.groupby(['side', 'mode'])['R'].agg(['count', 'mean', 'sum']).round(3).unstack('mode').to_string())
    return L


def verdict(res):
    t, months = res['oos'][0], res['oos'][1]

    def st(name, variant, ps):
        g = t[(t['mode'] == name) & (t.variant == variant) & t.period.isin(ps)]
        return T.stats(g, sum(months[p] for p in ps))

    a, e = st('база', 'толпа', T.PERIODS), st('E', 'толпа', T.PERIODS)
    af, ef = st('база', 'насквозь', T.PERIODS), st('E', 'насквозь', T.PERIODS)
    tb, mb = res['x12'][0], res['x12'][1]
    xb = T.stats(tb[(tb['mode'] == 'база') & (tb.variant == 'толпа')], mb['12m'])
    xe = T.stats(tb[(tb['mode'] == 'E') & (tb.variant == 'толпа')], mb['12m'])
    better = sum(st('E', 'толпа', [p]).get('R на сделку', -9) >= st('база', 'толпа', [p]).get('R на сделку', -9)
                 for p in T.PERIODS)
    checks = [
        ('1. A: R на сделку выше и итог не ниже', e['R на сделку'] > a['R на сделку'] and e['итог R'] >= a['итог R'],
         f'база {a["сделок"]} сд {a["R на сделку"]:+.3f} {a["итог R"]:+.1f}R | E {e["сделок"]} сд '
         f'{e["R на сделку"]:+.3f} {e["итог R"]:+.1f}R'),
        ('2. A, налив насквозь', ef['R на сделку'] > af['R на сделку'] and ef['итог R'] >= af['итог R'],
         f'база {af["R на сделку"]:+.3f} {af["итог R"]:+.1f}R | E {ef["R на сделку"]:+.3f} {ef["итог R"]:+.1f}R'),
        ('3. B: R на сделку выше', xe['R на сделку'] > xb['R на сделку'],
         f'база {xb["сделок"]} сд {xb["R на сделку"]:+.3f} {xb["итог R"]:+.1f}R | E {xe["сделок"]} сд '
         f'{xe["R на сделку"]:+.3f} {xe["итог R"]:+.1f}R'),
        ('4. A: не хуже базы в 3 периодах из 5', better >= 3, f'{better} из 5'),
    ]
    L = ['\n=== ПРИЁМКА (PROTOCOL_EARLY.md) ===']
    for name, ok, text in checks:
        L.append(f'  {name}: {"да" if ok else "НЕТ"} — {text}')
    L.append(f'  ИТОГ: {"прошло" if all(ok for _, ok, _ in checks) else "не прошло"}')
    return L


def main(samples):
    warnings.simplefilter('ignore', RuntimeWarning)
    pd.set_option('display.width', 250)
    res, lines = {}, []
    for s in samples:
        res[s] = run(s)
        lines += report(s, *res[s])
        print(f'{s}: готово', flush=True)
    if 'oos' in res and 'x12' in res:
        lines = verdict(res) + lines
    text = '\n'.join(lines)
    with open(os.path.join(T.OUT, 'early_eval.txt'), 'w', encoding='utf-8') as fh:
        fh.write(text + '\n')
    print(text)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main(sys.argv[1:] or ['oos', 'x12', 'pool'])
