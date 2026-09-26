"""
Набор сетапов для замера фильтров: каждый сетап каркаса — строка с признаками
на момент постановки и исходом по 5m.

ЗАЧЕМ. Прежде чем решать, где ИИ может сделать торговлю прибыльной, нужно
знать, есть ли вообще в данных, которые ему показывают, что-то, отделяющее
удачный сетап от неудачного. Фильтр (модель или формула) не может быть лучше
информации, на которой он решает. Если ни один признак и ни одна их
комбинация не держат знак вне выборки на пяти периодах, модель, читающая те
же числа, отобрать лучшие сетапы не сможет — сколько её ни учи.

Каркасы:
    smc       — боевое ядро SMC (backtest_smc.smc_orders), каждая заявка
                исполнена отдельно (smc_engine.simulate_order), без портфеля:
                фильтр решает о каждой заявке, портфель — потом;
    doctrine  — доктрина ИИ без модели (research/ai_doctrine.py).

Признаки — только из того, что было известно на момент постановки: закрытые
часовые свечи, открытый интерес не моложе часа, фандинг, уже выплаченный.
Сторона сделки учтена знаком: «ход за 24 ч» — в сторону сделки.

Запуск (строит и кладёт research/results/ai_setups_<период>.pkl):
    python research/ai_setups.py            # все периоды
    python research/ai_setups.py mid1 mid2
"""
import os
import sys
import time

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, 'Live_Bot'))
sys.path.insert(0, HERE)

import logger                                          # noqa: E402
logger.log = lambda *a, **k: None

import ai_doctrine as D                               # noqa: E402
import backtest_smc as bt                             # noqa: E402
from smc import params as smc_params                  # noqa: E402
from smc_engine import _prepare, simulate_order       # noqa: E402

H = 3_600_000
OUT = os.path.join(HERE, 'results')


