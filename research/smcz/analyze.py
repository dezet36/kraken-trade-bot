"""Разбор прогонов smcz: срез событий по признакам → сделки → итоги.

По умолчанию виден ТОЛЬКО DEV. VAL — только для отобранных вариантов
(reveal=('DEV','VAL')). HOLD открывается один раз, переменной окружения
SMCZ_OPEN_HOLD=1 — так протокол держит машина, а не память.
"""
from __future__ import annotations

import os
import pickle

import numpy as np
import pandas as pd

from smcz import data as D
from smcz import sim

OUT_ROOT = os.path.join(D.ROOT, 'results', 'smcz')


def _allowed(reveal):
    if 'HOLD' in reveal and os.getenv('SMCZ_OPEN_HOLD') != '1':
        raise RuntimeError('HOLD закрыт до фиксации правил (SMCZ_OPEN_HOLD=1)')
    return reveal


def load(tag, struct, geoms=None, pairs=None):
    """Собрать события и итоги по всем парам для одной структуры (tf, k).
    Возвращает (ev, res): ev — события с колонкой pair и uid; res — словарь
    геометрия → итоги с pair и uid события."""
    folder = os.path.join(OUT_ROOT, tag)
    files = sorted(f for f in os.listdir(folder) if f.endswith('.pkl'))
    evs, res = [], {}
    for f in files:
        pair = f[:-4]
        if pairs and pair not in pairs:
            continue
        with open(os.path.join(folder, f), 'rb') as fh:
            d = pickle.load(fh)
        ev = d['ev'][struct].copy()
        ev['pair'] = pair
        ev['uid'] = pair + ':' + ev.index.astype(str)
        ev['t1'] = d['t1']
        evs.append(ev)
        for key, r in d['res'].items():
            if (key[0], key[1]) != struct:
                continue
            g = key[2:]
            if geoms and g not in geoms:
                continue
            r = r.copy()
            r['pair'] = pair
            r['uid'] = pair + ':' + r['ev'].astype(str)
            res.setdefault(g, []).append(r)
        del d
    ev = pd.concat(evs, ignore_index=True)
    ev['period'] = D.period_of(ev.t.to_numpy())
    res = {g: pd.concat(v, ignore_index=True) for g, v in res.items()}
    return ev, res


def trades(ev, r, mask=None, exclusive=True, reveal=('DEV',)):
    """Сделки среза. mask — булев ряд по ev (по индексу ev) или None.
    Исключение «одна заявка/позиция на пару» — после среза."""
    _allowed(reveal)
    x = r.merge(ev, on=['uid', 'pair'], how='left', suffixes=('', '_ev'))
    if mask is not None:
        keep_uid = set(ev.uid[mask])
        x = x[x.uid.isin(keep_uid)]
    x = x[x.period.isin(reveal)]
    if exclusive:
        parts = []
        for pair, g in x.groupby('pair', sort=False):
            g = g.sort_values('i_place', kind='stable')
            k = sim.one_at_a_time(g.i_place.to_numpy(np.int64), g.end_i.to_numpy(np.int64),
                                  g.status.to_numpy(np.int64))
            parts.append(g[k])
        x = pd.concat(parts) if parts else x.iloc[:0]
    return x[x.status == sim.ST_FILLED].copy()


def boot_ci(t, r, n=2000, seed=7):
    """95% интервал среднего бутстрепом по дням (сделки дня — блок)."""
    if len(r) < 5:
        return np.nan, np.nan
    day = (np.asarray(t) // 1440)
    df = pd.DataFrame({'d': day, 'r': r})
    g = df.groupby('d').r.agg(['sum', 'count'])
    s, c = g['sum'].to_numpy(), g['count'].to_numpy()
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(s), size=(n, len(s)))
    m = s[idx].sum(1) / c[idx].sum(1)
    return np.percentile(m, 2.5), np.percentile(m, 97.5)


def stats(x, col='r_net', ci=False):
    r = x[col].to_numpy(np.float64)
    n = len(r)
    if n == 0:
        return dict(n=0)
    out = dict(n=n, mean=r.mean(), sum=r.sum(), win=(r > 0).mean(),
               t=r.mean() / (r.std(ddof=1) / np.sqrt(n)) if n > 2 else np.nan,
               pf=r[r > 0].sum() / max(-r[r < 0].sum(), 1e-9),
               stop_med=float(np.median(x.stop_pct)) if 'stop_pct' in x else np.nan)
    if ci:
        out['lo'], out['hi'] = boot_ci(x.t.to_numpy(), r)
    return out


def by(x, key, col='r_net'):
    g = x.groupby(key)[col]
    return pd.DataFrame({'n': g.size(), 'mean': g.mean(), 'sum': g.sum()})


def year(x):
    return pd.to_datetime(x.t * 60, unit='s', utc=True).dt.year
