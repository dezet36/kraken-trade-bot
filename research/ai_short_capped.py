"""
Шорт толпы с потолком мест (30.09.2026, п. 84) — условие записано до расчёта.

crowded_long_funding_short на всех 42 монетах, не больше 2 его позиций одновременно из 6 мест тетради,
в очереди часа — после P1, B3, P4. Приёмка в train, valid и 2021: сумма R выше, R на сделку портфеля ниже
не больше чем на 0.02R, просадка глубже не больше чем на 3R, своё R шорта вне выборки ≥ +0.05R.

    python research/ai_short_capped.py std
    python research/ai_short_capped.py 2021
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if sys.argv[1:2] == ['2021']:
    os.environ['FLOW_CACHE'] = 'flow_cache_2021'      # до импорта стенда: он читает папку при импорте
sys.path.insert(0, HERE)

import numpy as np                                    # noqa: E402
import pandas as pd                                   # noqa: E402

import ai_new_sources as S                            # noqa: E402

L = S.install()
import ai_model_trader_bt as B                        # noqa: E402
import ai_nearmiss_replication as R                   # noqa: E402
import ai_round5 as R5                                # noqa: E402
import ai_universe_extend as U                        # noqa: E402

CAP = 2


def play_capped(evs, res, cap_key='N', cap=CAP):
    """B.play с «брать все», но правило cap_key держит не больше cap мест. -> [(час, пара, R, закономерность)]."""
    held, kind, got = {}, {}, []
    for t, items in evs:
        held = {p: e for p, e in held.items() if e > t}
        kind = {p: k for p, k in kind.items() if p in held}
        free = B.SLOTS - len(held)
        n_cap = sum(1 for k in kind.values() if k == cap_key)
        for p, k in items:
            if free <= 0:
                break
            if p in held or (t, p) not in res or (k == cap_key and n_cap >= cap):
                continue
            r, exit_t, key = res[(t, p)]
            got.append((t, p, r, key))
            held[p], kind[p] = exit_t, key
            free -= 1
            n_cap += key == cap_key
    return got


def stats(got, weeks):
    r = np.array([x[2] for x in got])
    eq = np.cumsum(r)
    return len(r), len(r) / weeks, r.sum(), r.mean(), float(np.min(eq - np.maximum.accumulate(eq)))


def main(mode):
    B.PATTERNS = dict(U.NOTEBOOK)
    if mode == '2021':
        import ai_notebook_2021 as Y
        data, splits = Y.prepare(), ['y2021']
        new = U.load('flow_cache_2021_new', data['BTCUSDT'][0], data['BTCUSDT'][1]['cascade_count_3h'], U.NEW)
    else:
        data, splits = B.prepare(), ['train', 'valid']
        new = U.load('flow_cache_new', data['BTCUSDT'][0], data['BTCUSDT'][1]['cascade_count_3h'], U.NEW)
    both = S.enrich({**data, **new})
    base = list(data)
    core = [p for p in base if p not in B.EXTRA]
    was = {k: (core if U.NOTEBOOK[k].get('universe') == 'core' else base + (list(new) if k == 'P1' else []))
           for k in U.NOTEBOOK}
    rule = R.candidates()[0]
    assert rule['name'] == 'crowded_long_funding_short'
    pats = {**U.NOTEBOOK, 'N': R5.as_pattern(rule)}
    allowed = {**was, 'N': list(both)}
    lines = [f'Шорт толпы (п. 84), не больше {CAP} мест из {B.SLOTS}, {mode}: сумма R выше, R на сделку не ниже −0.02R, '
             'просадка не глубже 3R, своё R вне выборки ≥ +0.05R']
    verdict = True
    for split in splits:
        a, b = (pd.Timestamp(x, tz='UTC') for x in L.SPLITS[split])
        weeks = (b - a).days / 7
        out = {}
        for label, patterns in (('основа', dict(U.NOTEBOOK)), ('+ шорт', pats)):
            B.PATTERNS = patterns
            evs = U.alerts_u(both, split, {k: allowed[k] for k in patterns})
            got = play_capped(evs, B.outcomes(both, evs))
            out[label] = got
            n, per_w, total, mean, dd = stats(got, weeks)
            own = [x[2] for x in got if x[3] == 'N']
            lines.append(f"  {split:6s} {label:8s} {n:4d} сд {per_w:4.1f}/нед сумма {total:+6.1f}R на сделку {mean:+.3f} "
                         f"просадка {dd:6.1f}R  (" + ', '.join(f"{k} {sum(1 for x in got if x[3] == k)}" for k in patterns)
                         + ')' + (f"; шорт {len(own)} сд {np.mean(own):+.3f}" if own else ''))
        _, _, t0, m0, d0 = stats(out['основа'], weeks)
        _, _, t1, m1, d1 = stats(out['+ шорт'], weeks)
        own = [x[2] for x in out['+ шорт'] if x[3] == 'N']
        checks = [('сумма выше', t1 > t0), ('R на сделку не ниже −0.02', m1 >= m0 - 0.02), ('просадка не глубже 3R', d1 >= d0 - 3)]
        if split != 'train':
            checks.append(('своё R ≥ +0.05', bool(own) and float(np.mean(own)) >= 0.05))
        ok = all(c for _, c in checks)
        verdict &= ok
        lines.append(f"    {split}: " + '; '.join(f"{n} — {'да' if c else 'НЕТ'}" for n, c in checks))
    lines.append(f"\nИтог ({mode}): {'ПРОШЛО' if verdict else 'не прошло'}")
    text = '\n'.join(lines)
    print(text, flush=True)
    with open(os.path.join(HERE, 'results', f'ai_short_capped_{mode}.txt'), 'w', encoding='utf-8') as fh:
        fh.write(text + '\n')


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main(sys.argv[1])
