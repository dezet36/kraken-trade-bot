"""
Аудит SMC (30.09.2026): стенд по всей истории против «глаз бота» — одни и те
же зоны, разные цели.

Стенд research/smc_lab.py строит структуру по всей истории периода; бот — по
окну последних свечей (research/smc_lab_live.py повторяет его до цента). Пулы
ликвидности, которые ядро ставит первой целью, от глубины истории зависят,
вход и стоп — нет. Здесь:
  1. заявки живых правил (без фильтра толпы) из обоих источников: общие зоны,
     только у стенда, только у бота;
  2. общие зоны: совпали ли цели и R:R; R каждой заявки отдельно с целями
     стенда и с целями бота — во что обходится расхождение;
  3. то же для зон, которые пропускает фильтр толпы (−1 б.п.).

Запуск (после smc_lab.py gen и smc_lab_live.py gen):
    python research/smc_audit_targets.py > research/results/smc_audit_targets.txt
"""
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import smc_lab_eval as E                              # noqa: E402
from common import ci                                 # noqa: E402
from smc_audit_funding import passes, signed          # noqa: E402

E.BAR_H.setdefault('live', 1.0)


def orders(period, layout):
    spec = dict(E.LIVE, filt=lambda r, f: True, layouts=(layout,))
    out = {}
    for o, r in E.per_setup(period, spec, E.orders_for(period, spec)):
        row = o.meta['row']
        key = (row.pair, row.poi_type, int(row.poi_index), int(row.dir))
        out[key] = {'order': o, 'r': r, 'rr': o.meta['rr'], 'targets': list(o.targets),
                    'pass': passes(signed(o.meta['feats'])), 't': o.created}
    return out


def orders_retry(period, spec, layout):
    """
    Заявки «как у бота»: каждый час, пока зона жива и проходит пороги, — новая
    попытка. Стенд оставляет по зоне одну заявку — первое прошедшее
    срабатывание; если в тот час пару занимала заявка, шла пауза или был полон
    кэп, зона терялась. Бот пробует снова каждые 5 минут. Портфель
    (run_portfolio) ставит первую попытку, которую пропускают его правила, и
    дальше зону не повторяет (seen_keys).
    """
    f = E.rows(period, layout)
    f = f[f['pair'].isin(spec['pairs'])]
    entry = f['entry_near']
    sl = (entry - f['stop']).abs()
    stop_pct = sl / entry * 100
    keep = (f['confluence'] >= spec['min_conf'] - 1e-9) & (stop_pct >= spec['min_stop'] - 1e-9)
    f, entry, sl = f[keep], entry[keep], sl[keep]
    out = []
    for r, e, s in zip(f.itertuples(), entry, sl):
        fr = list(r.fractions)
        tg = list(r.targets[:len(fr)])
        fr = fr[:len(tg)]
        if not fr:
            continue
        fr[-1] += 1.0 - sum(fr)
        rr = sum(a * abs(t - e) for a, t in zip(fr, tg)) / s
        if rr < spec['min_rr'] - 1e-9:
            continue
        feats = None
        if spec['filt'] is not None:
            feats = E.features(period, r.pair, int(r.t), int(r.dir), float(e), float(r.stop), float(r.targets[0]))
            if feats is None or not spec['filt'](r, feats):
                continue
        limit = e * (1 + spec['offset'] * r.dir)
        share = limit / abs(limit - r.stop) * E.ROUND_TRIP * 100
        if spec['cost_limit'] and share > spec['cost_limit']:
            continue
        created = np.datetime64(int(r.t), 'ms')
        key = (r.pair, r.poi_type, int(r.poi_index), int(r.dir))
        out.append(E.Order(pair=r.pair, direction='BULLISH' if r.dir > 0 else 'BEARISH', entry=float(limit),
                           stop=float(r.stop), targets=tg, fractions=fr, created=created,
                           expires=created + np.timedelta64(int(spec['expiry'] * 3600), 's'), key=key,
                           meta={'row': r, 'rr': float(rr), 'feats': feats}))
    out.sort(key=lambda o: (o.created, -o.meta['row'].confluence, -o.meta['rr']))
    return out


def run_retry(layout, filt, label, fill_through=0.0):
    spec = dict(E.LIVE, filt=filt, fill_through=fill_through)
    per, allr = {}, []
    for period in E.PERIODS:
        res, _ = E.portfolio(period, spec, orders=orders_retry(period, spec, layout))
        rs = [t['pnl'] / t['risk'] for t in res['trades']]
        per[period] = sum(rs)
        allr += rs
    print(f'  {label:52s} {fmt(allr)}  ' + ' '.join(f'{p} {per[p]:+6.1f}' for p in E.PERIODS), flush=True)


