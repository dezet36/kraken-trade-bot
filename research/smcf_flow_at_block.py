"""
SMC: поток сделок в последний час перед касанием ордер-блока
(docs/SMC_поток_у_блока_2026-10-27.md — гипотезы, пороги и приёмка записаны
там до расчёта).

H1 натиск в зону, H2 всплеск оборота на подходе, H3 стакан за блоком.
DEV — bear и mid1, VAL — mid2. HOLD (12m, fresh) не считается, пока владелец
не скажет: заявки этих периодов отбрасываются до любых признаков.

    python research/smcf_flow_at_block.py      # → results/smcf/flow_at_block.txt
"""
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, 'results', 'smcf')
DEV, VAL = ('bear', 'mid1'), ('mid2',)
WINDOW_MIN = 60
BOOT = 10_000
ALPHA_DEV = 0.05 / 3


def load_pair(pair):
    """Минутки пары (накопленные суммы), часовой оборот и стакан — один раз на
    пару: все 20 пар разом в память не помещаются."""
    k = pd.read_pickle(os.path.join(HERE, 'binance_klines_1m_cache', pair + '.pkl'))[['qv', 'tbq']]
    ts = k.index.values.astype('datetime64[m]').astype('int64')
    cqv = np.concatenate([[0.0], np.cumsum(k['qv'].values)])
    ctb = np.concatenate([[0.0], np.cumsum(k['tbq'].values)])
    h = k['qv'].resample('1h').sum()
    hourly = h.index.values.astype('datetime64[m]').astype('int64'), h.values
    del k
    p = os.path.join(HERE, 'binance_bookdepth_cache', pair + '.pkl')
    bk = None
    if os.path.exists(p):
        b = pd.read_pickle(p)
        bk = b.index.values.astype('datetime64[m]').astype('int64'), b['b1'].values, b['a1'].values
    return (ts, cqv, ctb), hourly, bk


def window_sum(kl, start_m, end_m):
    """Сумма оборота и покупок тейкером за минуты [start, end)."""
    ts, cqv, ctb = kl
    i, j = np.searchsorted(ts, start_m), np.searchsorted(ts, end_m)
    if j - i < WINDOW_MIN * 0.9:                # дыра в минутках — признака нет
        return np.nan, np.nan
    return cqv[j] - cqv[i], ctb[j] - ctb[i]


def features(o, kl, hourly, bk):
    touch_m = int((o.t + o.wait_h * 3_600_000) // 60_000)
    end_m = touch_m - touch_m % 5                  # свеча касания не видна
    start_m = end_m - WINDOW_MIN
    qv, tb = window_sum(kl, start_m, end_m)
    a = -o.dir * (2 * tb - qv) / qv if qv and qv == qv else np.nan
    hts, hqv = hourly
    k = np.searchsorted(hts, start_m)
    prior = hqv[max(0, k - 168):k]
    v = qv / np.median(prior) if len(prior) >= 120 and np.median(prior) > 0 else np.nan
    b = np.nan
    if bk is not None:
        bts, bid, ask = bk
        n = np.searchsorted(bts, end_m) - 1
        if n >= 0 and end_m - bts[n] <= 15:
            ours, other = (bid[n], ask[n]) if o.dir > 0 else (ask[n], bid[n])
            if ours + other > 0:
                b = (ours - other) / (ours + other)
    return a, v, b


def boot_p(kept, skipped, rng):
    """Односторонний p: доля бутстрэп-разниц «оставленные − снятые» ≤ 0."""
    if len(kept) < 10 or len(skipped) < 10:
        return np.nan, np.nan
    d = (rng.choice(kept, (BOOT, len(kept))).mean(1) - rng.choice(skipped, (BOOT, len(skipped))).mean(1))
    return kept.mean() - skipped.mean(), float((d <= 0).mean())


HYP = {
    'H1 натиск в зону': ('A', 'top'),
    'H2 всплеск оборота': ('V', 'top'),
    'H3 стакан за блоком': ('B', 'bottom'),
}


def main():
    os.makedirs(OUT, exist_ok=True)
    o = pd.read_pickle(os.path.join(HERE, 'results', 'smcr', 'live_orders.pkl'))
    o = o[o['sample'].isin(['pool', 'other']) & o['period'].isin(DEV + VAL) & o['r_ft'].notna()].copy()
    o['A'] = o['V'] = o['B'] = np.nan
    for pair, idx in o.groupby('pair').groups.items():
        kl, hourly, bk = load_pair(pair)
        o.loc[idx, ['A', 'V', 'B']] = [features(r, kl, hourly, bk) for r in o.loc[idx].itertuples()]
        del kl, hourly, bk
    o['crowd0'] = o['fund_bp'] <= 0
    o['set'] = np.where(o['period'].isin(DEV), 'DEV', 'VAL')
    o.to_pickle(os.path.join(OUT, 'flow_at_block_rows.pkl'))
    rng = np.random.default_rng(27)
    lines = [f'заявок, налившихся насквозь: DEV {int((o.set == "DEV").sum())}, VAL {int((o.set == "VAL").sum())}',
             f'база r_ft: DEV {o[o.set == "DEV"].r_ft.mean():+.3f}, VAL {o[o.set == "VAL"].r_ft.mean():+.3f}', '']
    for name, (col, side) in HYP.items():
        dev = o[(o.set == 'DEV') & o[col].notna()]
        cut = dev[col].quantile(2 / 3 if side == 'top' else 1 / 3)
        skip = (lambda x: x[col] > cut) if side == 'top' else (lambda x: x[col] < cut)
        lines.append(f'{name}: снять, если {col} {">" if side == "top" else "<"} {cut:.4f} (треть DEV)')
        for label, part in (('DEV', dev), ('VAL', o[(o.set == 'VAL') & o[col].notna()])):
            s = skip(part)
            diff, p = boot_p(part[~s].r_ft.values, part[s].r_ft.values, rng)
            lines.append(f'  {label}: оставлено {int((~s).sum())} {part[~s].r_ft.mean():+.3f}R | '
                         f'снято {int(s.sum())} {part[s].r_ft.mean():+.3f}R | все {part.r_ft.mean():+.3f}R | '
                         f'разница {diff:+.3f}, p {p:.3f}')
            if label == 'VAL':
                for sub, mask in (('pool', part['sample'] == 'pool'), ('other', part['sample'] == 'other'),
                                  ('толпа ≤ 0', part['crowd0'])):
                    q = part[mask]
                    sq = skip(q)
                    if sq.sum() and (~sq).sum():
                        lines.append(f'      {sub}: оставлено {int((~sq).sum())} {q[~sq].r_ft.mean():+.3f}R | '
                                     f'снято {int(sq.sum())} {q[sq].r_ft.mean():+.3f}R')
        lines.append('')
    text = '\n'.join(lines)
    with open(os.path.join(OUT, 'flow_at_block.txt'), 'w', encoding='utf-8') as fh:
        fh.write(text + '\n')
    print(text)


if __name__ == '__main__':
    sys.exit(main())
