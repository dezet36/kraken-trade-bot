"""
Качество структуры само по себе, без стратегии: нынешняя ('fractal') против
классической ('protected'), 10 пар SMC, свечи стенда (кэши bear, mid1, mid2, 12m: 2022-01…2026-07), 1ч и 4ч.

  сломов, доля CHoCH, средняя длина тренда (свечей);
  ложный разворот — смена тренда, отменённая обратной сменой не позже W свечей;
  ход за тренд — от смены до следующей смены, в сторону тренда (б.п., до издержек);
  после смены тренда — ход цены через H свечей в сторону НОВОГО тренда
    (средний, б.п.; доля, где ход в эту сторону);
  состояние тренда — на каждой свече ход через H свечей в сторону текущего
    тренда (средний, б.п.): сколько направления несёт сам тренд.
Тренд на свече k — после сломов не позже k (без будущего: tests/test_structure_protected).

    python research/markup_audit/structure_quality.py
"""
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
RESEARCH = os.path.dirname(HERE)
sys.path.insert(0, RESEARCH)
sys.path.insert(0, os.path.join(os.path.dirname(RESEARCH), 'Live_Bot'))

from smcz import data as D                       # noqa: E402
from analysis.smc import params as P, structure  # noqa: E402

PAIRS = ('BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'XRPUSDT', 'BNBUSDT', 'DOGEUSDT', 'ADAUSDT', 'AVAXUSDT',
         'LINKUSDT', 'LTCUSDT')
# период — кэши стенда (CACHES ниже)
SETTINGS = {'1h': {'W': 24, 'H': (6, 24, 72)}, '4h': {'W': 6, 'H': (6, 18, 42)}}
OUT = os.path.join(RESEARCH, 'results', 'structure_audit')


CACHES = ('backtest_cache_bear', 'backtest_cache_mid1', 'backtest_cache_mid2', 'backtest_cache_12m')


def frames(pair, tf):
    """Свечи стенда по периодам (2022-01…2026-07, с разрывами между периодами):
    лёгкие кэши вместо минуток — их не хватало памяти держать рядом со стендом."""
    import backtest_smc as bt
    out = []
    for cache in CACHES:
        bt.CACHE_DIR = os.path.join(RESEARCH, cache)
        df = bt.load_cached(pair, tf)
        if df is not None and len(df):
            out.append(df[['timestamp', 'open', 'high', 'low', 'close']].reset_index(drop=True))
    return out


def measure(df, mode, tf):
    P.STRUCTURE_MODE = mode
    ev = structure.build_structure(df, tier='swing')['events']
    c = df.close.to_numpy()
    n = len(c)
    # тренд на каждой свече
    trend = np.zeros(n)
    idx = np.array([e['index'] for e in ev])
    side = np.array([1 if e['direction'] == 'BULLISH' else -1 for e in ev])
    k = np.searchsorted(idx, np.arange(n), side='right') - 1
    trend[k >= 0] = side[k[k >= 0]]
    flips = [j for j in range(1, len(ev)) if side[j] != side[j - 1]]
    fi = idx[flips] if flips else np.array([], int)
    W, Hs = SETTINGS[tf]['W'], SETTINGS[tf]['H']
    # ложный — следующая СМЕНА тренда (не следующий слом) не позже W свечей
    gaps = np.diff(fi)
    false = np.mean(gaps <= W) if len(gaps) else np.nan
    # ход за тренд — от смены до следующей смены в сторону тренда (до издержек)
    seg = np.log(c[fi[1:]] / c[fi[:-1]]) * side[np.array(flips[:-1], int)] * 1e4 if len(fi) > 1 else np.array([])
    out = {'сломов': len(ev), 'CHoCH': np.mean([e['type'] == 'CHOCH' for e in ev]),
           'длина тренда': n / max(len(flips), 1), 'ложных': false,
           'ход за тренд: б.п.': seg.mean() if len(seg) else np.nan,
           'ход за тренд: в сторону': (seg > 0).mean() if len(seg) else np.nan}
    for H in Hs:
        ok = fi[fi + H < n]
        r = np.log(c[ok + H] / c[ok]) * side[np.array(flips)][:len(ok)] * 1e4
        out[f'после смены {H}: б.п.'] = r.mean()
        out[f'после смены {H}: в сторону'] = (r > 0).mean()
        j = np.arange(n - H)
        rs = np.log(c[j + H] / c[j]) * trend[j] * 1e4
        out[f'состояние {H}: б.п.'] = rs[trend[j] != 0].mean()
    return out


def main():
    os.makedirs(OUT, exist_ok=True)
    rows = []
    for pair in PAIRS:
        for tf in ('1h', '4h'):
            for k, df in enumerate(frames(pair, tf)):
                for mode in ('fractal', 'protected'):
                    rows.append({'pair': pair, 'tf': tf, 'mode': mode, 'part': k, **measure(df, mode, tf)})
        print(pair, 'готово', flush=True)
    t = pd.DataFrame(rows)
    # по периодам — среднее по частям; дальше как было
    t = t.groupby(['pair', 'tf', 'mode']).mean(numeric_only=True).drop(columns='part').reset_index()
    pd.set_option('display.width', 250)
    lines = []
    for tf in ('1h', '4h'):
        g = t[t.tf == tf].drop(columns=['pair', 'tf']).groupby('mode').mean().T.dropna()
        lines.append(f'\n{tf}: среднее по 10 парам (ложный разворот — отменён за {SETTINGS[tf]["W"]} свечей)')
        lines.append(g.round(3).to_string())
        # по парам — главное: ход после смены и состояние на средней дистанции
        H = SETTINGS[tf]['H'][1]
        p = t[t.tf == tf].pivot(index='pair', columns='mode',
                                values=[f'после смены {H}: б.п.', f'состояние {H}: б.п.', 'ложных',
                                        'ход за тренд: б.п.'])
        lines.append(f'по парам ({tf}, H={H}):')
        lines.append(p.round(2).to_string())
    text = '\n'.join(lines)
    with open(os.path.join(OUT, 'structure_quality.txt'), 'w', encoding='utf-8') as fh:
        fh.write(text + '\n')
    print(text)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