def fmt(r):
    r = np.asarray([x for x in r if x is not None], dtype=float)
    if len(r) < 10:
        return f'{len(r):4d} сд {r.sum() if len(r) else 0:+7.1f}R'
    lo, hi = ci(r)
    return f'{len(r):4d} сд {r.sum():+7.1f}R {r.mean():+.3f} [{lo:+.3f}; {hi:+.3f}]'


def main():
    total = {'both': 0, 'lab': 0, 'live': 0, 'same_targets': 0, 'same_rr_gate': 0}
    pairs_r = {'all': ([], []), 'pass': ([], [])}
    only = {'lab': [], 'live': [], 'lab_pass': [], 'live_pass': []}
    later = []
    print('1. ЗАЯВКИ ЖИВЫХ ПРАВИЛ (без фильтра толпы): стенд по всей истории и окно бота')
    for period in E.PERIODS:
        lab, live = orders(period, '1h'), orders(period, 'live')
        common = set(lab) & set(live)
        total['both'] += len(common)
        total['lab'] += len(set(lab) - common)
        total['live'] += len(set(live) - common)
        same = 0
        for k in common:
            a, b = lab[k], live[k]
            if len(a['targets']) == len(b['targets']) and all(
                    abs(x / y - 1) < 1e-9 for x, y in zip(a['targets'], b['targets'])):
                same += 1
            later.append((b['t'] - a['t']) / np.timedelta64(1, 'h'))
            if a['r'] is not None and b['r'] is not None:
                pairs_r['all'][0].append(a['r'])
                pairs_r['all'][1].append(b['r'])
                if b['pass']:
                    pairs_r['pass'][0].append(a['r'])
                    pairs_r['pass'][1].append(b['r'])
        total['same_targets'] += same
        for k in set(lab) - common:
            only['lab'].append(lab[k]['r'])
            if lab[k]['pass']:
                only['lab_pass'].append(lab[k]['r'])
        for k in set(live) - common:
            only['live'].append(live[k]['r'])
            if live[k]['pass']:
                only['live_pass'].append(live[k]['r'])
        print(f'  {period:6s} стенд {len(lab):4d}, бот {len(live):4d}, общих {len(common):4d} '
              f'(цели совпали у {same}), только стенд {len(set(lab) - common)}, только бот {len(set(live) - common)}')
    later = np.array(later)
    print(f'  всего общих {total["both"]}, цели совпали у {total["same_targets"]} '
          f'({total["same_targets"] / max(total["both"], 1):.0%}); только стенд {total["lab"]}, только бот {total["live"]}')
    print(f'  заявка бота по общей зоне позже стенда: медиана {np.median(later):+.1f} ч, '
          f'в тот же час {np.mean(later == 0):.0%}')
    print()
    print('2. ОБЩИЕ ЗОНЫ, R КАЖДОЙ ЗАЯВКИ ОТДЕЛЬНО (налив касанием)')
    for name, label in (('all', 'все общие'), ('pass', 'общие, которые пропускает фильтр толпы')):
        a, b = pairs_r[name]
        d = np.array(b) - np.array(a)
        print(f'  {label}:')
        print(f'    с целями стенда {fmt(a)}')
        print(f'    с целями бота   {fmt(b)}')
        if len(d) > 10:
            lo, hi = ci(d)
            print(f'    разница «бот − стенд» {d.mean():+.3f} [{lo:+.3f}; {hi:+.3f}] R на сделку')
    print()
    print('3. ЗОНЫ ТОЛЬКО У ОДНОГО ИЗ ДВУХ')
    print(f'  только стенд: {fmt(only["lab"])};  из них проходят фильтр {fmt(only["lab_pass"])}')
    print(f'  только бот:   {fmt(only["live"])};  из них проходят фильтр {fmt(only["live_pass"])}')
    print()
    print('4. ПОРТФЕЛЬ С ЖИВЫМИ ПРАВИЛАМИ: одна попытка на зону (стенд) и повтор каждый час (как бот)')
    crowd = lambda r, f: passes(signed(f))                          # noqa: E731
    for layout, name in (('1h', 'стенд по всей истории'), ('live', 'окно бота')):
        for filt, fname in ((None, 'без фильтра'), (crowd, 'фильтр −1 б.п.')):
            spec = {'filt': filt, 'layouts': (layout,)}
            E.run(spec, f'{name}, {fname}, одна попытка')
            E.run(dict(spec, fill_through=0.0005), f'{name}, {fname}, одна попытка, насквозь')
            run_retry(layout, filt, f'{name}, {fname}, повтор каждый час')
            run_retry(layout, filt, f'{name}, {fname}, повтор каждый час, насквозь', fill_through=0.0005)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
