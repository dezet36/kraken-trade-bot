"""
Выход из сделок тетради: срок и цель (28.09.2026, docs/ИИ_замечания_на_проверку.md, п. 74).

Сейчас выход — стоп или срок (P1 24 ч, P2 и B3 48 ч), цели нет. Сетка: срок
{12, 24, 36, 48, 72} ч × цель {нет, 1.0, 1.5, 2.0, 3.0}R, стоп прежний. Сделки —
полная маска без наложения (как цифры тетради); стоп и цель в одном часе — стоп.
Правило отбора записано до расчёта (п. 74): лучший на train (≥ +0.03R к текущему)
→ подтверждение на valid (≥ +0.02R) → портфель 6 мест на train и valid. test не трогаем.

    python research/ai_exit_grid.py
"""
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ai_model_trader_bt as B                        # noqa: E402
import ai_pattern_lab as L                            # noqa: E402

RESULT = os.path.join(HERE, 'results', 'ai_exit_grid.txt')
HOLDS = (12, 24, 36, 48, 72)
TPS = (None, 1.0, 1.5, 2.0, 3.0)
CURRENT = {'P1': (24, None), 'P2': (48, None), 'B3': (48, None)}


def simulate(df, mask, side, hold, stop_mult, atr_d, tp=None):
    """Как ai_pattern_lab.simulate, плюс цель tp (в R, лимитом, без проскальзывания); стоп — раньше цели."""
    o, h, l, c = (df[k].to_numpy(float) for k in ('o', 'h', 'l', 'c'))
    idx = np.flatnonzero(mask)
    trades, busy_until = [], -1
    n = len(df)
    sgn = 1.0 if side == 'long' else -1.0
    for i in idx:
        if i <= busy_until or i + 1 >= n or np.isnan(atr_d[i]):
            continue
        entry = o[i + 1] * (1 + sgn * L.SLIP)
        dist = stop_mult * atr_d[i] / 100.0
        stop = entry * (1 - sgn * dist)
        target = entry * (1 + sgn * tp * dist) if tp else None
        last = min(i + hold, n - 1)
        exit_px, j_exit = None, last
        for j in range(i + 1, last + 1):
            if (sgn > 0 and l[j] <= stop) or (sgn < 0 and h[j] >= stop):
                exit_px, j_exit = stop * (1 - sgn * L.SLIP), j
                break
            if target is not None and ((sgn > 0 and h[j] >= target) or (sgn < 0 and l[j] <= target)):
                exit_px, j_exit = target, j
                break
        if exit_px is None:
            exit_px = c[last] * (1 - sgn * L.SLIP)
        net = sgn * (exit_px / entry - 1) - 2 * L.FEE
        trades.append((df.index[i], net * 100, net / dist, j_exit - i))
        busy_until = j_exit
    return trades


def pattern_r(data, key, split, hold, tp):
    pat = B.PATTERNS[key]
    a, b = (pd.Timestamp(x, tz='UTC') for x in L.SPLITS[split])
    out = []
    for p, (df, f) in data.items():
        if pat.get('universe') == 'core' and p in B.EXTRA:
            continue
        m = L.signal_mask(f, pat['conditions']) & (f.index >= a) & (f.index < b)
        out += [x[2] for x in simulate(df, m, pat['side'], hold, pat['stop'], f['atr_d'].to_numpy(float), tp)]
    return np.array(out)


def outcomes(data, evs, exits):
    """Как ai_model_trader_bt.outcomes, но со своим сроком и целью у закономерности."""
    res = {}
    for t, items in evs:
        for p, key in items:
            df, f = data[p]
            i = f.index.get_loc(t)
            mask = np.zeros(len(f), bool)
            mask[i] = True
            pat = B.PATTERNS[key]
            hold, tp = exits[key]
            tr = simulate(df, mask, pat['side'], hold, pat['stop'], f['atr_d'].to_numpy(float), tp)
            if tr:
                res[(t, p)] = (tr[0][2], t + pd.Timedelta(hours=tr[0][3] + 1), key)
    return res