# ── Позиционирование из кэша ─────────────────────────────────────────────────
def load_series(cache, sub, pair, column):
    name = f'{pair}_1h.csv' if sub == 'open_interest' else f'{pair}.csv'
    path = os.path.join(ROOT, 'research', cache, sub, name)
    if not os.path.exists(path):
        return None
    frame = pd.read_csv(path)
    # Через разность, а не astype('int64'): у pandas 3 единица метки зависит от
    # разбора строки (секунды, микросекунды), и деление на 10**6 давало бы то
    # секунды, то миллисекунды — ряд тихо съезжал бы целиком.
    stamps = pd.to_datetime(frame['timestamp'], utc=True)
    ts = ((stamps - pd.Timestamp('1970-01-01', tz='UTC')) // pd.Timedelta(milliseconds=1)).to_numpy(dtype='int64')
    order = np.argsort(ts)
    return ts[order], frame[column].to_numpy(dtype=float)[order]


def value_before(series, t_ms):
    """Последнее значение с меткой ≤ t_ms; None, если такого нет."""
    if series is None:
        return None
    ts, values = series
    k = int(np.searchsorted(ts, t_ms, side='right')) - 1
    return values[k] if k >= 0 else None


def efficiency(closes):
    path = np.abs(np.diff(closes)).sum()
    return abs(closes[-1] - closes[0]) / path if path > 0 else 0.0


# ── Признаки ─────────────────────────────────────────────────────────────────
def features(a1, t_ms, side, entry, stop, tp1, btc, oi, fr):
    """
    a1 — часовые свечи пары [ts, o, h, l, c, v]; решение принято в t_ms (закрытие
    часа). Всё — по свечам, закрывшимся к t_ms.
    """
    i = int(np.searchsorted(a1[:, 0], t_ms - H, side='right')) - 1
    if i < 24 * 31:
        return None
    sgn = 1.0 if side == 'LONG' else -1.0
    c = a1[:, 4]
    close = c[i]
    hi30, lo30 = a1[i - 719:i + 1, 2].max(), a1[i - 719:i + 1, 3].min()
    pos30 = (close - lo30) / (hi30 - lo30) * 100 if hi30 > lo30 else 50.0
    atr = D.atr_pct(a1[max(0, i - 800):i + 1])
    atr_now = atr[-1]
    window = atr[-720:]
    atr_rank = float((window[np.isfinite(window)] < atr_now).mean() * 100) if np.isfinite(atr_now) else np.nan
    spans = [a1[s:s + 24, 2].max() - a1[s:s + 24, 3].min() for s in range(i - 167, i - 22, 24)]
    adr = float(np.mean(spans) / close * 100)
    vol = a1[:, 5]
    vol_ratio = vol[i - 23:i + 1].sum() / (vol[i - 167:i + 1].sum() / 7) if vol[i - 167:i + 1].sum() > 0 else np.nan
    daily = c[np.arange(i, -1, -24)][::-1]                # закрытия каждые 24 ч до i
    ema = daily[0]
    k = 2 / 51
    for x in daily[1:]:
        ema = x * k + ema * (1 - k)
    stop_pct = abs(entry - stop) / entry * 100
    rr1 = abs(tp1 - entry) / abs(entry - stop) if entry != stop else np.nan
    row = {
        'ret_4h': (close / c[i - 4] - 1) * 100 * sgn,
        'ret_24h': (close / c[i - 24] - 1) * 100 * sgn,
        'ret_7d': (close / c[i - 168] - 1) * 100 * sgn,
        'ret_30d': (close / c[i - 720] - 1) * 100 * sgn,
        'pos30': pos30 if sgn > 0 else 100 - pos30,
        'atr_pct': atr_now,
        'atr_rank': atr_rank,
        'adr_pct': adr,
        'vol_ratio': vol_ratio,
        'dist_entry_pct': abs(close - entry) / close * 100,
        'stop_pct': stop_pct,
        'stop_adr': stop_pct / adr if adr else np.nan,
        'rr1': rr1,
        'tp_adr': stop_pct * rr1 / adr if adr else np.nan,
        'ema50d_gap': (close / ema - 1) * 100 * sgn,
        'er_30d': efficiency(daily[-31:]) if len(daily) > 31 else np.nan,
        'hour': int(((t_ms // H) % 24)),
        'weekday': int(((t_ms // (24 * H)) + 3) % 7),     # 0 = понедельник
    }
    if btc is not None:
        j = int(np.searchsorted(btc[:, 0], t_ms - H, side='right')) - 1
        if j >= 24 * 31:
            bc = btc[:, 4]
            bdaily = bc[np.arange(j, -1, -24)][::-1]
            row['btc_ret_24h'] = (bc[j] / bc[j - 24] - 1) * 100 * sgn
            row['btc_ret_7d'] = (bc[j] / bc[j - 168] - 1) * 100 * sgn
            row['btc_er_30d'] = efficiency(bdaily[-31:]) if len(bdaily) > 31 else np.nan
    oi_now = value_before(oi, t_ms - H)
    oi_24 = value_before(oi, t_ms - 25 * H)
    oi_4 = value_before(oi, t_ms - 5 * H)
    if oi_now and oi_24:
        row['oi_chg_24h'] = (oi_now / oi_24 - 1) * 100
        # Ход цены и ОИ вместе: плюс — позиция набирается по ходу цены.
        row['oi_x_price'] = row['oi_chg_24h'] * np.sign(close / c[i - 24] - 1)
    if oi_now and oi_4:
        row['oi_chg_4h'] = (oi_now / oi_4 - 1) * 100
    if fr is not None:
        ts, values = fr
        k = int(np.searchsorted(ts, t_ms, side='right')) - 1
        if k >= 2:
            # Плюс — толпа стоит в сторону сделки и платит за это.
            row['funding'] = values[k] * 1e4 * sgn
            row['funding_3'] = values[k - 2:k + 1].mean() * 1e4 * sgn
    return row


# ── Каркасы ──────────────────────────────────────────────────────────────────
def smc_rows(period, pair, frames, a1, btc, oi, fr):
    orders = bt.smc_orders(pair, frames)
    exec_arrays = _prepare(frames['5m'])
    out = []
    for o in orders:
        start = int(np.searchsorted(exec_arrays['ts'], o.created))
        res = simulate_order(o, exec_arrays, start, 100.0,
                             breakeven_after_tp1=smc_params.BREAKEVEN_AFTER_TP1,
                             max_hold_hours=smc_params.MAX_POSITION_HOLD_HOURS,
                             cancel_at_target=smc_params.CANCEL_PENDING_AT_TARGET)
        side = 'LONG' if o.direction in ('BULLISH', 'LONG') else 'SHORT'
        t_ms = int(pd.Timestamp(o.created).value // 10 ** 6)
        row = features(a1, t_ms, side, o.entry, o.stop, o.targets[0], btc, oi, fr)
        if row is None:
            continue
        factors = o.meta.get('factors') or {}
        row.update({'period': period, 'skeleton': 'smc', 'pair': pair, 't': t_ms, 'side': side,
                    'filled': res is not None, 'r': (res['pnl'] / res['risk']) if res else np.nan,
                    'confluence': o.meta.get('confluence'), 'poi_type': o.meta.get('poi_type'),
                    **{f'f_{k}': bool(v) for k, v in factors.items()}})
        out.append(row)
    return out


def doctrine_rows(period, pair, d, btc, oi, fr):
    trades, _ = D.run_pair(d, dict(D.BASE))
    out = []
    for t in trades:
        row = features(d['a1'], int(t['t']), t['side'], t['entry'], t['stop'], t['targets'][0], btc, oi, fr)
        if row is None:
            continue
        row.update({'period': period, 'skeleton': 'doctrine', 'pair': pair, 't': int(t['t']),
                    'side': t['side'], 'filled': True, 'r': t['r']})
        out.append(row)
    return out


def fibo_rows(period):
    """
    Фибо — заявки точной симуляции живого бота (research/fibo_live_sim.py,
    results/fibo_live_orders_<кэш>.pkl): тот же боевой сканер на 5-минутных
    шагах. Здесь каждая заявка в пределе издержек 5% исполнена отдельно по
    правилам брокера для фибо: срок 72 ч (в заявке), снятие у цели, безубыток
    на уровне B (в заявке), без безубытка после цели.
    """
    cache = D.PERIODS[period]
    path = os.path.join(OUT, f'fibo_live_orders_{cache}.pkl')
    if not os.path.exists(path):
        return []
    import pickle
    with open(path, 'rb') as fh:
        orders = pickle.load(fh)
    bt.CACHE_DIR = os.path.join(ROOT, 'research', cache)
    btc = D.load(cache, 'BTCUSDT', '1h')
    out, seen = [], set()
    by_pair = {}
    for o in orders:
        if o.meta.get('cost_share', 0) > 5.0 or o.key in seen:
            continue
        seen.add(o.key)
        by_pair.setdefault(o.pair, []).append(o)
    for pair, group in by_pair.items():
        frames = bt.load_pair(pair)
        a1 = D.load(cache, pair, '1h')
        if frames is None or a1 is None:
            continue
        exec_arrays = _prepare(frames['5m'])
        oi = load_series(cache, 'open_interest', pair, 'open_interest')
        fr = load_series(cache, 'funding', pair, 'funding_rate')
        for o in group:
            start = int(np.searchsorted(exec_arrays['ts'], o.created))
            res = simulate_order(o, exec_arrays, start, 100.0, breakeven_after_tp1=False,
                                 max_hold_hours=336.0, cancel_at_target=True)
            t_ms = int(pd.Timestamp(o.created).value // 10 ** 6)
            row = features(a1, t_ms, o.direction, o.entry, o.stop, o.targets[0], btc, oi, fr)
            if row is None:
                continue
            row.update({'period': period, 'skeleton': 'fibo', 'pair': pair, 't': t_ms, 'side': o.direction,
                        'filled': res is not None, 'r': (res['pnl'] / res['risk']) if res else np.nan})
            out.append(row)
        print(f'  fibo {period} {pair:13s} заявок {len(group):4d}', flush=True)
    return out


def build(period):
    cache = D.PERIODS[period]
    bt.CACHE_DIR = os.path.join(ROOT, 'research', cache)
    btc = D.load(cache, 'BTCUSDT', '1h')
    rows = []
    for pair in D.PAIRS:
        d = D.prepare(cache, pair)
        if d is None:
            continue
        started = time.time()
        oi = load_series(cache, 'open_interest', pair, 'open_interest')
        fr = load_series(cache, 'funding', pair, 'funding_rate')
        frames = bt.load_pair(pair)
        got = smc_rows(period, pair, frames, d['a1'], btc, oi, fr) if frames else []
        got += doctrine_rows(period, pair, d, btc, oi, fr)
        rows += got
        print(f'  {period} {pair:13s} строк {len(got):4d}  ОИ {"да" if oi else "нет"}  '
              f'фандинг {"да" if fr else "нет"}  {time.time() - started:5.1f} с', flush=True)
    frame = pd.DataFrame(rows)
    frame.to_pickle(os.path.join(OUT, f'ai_setups_{period}.pkl'))
    return frame


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    if sys.argv[1:2] == ['fibo']:
        for name in (sys.argv[2:] or ['bear', 'mid1', 'mid2', '12m']):
            rows = pd.DataFrame(fibo_rows(name))
            rows.to_pickle(os.path.join(OUT, f'ai_setups_fibo_{name}.pkl'))
            done = rows[rows.filled] if len(rows) else rows
            print(f'fibo {name}: заявок {len(rows)}, сделок {len(done)}'
                  + (f', в среднем {done.r.mean():+.3f}R' if len(done) else ''), flush=True)
        sys.exit(0)
    for name in (sys.argv[1:] or list(D.PERIODS)):
        f = build(name)
        smc = f[(f.skeleton == 'smc') & f.filled]
        doc = f[f.skeleton == 'doctrine']
        print(f'{name}: SMC сделок {len(smc)} (в среднем {smc.r.mean():+.3f}R), '
              f'доктрина {len(doc)} ({doc.r.mean():+.3f}R)', flush=True)
