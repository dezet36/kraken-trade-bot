"""
Тетрадь на 2021 годе — периоде, которого она не видела (28.09.2026, docs/ИИ_замечания_на_проверку.md, п. 75).

Данные — research/flow_cache_2021 (основные 20) и flow_cache_2021_extra (10
добавочных), скачаны research/ai_flow_fetch.py с FLOW_START=2020-11-01,
FLOW_END=2022-01-05. Пары — те, у которых в 2021 есть и свечи Binance, и ОИ
Bybit. Признаки, широта (по основным из имеющихся), сигналы, стоп, срок,
издержки — как в тетради v3.3, механика. Условия приёмки записаны до
скачивания данных (п. 75).

    python research/ai_notebook_2021.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ['FLOW_CACHE'] = 'flow_cache_2021'          # до импорта стенда: он читает папку при импорте
sys.path.insert(0, HERE)

import numpy as np                                    # noqa: E402
import pandas as pd                                   # noqa: E402

import ai_exit_grid as X                              # noqa: E402
import ai_model_trader_bt as B                        # noqa: E402
import ai_notebook_flags as F                         # noqa: E402
import ai_pattern_lab as L                            # noqa: E402

PERIOD = ('2021-01-01', '2022-01-01')
L.SPLITS['y2021'] = PERIOD
EXTRA_DIR = os.path.join(HERE, 'flow_cache_2021_extra')
RESULT = os.path.join(HERE, 'results', 'ai_notebook_2021.txt')


def covered(df):
    """Есть ли у пары в 2021 и свечи, и ОИ (больше половины часов периода)."""
    a, b = (pd.Timestamp(x, tz='UTC') for x in PERIOD)
    part = df[(df.index >= a) & (df.index < b)]
    return len(part) > 24 * 30 and part['oi'].notna().mean() > 0.5


def prepare():
    raw = {}
    for f in sorted(os.listdir(L.CACHE)):
        if f.endswith('.pkl'):
            df = pd.read_pickle(os.path.join(L.CACHE, f))
            raw[f[:-4]] = df[df['v'] > 0] if len(df) else df
    btc = raw['BTCUSDT']
    data = {p: (df, L.features(df, btc)) for p, df in raw.items() if len(df) and covered(df)}
    cascade = B.PATTERNS['P1']['conditions'][:2]
    trig = pd.DataFrame({p: pd.Series(L.signal_mask(f, cascade), index=f.index)
                         for p, (d, f) in data.items()}).fillna(False)
    breadth = trig.rolling(3, min_periods=1).max().sum(axis=1)
    for p in B.EXTRA:
        path = os.path.join(EXTRA_DIR, f'{p}.pkl')
        if os.path.exists(path):
            df = pd.read_pickle(path)
            df = df[df['v'] > 0] if len(df) else df
            if len(df) and covered(df):
                data[p] = (df, L.features(df, btc))
    for p, (d, f) in data.items():
        f['cascade_count_3h'] = breadth.reindex(f.index).to_numpy()
    return data


def main():
    lines = []

    def say(text=''):
        print(text, flush=True)
        lines.append(text)

    data = prepare()
    core = [p for p in data if p in B.POOL] if hasattr(B, 'POOL') else [p for p in data if p not in B.EXTRA]
    say(f'Тетрадь v3.3 на 2021 годе (п. 75): пар {len(data)} — '
        f'основные {sorted(p for p in data if p not in B.EXTRA)}, добавочные {sorted(p for p in data if p in B.EXTRA)}')

    weeks = (pd.Timestamp(PERIOD[1]) - pd.Timestamp(PERIOD[0])).days / 7
    per = {}
    for key in ('P1', 'P2', 'B3'):
        hold = B.PATTERNS[key]['hold']
        r = X.pattern_r(data, key, 'y2021', hold, None)
        per[key] = r
        t = r.mean() / r.std(ddof=1) * np.sqrt(len(r)) if len(r) > 1 else float('nan')
        say(f'  {key}: {len(r)} сд ({len(r) / weeks:.1f}/нед), средний R {r.mean() if len(r) else float("nan"):+.3f} '
            f'(t {t:+.1f}), плюс {np.mean(r > 0) * 100 if len(r) else 0:.0f}%')

    evs = B.alerts(data, 'y2021')
    got = B.play(evs, B.outcomes(data, evs), lambda t, c, h, f: [p for p, _ in c])
    pr = np.array([x[0] for x in got])
    eq = np.cumsum(pr)
    dd = float(np.min(eq - np.maximum.accumulate(eq))) if len(pr) else 0.0
    by = ', '.join(f"{k} {sum(1 for x in got if x[1] == k)} сд" for k in ('P1', 'P2', 'B3'))
    say(f'  портфель 6 мест: {len(pr)} сд ({len(pr) / weeks:.1f}/нед), средний R {pr.mean():+.3f}, '
        f'сумма {pr.sum():+.1f}R, просадка {dd:.1f}R ({by})')

    rows = pd.DataFrame(F.rows_of(data, 'y2021'))
    p1 = rows[rows.key == 'P1']
    if len(p1):
        hi, lo = p1[p1.score > 0], p1[p1.score <= 0]
        say(f'  справочно, флаги P1 (п. 71): счёт > 0 — {len(hi)} сд {hi.r.mean():+.3f}, '
            f'≤ 0 — {len(lo)} сд {lo.r.mean() if len(lo) else float("nan"):+.3f}')

    checks = [(f'1. P1 средний R > 0: {per["P1"].mean():+.3f}', len(per['P1']) > 0 and per['P1'].mean() > 0),
              (f'2. P2 средний R > 0: {per["P2"].mean():+.3f}', len(per['P2']) > 0 and per['P2'].mean() > 0),
              (f'3. портфель средний R > 0: {pr.mean():+.3f}', len(pr) > 0 and pr.mean() > 0)]
    say()
    for text, ok in checks:
        say(f"  {'да ' if ok else 'НЕТ'} {text}")
    verdict = all(ok for _, ok in checks)
    say(f"\nИтог: {'тетрадь держится на 2021' if verdict else 'тетрадь на 2021 не держится — разобрать'}")
    os.makedirs(os.path.dirname(RESULT), exist_ok=True)
    with open(RESULT, 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(lines) + '\n')


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