def portfolio(data, split, exits):
    evs = B.alerts(data, split)
    got = B.play(evs, outcomes(data, evs, exits), lambda t, c, h, f: [p for p, _ in c])
    a, b = (pd.Timestamp(x, tz='UTC') for x in L.SPLITS[split])
    r = np.array([x[0] for x in got])
    return len(r) / ((b - a).days / 7), r.sum(), r.mean()


def label(hold, tp):
    return f"{hold} ч{'' if tp is None else f', цель {tp}R'}"


def main():
    lines = []

    def say(text=''):
        print(text, flush=True)
        lines.append(text)

    data = B.prepare()
    # Проверка: без цели — ровно исходный симулятор.
    for key, (hold, tp) in CURRENT.items():
        pat = B.PATTERNS[key]
        a, b = (pd.Timestamp(x, tz='UTC') for x in L.SPLITS['train'])
        mine = pattern_r(data, key, 'train', hold, None)
        theirs = []
        for p, (df, f) in data.items():
            if pat.get('universe') == 'core' and p in B.EXTRA:
                continue
            m = L.signal_mask(f, pat['conditions']) & (f.index >= a) & (f.index < b)
            theirs += [x[2] for x in L.simulate(df, m, pat['side'], hold, pat['stop'], f['atr_d'].to_numpy(float))]
        assert np.allclose(mine, np.array(theirs)), key

    say('Выход тетради: средний R сделки, train | valid (п. 74)')
    accepted = dict(CURRENT)
    for key in ('P1', 'P2', 'B3'):
        grid = {}
        for hold in HOLDS:
            for tp in TPS:
                tr, va = pattern_r(data, key, 'train', hold, tp), pattern_r(data, key, 'valid', hold, tp)
                grid[(hold, tp)] = (tr.mean(), len(tr), va.mean(), len(va))
        cur = grid[CURRENT[key]]
        say(f'\n{key}: сейчас {label(*CURRENT[key])} — train {cur[0]:+.3f} ({cur[1]} сд), valid {cur[2]:+.3f} ({cur[3]} сд)')
        say('  цель →      ' + ''.join(f'{("нет" if tp is None else f"{tp}R"):>16s}' for tp in TPS))
        for hold in HOLDS:
            say(f'  {hold:3d} ч      ' + ''.join(f'{grid[(hold, tp)][0]:+7.3f} | {grid[(hold, tp)][2]:+6.3f}'
                                           for tp in TPS))
        best = max(grid, key=lambda k: grid[k][0])
        gain_tr, gain_va = grid[best][0] - cur[0], grid[best][2] - cur[2]
        if best == CURRENT[key] or gain_tr < 0.03:
            say(f'  лучший на train — {label(*best)} ({gain_tr:+.3f}): не кандидат (нужно ≥ +0.03)')
        elif gain_va < 0.02:
            say(f'  кандидат {label(*best)}: train {gain_tr:+.3f}, valid {gain_va:+.3f} — не подтверждён (нужно ≥ +0.02)')
        else:
            say(f'  кандидат {label(*best)}: train {gain_tr:+.3f}, valid {gain_va:+.3f} — ПОДТВЕРЖДЁН')
            accepted[key] = best

    if accepted != CURRENT:
        say('\nПортфель 6 мест, механика (сд/нед, сумма R, средний R):')
        ok = True
        for split in ('train', 'valid'):
            w0, s0, m0 = portfolio(data, split, CURRENT)
            w1, s1, m1 = portfolio(data, split, accepted)
            say(f'  {split}: сейчас {w0:.1f}/нед {s0:+.1f}R {m0:+.3f} | новое {w1:.1f}/нед {s1:+.1f}R {m1:+.3f}')
            ok &= s1 >= s0 and w1 >= 0.9 * w0
        say(f"\nИтог: {'новый выход принят: ' + ', '.join(f'{k} {label(*v)}' for k, v in accepted.items() if v != CURRENT[k]) if ok else 'портфель не подтвердил — выход не меняем'}")
    else:
        say('\nИтог: ни одна закономерность не прошла — выход не меняем')
    with open(RESULT, 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(lines) + '\n')


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
